"""Cover-letter bridge for the update2 external application agent.

The production external agent cannot safely invent a letter or select an
arbitrary recent file.  When an ATS marks a cover-letter file control as
required, this module sends a structured request to the dedicated Codex
session and waits for the exact, job-specific DOCX requested in the payload.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parent.parent
REQUEST_DIR = PROJECT_ROOT / "data" / "external_cover_letter_requests"
DEFAULT_OUTPUT_DIR = Path("cover_letters")
SESSION_ID = ""
SESSION_TITLE = "简历定制 (自动投递)"


def _safe_name(value: Any, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._-")
    return (text or fallback)[:60]


def _session_ref(config: Mapping[str, Any] | None) -> str:
    section = (config or {}).get("cover_letter") or {}
    return str(section.get("external_session_id") or SESSION_ID).strip()


def _output_dir(config: Mapping[str, Any] | None) -> Path:
    section = (config or {}).get("cover_letter") or {}
    configured = str(section.get("external_output_dir") or DEFAULT_OUTPUT_DIR).strip()
    output_dir = Path(configured)
    return output_dir if output_dir.is_absolute() else PROJECT_ROOT / output_dir


def request_external_cover_letter(
    job: Mapping[str, Any],
    jd_text: str,
    profile: Mapping[str, Any],
    resume_path: str,
    config: Mapping[str, Any] | None,
    logger,
) -> str:
    """Ask the dedicated session for a truthful DOCX and return its path.

    An empty string means the session did not produce the requested file in
    time; callers must route that external application to the manual queue.
    """
    title = str(job.get("title") or "Unknown position")
    company = str(job.get("company") or "Unknown company")
    job_id = str(job.get("job_id") or "no-id")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = (
        f"Cover_Letter_{_safe_name(company, 'Company')}_"
        f"{_safe_name(title, 'Position')}_{_safe_name(job_id, 'job')}_{stamp}.docx"
    )
    output_dir = _output_dir(config)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / filename
    section = (config or {}).get("cover_letter") or {}
    template = Path(str(section.get("template_path") or "").strip())
    if template and not template.is_absolute():
        template = PROJECT_ROOT / template
    if not _session_ref(config) and template.is_file():
        logger.info("  Using configured cover-letter template because no dedicated AI session is configured")
        return str(template)
    request_id = f"external_{_safe_name(job_id, 'job')}_{stamp}"
    request_path = REQUEST_DIR / f"{request_id}.json"
    payload = {
        "request_id": request_id,
        "job_id": job_id,
        "title": title,
        "company": company,
        "jd_text": str(jd_text or ""),
        "candidate_profile": dict(profile or {}),
        "resume_path": str(resume_path or ""),
        "output_path": str(output_path),
        "output_format": "docx",
        "truthfulness": "Use only the supplied CV and applicant_context facts. Ask the user rather than inventing missing experience.",
        "instructions": "Write a concise, role-specific English cover letter. Save the DOCX at output_path. Do not only reply in chat.",
        "created_at": datetime.now().isoformat(),
    }
    REQUEST_DIR.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    codex = shutil.which("codex.exe") or shutil.which("codex") or "codex"
    message = (
        f"处理外部自动投递求职信请求。请读取请求文件 {request_path}，用其中的完整 JD、CV 路径和候选人资料生成真实、定制的英文 DOCX。"
        f"必须把文件保存到请求里的 output_path：{output_path}；保存成功后再回复确认。不要只在对话中输出正文，不要使用其他岗位的求职信。"
    )
    logger.info("  Requesting external cover letter from session %s for %s @ %s", SESSION_TITLE, title, company)
    try:
        # `codex queue` does not accept `--model`; queued messages inherit the
        # model configured on the target session.  Passing the unsupported
        # option caused every external motivation-letter request to fail
        # before the dedicated session could even read the JD.
        completed = subprocess.run(
            [codex, "queue", "--thread", _session_ref(config), "--message", message],
            cwd=str(PROJECT_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("  Cover-letter session could not be queued: %s", type(exc).__name__)
        return ""
    if completed.returncode != 0:
        logger.warning("  Cover-letter session queue failed: %s", (completed.stdout or "")[-500:])
        return ""
    logger.info(
        "  Cover-letter request queued to session %s; Codex queue inherits that session's configured model",
        _session_ref(config),
    )

    timeout_s = max(30, int(section.get("external_timeout_seconds", 300)))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if output_path.exists() and output_path.stat().st_size > 1024:
                # A DOCX is a ZIP container.  Checking the signature prevents
                # a chat transcript or HTML error page from being uploaded.
                with output_path.open("rb") as handle:
                    if handle.read(2) == b"PK":
                        logger.info("  External cover letter ready: %s", output_path)
                        return str(output_path)
        except OSError:
            pass
        time.sleep(2)
    logger.warning("  External cover-letter session timed out; expected file was not created: %s", output_path)
    return ""

