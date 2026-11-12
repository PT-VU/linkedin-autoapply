"""Build an auditable direct-URL retry list for the 2026-10-28 Easy Apply run.

The failed run routed each attempted Easy Apply job to the unified manual queue.
This script selects exactly those records by their stable LinkedIn job id and
writes direct job URLs for ``main.py --apply-urls-file``.  It is intentionally
time-bounded so older handoff/manual records cannot accidentally be retried.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QUEUE = ROOT / "data" / "manual_apply_queue.json"
URLS = ROOT / "data" / "easy_apply_retry_20261028.txt"
MANIFEST = ROOT / "data" / "easy_apply_retry_20261028.json"


def _queued_at(value: object) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return None


def main() -> int:
    records = json.loads(QUEUE.read_text(encoding="utf-8"))
    start = datetime(2026, 10, 28, 0, 55)
    end = datetime(2026, 10, 28, 0, 56)
    selected: list[dict] = []
    seen: set[str] = set()
    for record in records:
        if record.get("decision") != "legacy_human_handoff_migrated":
            continue
        if not str(record.get("reason", "")).endswith("easy_apply_ai_needs_user_human_intervention"):
            continue
        queued = _queued_at(record.get("queued_at"))
        job_id = str(record.get("job_id") or "").strip()
        if queued is None or not (start <= queued < end) or not job_id.isdigit() or job_id in seen:
            continue
        seen.add(job_id)
        selected.append(record)

    selected.sort(key=lambda item: (_queued_at(item.get("queued_at")) or start, str(item.get("job_id"))))
    if len(selected) != 44:
        raise SystemExit(f"Expected 44 Easy Apply records from 2026-10-28, found {len(selected)}")

    URLS.write_text(
        "".join(f"https://www.linkedin.com/jobs/view/{record['job_id']}/\n" for record in selected),
        encoding="utf-8",
    )
    manifest = [
        {
            "job_id": record["job_id"],
            "title": record.get("title", ""),
            "company": record.get("company", ""),
            "linkedin_url": record.get("linkedin_url", ""),
            "queued_at": record.get("queued_at", ""),
            "previous_reason": record.get("reason", ""),
        }
        for record in selected
    ]
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Built {len(selected)} retry URLs: {URLS}")
    print(f"Audit manifest: {MANIFEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
