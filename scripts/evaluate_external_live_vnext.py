"""Apply the experimental state contract to a real ATS probe artifact.

This is deliberately separate from both the production workflow and the
browser probe.  It converts a page-level smoke result into the state the
experimental controller would choose, without clicking any submit control.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.external_apply_experimental import FormObservation, handoff_decision


def evaluate(payload: dict) -> dict:
    evaluated = {}
    for name, result in (payload.get("results") or {}).items():
        after = result.get("after") or {}
        missing = tuple(after.get("missing_required") or [])
        uploads = result.get("fill", {}).get("uploads") or []
        upload_retained = any(int(item.get("file_count", 0) or 0) > 0 for item in uploads)
        # The probe has a real DOM page but does not create a human-visible
        # challenge.  A captcha iframe alone is intentionally not enough.
        observation = FormObservation(
            ready_to_submit=bool(result.get("ready_to_submit")),
            visible_tab_stable=not bool(result.get("error")),
            interactive_challenge=bool(after.get("challenge_text")),
            missing_required=missing,
            upload_retained=upload_retained or not uploads,
        )
        if not after.get("application_submitters"):
            state, reason = "unsupported", "no_application_form_or_submit_control"
            handoff = False
        else:
            handoff, reason = handoff_decision(observation)
            state = "challenge_handoff" if handoff else reason
        evaluated[name] = {
            "ready_to_submit": observation.ready_to_submit,
            "missing_required_count": len(missing),
            "upload_retained": observation.upload_retained,
            "captcha_frames": len(after.get("captcha_frames") or []),
            "visible_challenge_text": observation.interactive_challenge,
            "experimental_state": state,
            "handoff_allowed": handoff,
            "reason": reason,
            "final_submit_clicked": False,
        }
    return {
        "mode": "experimental_live_probe_evaluation",
        "source": payload.get("run", ""),
        "production_flow_changed": False,
        "final_submit_clicked": False,
        "results": evaluated,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("probe", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.probe.read_text(encoding="utf-8-sig"))
    result = evaluate(payload)
    output = args.output or ROOT / "tests" / "experimental_live_smoke_20261107.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
