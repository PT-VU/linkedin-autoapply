"""Small, dependency-free OpenAI-compatible backend.

The public project defaults to the local Codex CLI.  This module exists for
users who prefer an API endpoint without forcing a vendor SDK or putting
secrets in the repository.  It intentionally accepts only the subset needed
by the supervisor: a prompt in, one JSON object out.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def _text_content(value: Any) -> str:
    """Normalize chat-completion content to text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    if isinstance(value, dict) and isinstance(value.get("text"), str):
        return value["text"]
    return ""


def parse_json_message(text: str) -> dict[str, Any]:
    """Parse JSON returned by an API, tolerating a fenced JSON response."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("API response JSON must be an object")
    return value


def call_openai_compatible(
    *,
    prompt: str,
    config: dict[str, Any],
    timeout: int,
    result_file: Path,
    stdout_file: Path,
) -> dict[str, Any]:
    """Call an OpenAI-compatible ``/chat/completions`` endpoint.

    ``config`` is the ``ai`` section of ``config.yaml``.  The API key is read
    from ``api_key_env`` and never written to the run files.
    """
    base_url = str(config.get("base_url") or "").strip().rstrip("/")
    model = str(config.get("model") or "").strip()
    key_env = str(config.get("api_key_env") or "OPENAI_API_KEY").strip()
    api_key = os.environ.get(key_env, "").strip()
    if not base_url:
        raise ValueError("ai.base_url is required when ai.backend=api")
    if not model:
        raise ValueError("ai.model is required when ai.backend=api")
    if not api_key:
        raise ValueError(f"API key environment variable {key_env!r} is not set")

    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    # Some compatible providers do not implement response_format.  It is
    # opt-in so the generic endpoint remains broadly usable.
    if bool(config.get("json_mode", False)):
        payload["response_format"] = {"type": "json_object"}

    request = Request(
        f"{base_url}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec B310 - configured endpoint
            raw = response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"API request failed with HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"API request failed: {exc.reason}") from exc

    envelope = json.loads(raw)
    choices = envelope.get("choices") if isinstance(envelope, dict) else None
    if not isinstance(choices, list) or not choices:
        raise ValueError("API response did not contain choices[0]")
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first, dict) else {}
    text = _text_content(message.get("content") if isinstance(message, dict) else "")
    if not text:
        text = _text_content(first.get("text")) if isinstance(first, dict) else ""
    if not text:
        raise ValueError("API response choices[0] did not contain message content")

    result = parse_json_message(text)
    result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    # This file is intentionally metadata-only; prompt and incident are saved
    # by the supervisor, while secrets and full API responses stay out of logs.
    stdout_file.write_text(
        json.dumps({"backend": "api", "provider": config.get("provider", "openai_compatible"), "model": model}, indent=2),
        encoding="utf-8",
    )
    return result
