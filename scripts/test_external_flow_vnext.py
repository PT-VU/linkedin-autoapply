"""Run the opt-in external-flow experiment without touching production flow.

Usage (from F:\\LinkedIn-AutoApply):
    python scripts\\test_external_flow_vnext.py

The script replays known failure shapes and reads the existing page-level ATS
probe artifacts.  It never opens a browser, sends an application, or mutates
the production queues/flags.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.external_apply_experimental import (
    AtomicRunStateStore,
    ExperimentalSubmissionFlow,
    FormObservation,
    SubmissionObservation,
    handoff_decision,
)


OUTPUT = ROOT / "tests" / "experimental_flow_smoke_20261106.json"


def legacy_handoff(case: dict) -> bool:
    """Approximate the old broad trigger used in historical failures."""

    return bool(case.get("needs_user") or case.get("visible_challenge") or case.get("captcha_token_missing"))


def replay_failure_cases() -> list[dict]:
    cases = [
        {
            "name": "incomplete_form_but_challenge_present",
            "needs_user": True,
            "visible_challenge": True,
            "captcha_token_missing": False,
            "observation": FormObservation(False, True, True, ("School", "Degree")),
            "expected_handoff": False,
        },
        {
            "name": "token_missing_without_visible_challenge",
            "needs_user": True,
            "visible_challenge": False,
            "captcha_token_missing": True,
            "observation": FormObservation(True, True, False),
            "expected_handoff": False,
        },
        {
            "name": "blank_page_after_challenge",
            "needs_user": True,
            "visible_challenge": True,
            "captcha_token_missing": False,
            "observation": FormObservation(True, True, True, page_lost=True),
            "expected_handoff": False,
        },
        {
            "name": "ready_visible_interactive_challenge",
            "needs_user": True,
            "visible_challenge": True,
            "captcha_token_missing": False,
            "observation": FormObservation(True, True, True),
            "expected_handoff": True,
        },
    ]
    results = []
    for case in cases:
        allowed, reason = handoff_decision(case["observation"])
        results.append(
            {
                "name": case["name"],
                "legacy_handoff": legacy_handoff(case),
                "experimental_handoff": allowed,
                "experimental_reason": reason,
                "expected_handoff": case["expected_handoff"],
                "passed": allowed == case["expected_handoff"],
            }
        )
    return results


def replay_receipt_and_retry_cases() -> list[dict]:
    store = AtomicRunStateStore(ROOT / "tests" / ".experimental_run_state.json")
    results = []
    try:
        flow = ExperimentalSubmissionFlow(store, "smoke-human-receipt")
        flow.observe_form({"ready_to_submit": True})
        flow.request_handoff({
            "ready_to_submit": True,
            "visible_tab_stable": True,
            "interactive_challenge": True,
        })
        state, _ = flow.observe_submission(
            SubmissionObservation(receipt_visible=True, human_completed=True)
        )
        results.append({"case": "human_receipt_stops", "state": state, "second_click": flow.begin_submit()})

        flow = ExperimentalSubmissionFlow(store, "smoke-request-no-receipt")
        flow.observe_form({"ready_to_submit": True})
        flow.begin_submit()
        flow.observe_submission(SubmissionObservation(application_request_seen=True, form_valid=True))
        allowed, reason = flow.authorize_recovery(
            SubmissionObservation(application_request_seen=True, form_valid=True, runtime_error="busy")
        )
        results.append({"case": "same_site_request_no_retry", "recovery_allowed": allowed, "reason": reason})

        flow = ExperimentalSubmissionFlow(store, "smoke-runtime-recovery")
        flow.observe_form({"ready_to_submit": True})
        flow.begin_submit()
        flow.observe_submission(SubmissionObservation(form_valid=True, runtime_error="TypeError: no access"))
        allowed, reason = flow.authorize_recovery(
            SubmissionObservation(form_valid=True, runtime_error="TypeError: no access")
        )
        results.append({"case": "no_request_one_recovery", "recovery_allowed": allowed, "reason": reason})
    finally:
        try:
            store.path.unlink()
        except FileNotFoundError:
            pass
    return results


def read_ats_probe_summary() -> list[dict]:
    summaries = []
    for name in ("recruitee", "greenhouse", "lever", "teamtailor"):
        path = ROOT / "tests" / f"ats_matrix_{name}_20261106.json"
        if not path.exists():
            summaries.append({"ats": name, "available": False})
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        result = (payload.get("results") or {}).get(name) or {}
        summaries.append(
            {
                "ats": name,
                "available": True,
                "ready_to_submit": bool(result.get("ready_to_submit")),
                "application_like_requests": len(result.get("application_like_requests") or []),
                "test": result.get("test", ""),
            }
        )
    return summaries


def main() -> int:
    handoff = replay_failure_cases()
    retry = replay_receipt_and_retry_cases()
    ats = read_ats_probe_summary()
    payload = {
        "mode": "experimental_only",
        "production_flow_changed": False,
        "handoff_cases": handoff,
        "retry_and_receipt_cases": retry,
        "ats_probe_summary": ats,
        "metrics": {
            "legacy_false_handoff_cases": sum(x["legacy_handoff"] and not x["expected_handoff"] for x in handoff),
            "experimental_false_handoff_cases": sum(x["experimental_handoff"] != x["expected_handoff"] for x in handoff),
            "handoff_cases_passed": sum(x["passed"] for x in handoff),
        },
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if all(item["passed"] for item in handoff) else 1


if __name__ == "__main__":
    raise SystemExit(main())
