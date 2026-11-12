"""Rolling keyword quality feedback for job discovery.

Only search-term discovery learns from feedback. Job evaluation itself remains
theory-driven and independent from interview/application outcomes.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_FILE = PROJECT_ROOT / "data" / "keyword_performance.json"


@contextmanager
def _state_lock(path: Path, timeout_s: float = 8.0):
    """Serialize updates from parallel platform workers."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    deadline = time.monotonic() + timeout_s
    descriptor = None
    while descriptor is None:
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 30:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Keyword state lock timed out: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "active_keywords": [], "events": [], "replacements": []}
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "active_keywords": [], "events": [], "replacements": []}


def _save(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _feedback_config(config: Dict[str, Any]) -> Dict[str, Any]:
    return config.get("search", {}).get("keyword_feedback", {}) or {}


def _expansion_config(config: Dict[str, Any]) -> Dict[str, Any]:
    return config.get("search", {}).get("keyword_expansion", {}) or {}


def get_active_keywords(config: Dict[str, Any], state_path: Path = DEFAULT_STATE_FILE) -> List[str]:
    configured = list(config.get("search", {}).get("keywords", []) or [])
    with _state_lock(state_path):
        state = _load(state_path)
        active = [str(item) for item in state.get("active_keywords", []) if str(item).strip()]
        if not active:
            state["active_keywords"] = configured
            _save(state_path, state)
            return configured
        return active


def _event_time(event: Dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(event.get("recorded_at", "")).replace("Z", "+00:00"))
    except ValueError:
        return None


def _rolling_events(state: Dict[str, Any], days: int, now: datetime) -> List[Dict[str, Any]]:
    cutoff = now - timedelta(days=days)
    return [
        event for event in state.get("events", [])
        if (_event_time(event) is not None and _event_time(event) >= cutoff)
    ]


def _candidate_family(keyword: str, families: Dict[str, Iterable[str]]) -> str | None:
    key = keyword.casefold()
    for family, members in families.items():
        if any(str(member).casefold() == key for member in members):
            return family
    return None


def _choose_replacement(
    retired: str,
    active: List[str],
    candidates: List[str],
    families: Dict[str, Iterable[str]],
    previously_used: set[str],
) -> str | None:
    unavailable = {item.casefold() for item in active} | previously_used
    family = _candidate_family(retired, families)
    ordered = list(candidates)
    if family:
        family_members = [str(item) for item in families.get(family, [])]
        ordered = family_members + [item for item in candidates if item not in family_members]
    for candidate in ordered:
        if candidate.casefold() not in unavailable:
            return candidate
    return None


def optimize_keywords(
    config: Dict[str, Any],
    state: Dict[str, Any],
    now: datetime | None = None,
) -> Dict[str, Any]:
    cfg = _feedback_config(config)
    if not cfg.get("enabled", True):
        return state
    now = now or _now()
    days = int(cfg.get("window_days", 7))
    min_jobs = int(cfg.get("minimum_jobs", 15))
    min_runs = int(cfg.get("minimum_runs", 3))
    zero_result_runs = int(cfg.get("replace_zero_result_after_runs", min_runs))
    min_rate = float(cfg.get("minimum_match_rate", 0.12))
    max_replacements = int(cfg.get("max_replacements_per_update", 1))
    events = _rolling_events(state, days, now)
    active = list(state.get("active_keywords", []))
    candidates = list(cfg.get("candidate_keywords", []))
    expansion = _expansion_config(config)
    if bool(expansion.get("enabled", False)):
        candidates.extend(expansion.get("candidate_keywords", []) or [])
    families = cfg.get("families", {}) or {}
    used = {
        str(item.get("replacement", "")).casefold()
        for item in state.get("replacements", [])
        if item.get("replacement")
    }

    aggregates: Dict[str, Dict[str, Any]] = {}
    for event in events:
        keyword = str(event.get("keyword", ""))
        bucket = aggregates.setdefault(
            keyword,
            {"runs": 0, "legacy_jobs": 0, "legacy_matched": 0, "jobs": set(), "matched": set()},
        )
        bucket["runs"] += 1
        job_ids = {str(value) for value in event.get("job_ids", []) if str(value)}
        matched_ids = {str(value) for value in event.get("matched_job_ids", []) if str(value)}
        if job_ids:
            bucket["jobs"].update(job_ids)
            bucket["matched"].update(matched_ids)
        else:
            bucket["legacy_jobs"] += int(event.get("eligible_jobs", 0))
            bucket["legacy_matched"] += int(event.get("matched_jobs", 0))

    replaced = 0
    for keyword in list(active):
        stats = aggregates.get(keyword, {})
        jobs = len(stats.get("jobs", set())) + int(stats.get("legacy_jobs", 0))
        runs = int(stats.get("runs", 0))
        matched = len(stats.get("matched", set())) + int(stats.get("legacy_matched", 0))
        rate = (matched / jobs) if jobs else 0.0
        enough_evidence = runs >= min_runs and jobs >= min_jobs
        repeated_zero_result = runs >= zero_result_runs and jobs == 0
        if not (enough_evidence or repeated_zero_result) or rate >= min_rate:
            continue
        replacement = _choose_replacement(keyword, active, candidates, families, used)
        if not replacement:
            continue
        active[active.index(keyword)] = replacement
        state.setdefault("replacements", []).append({
            "retired": keyword,
            "replacement": replacement,
            "recorded_at": now.isoformat(),
            "window_days": days,
            "runs": runs,
            "eligible_jobs": jobs,
            "matched_jobs": matched,
            "match_rate": round(rate, 4),
        })
        used.add(replacement.casefold())
        replaced += 1
        if replaced >= max_replacements:
            break
    state["active_keywords"] = active
    if bool(expansion.get("enabled", False)):
        target = max(0, int(expansion.get("target_count", len(active))))
        seen = {str(item).casefold() for item in active}
        for candidate in candidates:
            candidate = str(candidate).strip()
            if len(active) >= target:
                break
            if candidate and candidate.casefold() not in seen:
                active.append(candidate)
                seen.add(candidate.casefold())
        state["active_keywords"] = active
    state["events"] = events
    state["updated_at"] = now.isoformat()
    return state


def record_keyword_result(
    platform: str,
    keyword: str,
    jobs: List[Dict[str, Any]],
    scores: Dict[str, float],
    threshold: float,
    config: Dict[str, Any],
    state_path: Path = DEFAULT_STATE_FILE,
    now: datetime | None = None,
) -> Dict[str, Any]:
    now = now or _now()
    with _state_lock(state_path):
        state = _load(state_path)
        if not state.get("active_keywords"):
            state["active_keywords"] = list(config.get("search", {}).get("keywords", []))
        usable = [job for job in jobs if len(str(job.get("jd_text", "") or "")) >= 200]
        usable_ids = [str(job.get("job_id", "")) for job in usable if str(job.get("job_id", ""))]
        matched_ids = [job_id for job_id in usable_ids if float(scores.get(job_id, 0.0)) >= threshold]
        matched = len(matched_ids)
        state.setdefault("events", []).append({
            "recorded_at": now.isoformat(),
            "platform": platform,
            "keyword": keyword,
            "found_jobs": len(jobs),
            "eligible_jobs": len(usable),
            "matched_jobs": matched,
            "job_ids": usable_ids,
            "matched_job_ids": matched_ids,
            "match_rate": round(matched / len(usable), 4) if usable else None,
            "threshold": threshold,
        })
        state = optimize_keywords(config, state, now=now)
        _save(state_path, state)
        return state


def build_report(config: Dict[str, Any], state_path: Path = DEFAULT_STATE_FILE) -> str:
    state = _load(state_path)
    cfg = _feedback_config(config)
    events = _rolling_events(state, int(cfg.get("window_days", 7)), _now())
    lines = ["keyword | runs | usable jobs | matched | rate", "---|---:|---:|---:|---:"]
    for keyword in state.get("active_keywords", []):
        rows = [event for event in events if event.get("keyword") == keyword]
        job_ids = {
            str(job_id)
            for row in rows
            for job_id in row.get("job_ids", [])
            if str(job_id)
        }
        matched_ids = {
            str(job_id)
            for row in rows
            for job_id in row.get("matched_job_ids", [])
            if str(job_id)
        }
        legacy_rows = [row for row in rows if not row.get("job_ids")]
        jobs = len(job_ids) + sum(int(row.get("eligible_jobs", 0)) for row in legacy_rows)
        matched = len(matched_ids) + sum(int(row.get("matched_jobs", 0)) for row in legacy_rows)
        rate = matched / jobs if jobs else 0.0
        lines.append(f"{keyword} | {len(rows)} | {jobs} | {matched} | {rate:.1%}")
    return "\n".join(lines)


def main() -> int:
    from src.config import load_config

    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    if args.report:
        print(build_report(load_config()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
