"""Persistent rate limiter for site-specific browser actions."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = PROJECT_ROOT / "data" / "linkedin" / "rate_limit_state.json"


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def linkedin_rate_limit(config: dict[str, Any], event: str, logger=None, context: str = "") -> None:
    """Throttle LinkedIn search and job-read events across short-lived runs."""
    settings = config.get("rate_limit", {}).get("linkedin", {}) if isinstance(config, dict) else {}
    if settings.get("enabled", True) is False:
        return

    max_reads = int(settings.get("max_job_reads_per_minute", 10) or 10)
    min_job_gap = float(settings.get("min_seconds_between_job_reads", 60.0 / max_reads))
    min_search_gap = float(settings.get("min_seconds_between_searches", 8.0))

    state = _load_state()
    now = time.time()
    sleep_for = 0.0

    if event == "search":
        last = float(state.get("last_search_at", 0) or 0)
        sleep_for = max(0.0, min_search_gap - (now - last))
    elif event == "job_read":
        last = float(state.get("last_job_read_at", 0) or 0)
        sleep_for = max(0.0, min_job_gap - (now - last))

        window = [float(ts) for ts in state.get("job_read_timestamps", []) if now - float(ts) < 60.0]
        if len(window) >= max_reads:
            sleep_for = max(sleep_for, 60.0 - (now - min(window)))
        state["job_read_timestamps"] = window
    else:
        return

    if sleep_for > 0:
        if logger:
            suffix = f" ({context})" if context else ""
            logger.info(f"LinkedIn rate limit: sleeping {sleep_for:.1f}s before {event}{suffix}")
        time.sleep(sleep_for)
        now = time.time()
        state = _load_state()

    if event == "search":
        state["last_search_at"] = now
    elif event == "job_read":
        window = [float(ts) for ts in state.get("job_read_timestamps", []) if now - float(ts) < 60.0]
        window.append(now)
        state["job_read_timestamps"] = window
        state["last_job_read_at"] = now

    _save_state(state)
