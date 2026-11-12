"""Legacy queue for applications that are filled and waiting for a person.

This queue is retained separately from ``failed_jobs_for_manual_apply`` for
future external-automation reuse and audit history.  The active
paused-external workflow routes incomplete Easy Apply forms to the unified
manual queue instead.  ``failed_jobs_for_manual_apply`` means the automated
form could not be completed (or the site requires login/registration), while
this queue means the form is ready and
only a human CAPTCHA, final Send click, or short confirmation is needed.
``source`` identifies the workflow so a queue processor never treats a
LinkedIn Easy Apply URL as an external ATS URL.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
QUEUE_FILE = PROJECT_ROOT / "data" / "external_handoff_queue.json"
logger = logging.getLogger("linkedin_autoapply.external_handoff_queue")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> List[Dict[str, Any]]:
    if not QUEUE_FILE.exists():
        return []
    try:
        data = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not read external handoff queue")
        return []


def _save(records: List[Dict[str, Any]]) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = QUEUE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(QUEUE_FILE)


def queue_key(job: Dict[str, Any], apply_url: str = "") -> str:
    """Return a stable, privacy-safe queue key."""
    job_id = str(job.get("job_id") or "").strip()
    if job_id:
        return f"job:{job_id}"
    url = str(apply_url or job.get("apply_url") or job.get("url") or "").strip()
    if url:
        return f"url:{url}"
    return "title:" + "|".join(str(job.get(k) or "").strip().casefold() for k in ("company", "title"))


def is_human_handoff_reason(reason: str) -> bool:
    """Classify a completed/mostly completed form that needs a person.

    Human intervention is reserved for a visible, interactive anti-bot
    challenge.  Missing answers, incomplete forms, a stuck Send control, and
    an unconfirmed submission without a visible challenge belong in the
    regular manual queue; sounding an alert for those states presents the
    user with a page that is not ready for the final human action.
    """
    text = str(reason or "").casefold().strip()
    # These codes are emitted only after the live page inspection has found a
    # visible widget/challenge (or the preflight itself saw one).
    visible_codes = {
        "captcha_required",
        "recaptcha_required",
        "hcaptcha_required",
        "turnstile_required",
        "visible_challenge",
        "captcha_token_missing_visible_challenge",
    }
    if text in visible_codes:
        return True
    # Preserve support for structured reasons that append evidence after the
    # primary code, e.g. ``captcha_required; ...``.  Do not match generic
    # words such as "captcha" or "challenge" because hidden integrations are
    # common on otherwise ordinary forms.
    head = text.split(";", 1)[0].strip()
    return head in visible_codes


def notify_handoff_required(*, source: str = "unknown") -> None:
    """Play a short Windows alert when a human handoff begins.

    The notification is best effort and never changes the browser state.  It
    can be disabled for CI with ``EXTERNAL_HANDOFF_SOUND=0``.
    """
    if os.environ.get("EXTERNAL_HANDOFF_SOUND", "1").casefold() in {"0", "false", "off", "no"}:
        return
    logger.info("Human handoff alert emitted (source=%s)", source)
    try:
        import winsound  # type: ignore
        for frequency in (880, 660, 880):
            winsound.Beep(frequency, 180)
            time.sleep(0.08)
        return
    except Exception:
        pass
    try:
        sys.stdout.write("\a\a\a")
        sys.stdout.flush()
    except Exception:
        logger.debug("Could not emit external handoff alert", exc_info=True)


def save_external_handoff(
    job: Dict[str, Any],
    reason: str,
    apply_url: str = "",
    score: Any = 0,
    *,
    source: str = "linkedin",
    run_path: str = "",
    notify: bool = True,
) -> Dict[str, Any]:
    """Upsert a pending human-intervention item.

    A live application flow emits its alert when the application tab is ready;
    persisting the queue item afterwards must not emit a second alert while
    leaving the browser on an unrelated or already timed-out page. Easy Apply
    callers without a live wait loop retain the default alert behaviour.
    """
    records = _load()
    key = queue_key(job, apply_url)
    existing: Optional[Dict[str, Any]] = next((r for r in records if r.get("queue_key") == key), None)
    try:
        score_value: Any = float(score or job.get("score") or job.get("ai_score") or 0)
    except (TypeError, ValueError):
        score_value = 0
    now = _now()
    if existing is None:
        existing = {
            "queue_key": key,
            "job_id": str(job.get("job_id") or ""),
            "title": job.get("title", "Unknown"),
            "company": job.get("company", "Unknown"),
            "location": job.get("location", ""),
            "score": score_value,
            "apply_url": apply_url or job.get("apply_url") or job.get("url", ""),
            "source": source,
            "state": "pending",
            "queued_at": now,
            "attempts": 0,
        }
        records.append(existing)
    else:
        existing["attempts"] = int(existing.get("attempts") or 0) + 1
    existing.update({
        "reason": str(reason or "human_intervention_required"),
        "updated_at": now,
        "state": "pending",
        "workflow": "easy_apply" if source == "linkedin_easy_apply" else "external_apply",
    })
    if run_path:
        existing["run_path"] = str(run_path)
    _save(records)
    if notify:
        notify_handoff_required(source="queue_enqueue")
    return dict(existing)


def get_pending_handoffs() -> List[Dict[str, Any]]:
    return sorted(
        [r for r in _load() if r.get("state", "pending") == "pending"],
        key=lambda r: (-(float(r.get("score") or 0)), str(r.get("queued_at") or "")),
    )


def mark_handoff_submitted(job_or_key: Any) -> bool:
    key = str((job_or_key.get("queue_key") or queue_key(job_or_key)) if isinstance(job_or_key, dict) else job_or_key)
    records = _load()
    changed = False
    for item in records:
        if item.get("queue_key") == key or item.get("job_id") == key:
            item["state"] = "submitted"
            item["submitted_at"] = _now()
            changed = True
    if changed:
        _save([r for r in records if r.get("state") != "submitted"])
    return changed


def remove_handoff(job_or_key: Any) -> bool:
    key = str((job_or_key.get("queue_key") or queue_key(job_or_key)) if isinstance(job_or_key, dict) else job_or_key)
    records = _load()
    remaining = [r for r in records if r.get("queue_key") != key and r.get("job_id") != key]
    if len(remaining) == len(records):
        return False
    _save(remaining)
    return True

