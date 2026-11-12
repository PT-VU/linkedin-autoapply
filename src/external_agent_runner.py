"""Production bridge to the validated Edge/Codex external-apply agent.

The external agent itself remains an isolated CLI because it owns a separate
Edge CDP context and a per-application diagnostics directory.  This module is
the small, synchronous adapter used by the normal LinkedIn workflow: it
starts one agent for one eligible external job, mirrors its progress into the
normal logger, and returns a structured result so the caller can route every
non-confirmed outcome to the manual queue.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNNER = PROJECT_ROOT / "scripts" / "run_external_agent.py"
_RUN_DIR_RE = re.compile(r"^\[external-agent\] Run directory: (.+?)\s*$")


@dataclass
class ExternalAgentResult:
    """Normalized outcome returned to the LinkedIn page workflow."""

    success: bool = False
    status: str = "unconfirmed"
    reason: str = "external_agent_failed"
    failure_code: str = ""
    handoff_required: bool = False
    run_dir: str = ""
    return_code: int = 1
    output: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)


def _config_value(config: Mapping[str, Any] | None, key: str, default: Any) -> Any:
    section = (config or {}).get("external_agent") or {}
    value = section.get(key, default)
    return default if value is None else value


def _safe_queue_key(job: Mapping[str, Any]) -> str:
    job_id = str(job.get("job_id") or "").strip()
    if job_id:
        return f"linkedin_{job_id}"
    title = str(job.get("title") or "external_job").strip()
    return "linkedin_" + re.sub(r"[^A-Za-z0-9._-]+", "_", title)[:80]


def _read_result(run_dir: str) -> dict[str, Any]:
    if not run_dir:
        return {}
    path = Path(run_dir) / "result.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _run_once(
    job: Mapping[str, Any],
    external_url: str,
    score: float,
    config: Mapping[str, Any] | None,
    logger,
    cover_letter_path: str = "",
) -> ExternalAgentResult:
    """Run one update2 agent attempt for one external application.

    The default is to defer visible CAPTCHA handoff instead of blocking the
    whole keyword run.  The retained Edge page is still written to the
    external handoff queue by the CLI; the caller additionally puts the job in
    the user-facing manual queue when the result is not confirmed.
    """

    url = str(external_url or "").strip()
    if not url:
        return ExternalAgentResult(reason="external_apply_url_missing")
    if not RUNNER.exists():
        return ExternalAgentResult(reason="external_agent_runner_missing")

    timeout = max(60, int(_config_value(config, "timeout_seconds", 600)))
    handoff_timeout = max(30, int(_config_value(config, "handoff_timeout_seconds", 120)))
    defer_handoff = bool(_config_value(config, "defer_handoff", True))
    cmd = [
        sys.executable,
        str(RUNNER),
        "--url", url,
        "--title", str(job.get("title") or "Unknown"),
        "--company", str(job.get("company") or "Unknown"),
        "--job-id", str(job.get("job_id") or ""),
        "--score", str(float(score or 0)),
        "--queue-key", _safe_queue_key(job),
        "--timeout", str(timeout),
        "--handoff-timeout", str(handoff_timeout),
    ]
    if cover_letter_path:
        cmd.extend(["--cover-letter", str(cover_letter_path)])
    if defer_handoff:
        cmd.append("--defer-handoff")

    env = os.environ.copy()
    env["LINKEDIN_AUTOAPPLY_AI_MODE"] = "codex"
    lines: list[str] = []
    run_dir = ""
    logger.info("  External agent starting (Edge + configured Codex model): %s", url[:180])
    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        output_queue: queue.Queue[str | None] = queue.Queue()

        def _pump() -> None:
            try:
                for raw in process.stdout:
                    output_queue.put(raw)
            finally:
                output_queue.put(None)

        reader = threading.Thread(target=_pump, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout + 90
        stream_closed = False
        while time.monotonic() < deadline:
            try:
                raw = output_queue.get(timeout=0.2)
            except queue.Empty:
                if process.poll() is not None and stream_closed:
                    break
                continue
            if raw is None:
                stream_closed = True
                if process.poll() is not None:
                    break
                continue
            line = raw.rstrip("\r\n")
            lines.append(line)
            match = _RUN_DIR_RE.match(line)
            if match:
                run_dir = match.group(1).strip()
            # Keep the standard run observable without dumping personal form
            # values or the full Codex trace into the main log.
            if line:
                logger.info("  [external-agent] %s", line)
        if process.poll() is None:
            process.kill()
            lines.append("external agent process timed out")
        return_code = process.wait(timeout=10)
        reader.join(timeout=2)
    except OSError as exc:
        logger.exception("  External agent could not start")
        return ExternalAgentResult(reason=f"external_agent_start_error:{type(exc).__name__}")

    evidence = _read_result(run_dir)
    status = str(evidence.get("status") or "unconfirmed")
    success = status == "submitted"
    reason = str(evidence.get("reason") or "")
    if not reason and lines:
        reason = lines[-1][:500]
    if not reason:
        reason = f"external_agent_exit_{return_code}"
    result = ExternalAgentResult(
        success=success,
        status=status,
        reason=reason,
        failure_code=str(evidence.get("failure_code") or ""),
        handoff_required=bool(evidence.get("handoff_required")),
        run_dir=run_dir,
        return_code=return_code,
        output=lines[-80:],
        evidence=evidence,
    )
    if result.success:
        logger.info("  External agent confirmed employer receipt (%s)", evidence.get("receipt") or "receipt")
    else:
        logger.warning("  External agent not confirmed: %s", result.reason[:500])
    return result


def _cover_letter_required(result: ExternalAgentResult) -> bool:
    evidence = result.evidence or {}
    reason = str(result.reason or "").casefold()
    if "cover_letter_required" in reason or "cover letter" in reason or "motivation letter" in reason:
        return True
    for field in evidence.get("missing_required_fields") or []:
        text = " ".join(str(field.get(key) or "") for key in ("label", "id", "name", "selector")).casefold()
        if any(term in text for term in ("cover letter", "cover_letter", "coverletter", "motivation letter", "motivatiebrief")):
            return True
    return False


def run_external_agent(
    job: Mapping[str, Any],
    external_url: str,
    score: float,
    config: Mapping[str, Any] | None,
    logger,
) -> ExternalAgentResult:
    """Run update2 and, only when required, obtain a job-specific Cover Letter.

    The first pass deliberately does not guess a file role.  If the agent
    reports a required Cover Letter field before submission, the dedicated
    cover-letter session receives the full JD and writes a unique DOCX; the
    external agent is then restarted with that exact path.  Other failures are
    returned unchanged for manual-queue routing.
    """
    result = _run_once(job, external_url, score, config, logger)
    if result.success or not _cover_letter_required(result):
        return result
    logger.info("  External form requires a Cover Letter; requesting a job-specific DOCX")
    try:
        from .external_cover_letter import request_external_cover_letter
        from .config import get_resume_path

        cover_path = request_external_cover_letter(
            job,
            str(job.get("jd_text") or ""),
            (config or {}).get("profile") or {},
            str(get_resume_path(dict(config or {}))),
            config,
            logger,
        )
    except Exception as exc:
        logger.exception("  External cover-letter generation failed")
        cover_path = ""
    if not cover_path:
        result.reason = f"{result.reason}; external_cover_letter_unavailable"
        result.failure_code = result.failure_code or "cover_letter_required"
        return result
    logger.info("  Retrying external form with job-specific Cover Letter: %s", cover_path)
    return _run_once(job, external_url, score, config, logger, cover_letter_path=cover_path)

