#!/usr/bin/env python3
"""Process the saved external human-intervention queue without job search.

Each item is opened directly at its stored ATS URL.  Codex fills the form and
the normal Edge handoff waits for the person to complete CAPTCHA/Send.  Items
that receive an employer receipt are removed by ``run_external_agent.py``;
items that time out stay pending for the next batch.
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.external_handoff_queue import get_pending_handoffs  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Fill queued external applications and wait for human handoff")
    parser.add_argument("--limit", type=int, default=0, help="Maximum pending items to process; 0 means all")
    parser.add_argument("--handoff-timeout", type=int, default=120, help="Seconds to wait for human CAPTCHA/Send")
    parser.add_argument("--timeout", type=int, default=300, help="Maximum Codex time per queued item")
    args = parser.parse_args()

    items = get_pending_handoffs()
    if args.limit > 0:
        items = items[:args.limit]
    if not items:
        print("External handoff queue: 0 pending")
        return 0

    print(f"External handoff queue: {len(items)} pending; no external search will run.", flush=True)
    failures = 0
    runner = ROOT / "scripts" / "run_external_agent.py"
    for index, item in enumerate(items, 1):
        if str(item.get("workflow") or "").casefold() == "easy_apply":
            print(
                f"[{index}/{len(items)}] {item.get('title', '?')} @ {item.get('company', '?')} "
                "requires the LinkedIn Easy Apply human-intervention flow; skipped by external processor.",
                flush=True,
            )
            failures += 1
            continue
        url = str(item.get("apply_url") or "").strip()
        if not url:
            print(f"[{index}/{len(items)}] skipped: queue item has no apply_url", flush=True)
            failures += 1
            continue
        command = [
            sys.executable, str(runner),
            "--url", url,
            "--title", str(item.get("title") or "Queued external application"),
            "--company", str(item.get("company") or "Unknown"),
            "--job-id", str(item.get("job_id") or ""),
            "--score", str(item.get("score") or 0),
            "--queue-key", str(item.get("queue_key") or ""),
            "--timeout", str(args.timeout),
            "--handoff-timeout", str(args.handoff_timeout),
        ]
        print(f"[{index}/{len(items)}] {item.get('title', '?')} @ {item.get('company', '?')}", flush=True)
        result = subprocess.run(command, cwd=ROOT)
        if result.returncode != 0:
            failures += 1
            print(f"[{index}/{len(items)}] remains pending (exit {result.returncode})", flush=True)
    print(f"External handoff queue finished: {len(items) - failures} submitted, {failures} pending/failed.", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

