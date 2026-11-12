"""Move legacy pending human-handoff items into the unified manual queue.

This is a one-time migration for the paused-external-automation workflow.
The old queue is retained as an audit trail, but no item remains pending there.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.manual_queue import enqueue_manual_job


ROOT = Path(__file__).resolve().parents[1]
HANDOFF = ROOT / "data" / "external_handoff_queue.json"
MANUAL_JSON = ROOT / "data" / "manual_apply_queue.json"


def main() -> int:
    if not HANDOFF.exists():
        print("No legacy handoff queue found")
        return 0
    records = json.loads(HANDOFF.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise SystemExit("Legacy handoff queue is not a JSON list")

    # The rows already present in the user-maintained manual list represent
    # applications the user has completed outside this run.  Preserve that
    # deduplication fact in the JSON source of truth as well as in Excel.
    if MANUAL_JSON.exists():
        existing_manual = json.loads(MANUAL_JSON.read_text(encoding="utf-8"))
        if isinstance(existing_manual, list):
            changed = False
            for row in existing_manual:
                if row.get("status") == "待投递":
                    row["status"] = "已投递"
                    changed = True
            if changed:
                tmp_manual = MANUAL_JSON.with_suffix(".tmp")
                tmp_manual.write_text(json.dumps(existing_manual, ensure_ascii=False, indent=2), encoding="utf-8")
                tmp_manual.replace(MANUAL_JSON)

    migrated = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for item in records:
        if item.get("state", "pending") != "pending":
            continue
        apply_url = str(item.get("apply_url") or "").strip()
        job = {
            "job_id": str(item.get("job_id") or ""),
            "title": item.get("title", "Unknown"),
            "company": item.get("company", "Unknown"),
            "location": item.get("location", ""),
            "link": apply_url,
            "apply_url": apply_url,
            "source_keyword": item.get("source_keyword", ""),
            "jd_text": item.get("jd_text", ""),
        }
        reason = f"legacy_human_handoff:{item.get('reason') or 'manual_intervention_required'}"
        queued = enqueue_manual_job(
            job,
            item.get("score") or 0,
            reason,
            apply_url,
            str(item.get("source_keyword") or ""),
            None,
            decision="legacy_human_handoff_migrated",
            write_workbook=False,
        )
        if queued:
            migrated += 1
        item["state"] = "migrated_manual"
        item["migrated_at"] = now
        item["migrated_to"] = "data/manual_apply_queue.json"

    temporary = HANDOFF.with_suffix(".tmp")
    temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(HANDOFF)
    pending = sum(1 for item in records if item.get("state", "pending") == "pending")
    print(f"Migrated {migrated} legacy handoff items; pending legacy items remaining: {pending}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
