"""Discover models exposed by the locally installed Codex runtime.

Codex CLI does not currently expose a stable ``codex models`` subcommand.
The desktop/CLI runtime writes its current catalog to ``CODEX_HOME``. This
module reads that local catalog when available and falls back to the model in
the local Codex config or the generic inherited-default option. No network
request and no credential access are performed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _codex_home() -> Path:
    configured = str(os.environ.get("CODEX_HOME") or "").strip()
    return Path(configured) if configured else Path.home() / ".codex"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def discover_codex_models() -> list[str]:
    """Return visible model slugs known to the local Codex installation."""
    home = _codex_home()
    candidates = [
        home / "models_cache.codex_work_desktop.json",
        home / "models_cache.json",
    ]
    models: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        payload = _read_json(path)
        for item in payload.get("models", []) if isinstance(payload.get("models"), list) else []:
            if not isinstance(item, dict):
                continue
            slug = str(item.get("slug") or item.get("id") or "").strip()
            visibility = str(item.get("visibility") or "list").strip().lower()
            if not slug or visibility in {"hidden", "none"} or slug in seen:
                continue
            seen.add(slug)
            models.append(slug)

    # Include a configured model even if the cache is stale or unavailable.
    config_model = ""
    config_path = home / "config.toml"
    try:
        import tomllib

        config_model = str(tomllib.loads(config_path.read_text(encoding="utf-8")).get("model") or "").strip()
    except (OSError, ValueError, TypeError, ImportError):
        pass
    if config_model and config_model not in seen:
        models.insert(0, config_model)

    return models or [""]
