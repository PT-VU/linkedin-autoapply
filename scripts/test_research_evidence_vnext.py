"""Verify the experimental contracts against the latest research artifacts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.external_apply_experimental import (
    DocumentRoleObservation,
    logical_required_greenhouse_controls,
    validate_document_roles,
    validate_radio_answer_contract,
)


def main() -> int:
    role_probe = json.loads(
        (ROOT / "tests" / "external_document_role_probe_20261107.json").read_text(encoding="utf-8")
    )
    greenhouse_probe = json.loads(
        (ROOT / "tests" / "greenhouse_required_control_probe_20261107.json").read_text(encoding="utf-8")
    )
    role_items = []
    for role in ("resume", "cover_letter"):
        role_items.append({"role": role, **role_probe["document_role_contract"][role]})
    roles_ok, role_errors = validate_document_roles(role_items)
    radio_ok, radio_errors = validate_radio_answer_contract(role_probe["radio_groups"], {})
    logical = logical_required_greenhouse_controls(greenhouse_probe["logical_required_controls"])
    result = {
        "mode": "research_evidence_contract_test",
        "production_flow_changed": False,
        "document_roles": {"valid": roles_ok, "errors": role_errors},
        "radio_contract_without_answers": {"valid": radio_ok, "errors": radio_errors},
        "greenhouse_logical_required_controls": [item.get("control_id") for item in logical],
        "expected_safety": {
            "document_roles_valid": True,
            "radio_contract_without_answers_valid": False,
            "greenhouse_sentinel_deduplicated": len(logical) == 3,
        },
    }
    result["passed"] = (
        roles_ok
        and not radio_ok
        and len(logical) == 3
        and role_probe.get("cross_role_duplicate_detected") is False
    )
    output = ROOT / "tests" / "research_evidence_contract_test_20261107.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
