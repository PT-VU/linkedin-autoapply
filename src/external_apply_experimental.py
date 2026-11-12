"""Opt-in external-application state machine used by regression tests.

This module is intentionally *not* imported by the production workflow.  It
encodes the safer design from the external-ATS research report so it can be
tested against historical failure cases before wiring anything into the live
submission path.

The module has no Playwright dependency and never performs a browser action.
It only turns observed page/evidence snapshots into durable, auditable
decisions.  A future production adapter can consume the same contract after
the test cases are proven against real pages.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional


class FlowState(str, Enum):
    DISCOVERED = "discovered"
    FORM_INCOMPLETE = "form_incomplete"
    READY_TO_SUBMIT = "ready_to_submit"
    SUBMITTING = "submitting"
    CHALLENGE_HANDOFF = "challenge_handoff"
    SUBMITTED_CONFIRMED = "submitted_confirmed"
    SUBMITTED_CONFIRMED_AFTER_HANDOFF = "submitted_confirmed_after_handoff"
    SUBMISSION_UNCONFIRMED = "submission_unconfirmed"
    PAGE_LOST = "page_lost"
    ANTI_ABUSE_STALL = "anti_abuse_stall"
    QUOTA_EXHAUSTED = "quota_exhausted"
    AUTH_REQUIRED = "auth_required"
    UNSUPPORTED = "unsupported"


_FINAL_STATES = {
    FlowState.SUBMITTED_CONFIRMED.value,
    FlowState.SUBMITTED_CONFIRMED_AFTER_HANDOFF.value,
    FlowState.SUBMISSION_UNCONFIRMED.value,
    FlowState.PAGE_LOST.value,
    FlowState.QUOTA_EXHAUSTED.value,
    FlowState.AUTH_REQUIRED.value,
    FlowState.UNSUPPORTED.value,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_bool(value: Any) -> bool:
    return bool(value)


@dataclass(frozen=True)
class FormObservation:
    """Snapshot of the page immediately before a possible handoff/click."""

    ready_to_submit: bool
    visible_tab_stable: bool
    interactive_challenge: bool
    missing_required: tuple[str, ...] = ()
    upload_retained: bool = True
    page_lost: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FormObservation":
        missing = value.get("missing_required") or value.get("missing_required_fields") or ()
        return cls(
            ready_to_submit=_as_bool(value.get("ready_to_submit")),
            visible_tab_stable=_as_bool(
                value.get("visible_tab_stable", value.get("tab_stable", False))
            ),
            interactive_challenge=_as_bool(
                value.get("interactive_challenge", value.get("visible_challenge", False))
            ),
            missing_required=tuple(str(item) for item in missing),
            upload_retained=_as_bool(value.get("upload_retained", True)),
            page_lost=_as_bool(value.get("page_lost", False)),
        )


@dataclass(frozen=True)
class SubmissionObservation:
    """Evidence captured after a final click or a human handoff."""

    receipt_visible: bool = False
    success_url: bool = False
    success_text: bool = False
    human_completed: bool = False
    application_request_seen: bool = False
    visible_challenge: bool = False
    page_lost: bool = False
    quota_exhausted: bool = False
    runtime_error: str = ""
    form_valid: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SubmissionObservation":
        return cls(
            receipt_visible=_as_bool(value.get("receipt_visible")),
            success_url=_as_bool(value.get("success_url")),
            success_text=_as_bool(value.get("success_text")),
            human_completed=_as_bool(value.get("human_completed")),
            application_request_seen=_as_bool(value.get("application_request_seen")),
            visible_challenge=_as_bool(value.get("visible_challenge")),
            page_lost=_as_bool(value.get("page_lost")),
            quota_exhausted=_as_bool(value.get("quota_exhausted")),
            runtime_error=str(value.get("runtime_error") or ""),
            form_valid=_as_bool(value.get("form_valid")),
        )


@dataclass(frozen=True)
class DocumentRoleObservation:
    """Evidence for one attachment field in an ATS form."""

    role: str
    field_name: str
    semantic_label: str
    required: bool
    source_file: str = ""
    retained_file_list: tuple[str, ...] = ()
    schema_observed: bool = True

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DocumentRoleObservation":
        retained = value.get("retained_file_list") or value.get("retained_files") or ()
        return cls(
            role=str(value.get("role") or value.get("semantic_role") or ""),
            field_name=str(value.get("field_name") or value.get("name") or ""),
            semantic_label=str(value.get("semantic_label") or value.get("label") or ""),
            required=_as_bool(value.get("required")),
            source_file=str(value.get("source_file") or ""),
            retained_file_list=tuple(str(item) for item in retained),
            schema_observed=_as_bool(value.get("schema_observed", True)),
        )


def validate_document_roles(
    roles: list[DocumentRoleObservation | Mapping[str, Any]],
    *,
    explicit_duplicate_allowed: bool = False,
) -> tuple[bool, list[str]]:
    """Validate attachment semantics before any real submission.

    A filename alone is never enough: the role must be known, the source file
    must be retained by the same field, and CV/Cover Letter cannot silently
    share a source file unless the employer schema explicitly allows it.
    Unobserved Diploma/Transcript roles are intentionally not assignable.
    """

    observations = [
        item if isinstance(item, DocumentRoleObservation) else DocumentRoleObservation.from_mapping(item)
        for item in roles
    ]
    errors: list[str] = []
    allowed = {"resume", "cover_letter", "diploma", "transcript_grade_list"}
    seen_roles: set[str] = set()
    sources: dict[str, str] = {}
    for item in observations:
        role = item.role.casefold().strip()
        if role not in allowed:
            errors.append(f"unsupported_document_role:{item.role}")
            continue
        if role in {"diploma", "transcript_grade_list"} and not item.schema_observed:
            errors.append(f"document_role_not_in_schema:{role}")
        if role in seen_roles:
            errors.append(f"duplicate_document_role:{role}")
        seen_roles.add(role)
        if item.required and not item.source_file:
            errors.append(f"missing_source_file:{role}")
        if item.source_file and item.source_file not in item.retained_file_list:
            errors.append(f"retained_file_mismatch:{role}")
        if item.source_file:
            sources[role] = Path(item.source_file).name.casefold()
    if not explicit_duplicate_allowed and sources.get("resume") and sources.get("cover_letter"):
        if sources["resume"] == sources["cover_letter"]:
            errors.append("resume_cover_letter_same_source_file")
    return not errors, errors


def validate_radio_answer_contract(
    groups: list[Mapping[str, Any]],
    answers_by_question_id: Mapping[str, Any],
) -> tuple[bool, list[str]]:
    """Validate boolean question answers by question ID, never by position."""

    errors: list[str] = []
    for group in groups:
        question_id = str(group.get("question_id") or "").strip()
        if not question_id:
            errors.append("radio_group_missing_question_id")
            continue
        if not _as_bool(group.get("required")):
            continue
        if question_id not in answers_by_question_id:
            errors.append(f"missing_radio_answer:{question_id}")
            continue
        answer = str(answers_by_question_id[question_id]).casefold()
        values = {str(item).casefold() for item in (group.get("values") or ("true", "false"))}
        if answer not in values:
            errors.append(f"invalid_radio_answer:{question_id}")
    return not errors, errors


def logical_required_greenhouse_controls(
    controls: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Drop hidden React Select required sentinels and keep logical controls."""

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for control in controls:
        control_id = str(control.get("control_id") or control.get("id") or "").strip()
        role = str(control.get("role") or "").casefold()
        hidden = _as_bool(control.get("hidden_required_sentinel")) or _as_bool(control.get("aria_hidden"))
        if hidden and role not in {"combobox", "listbox"}:
            continue
        if not control_id or control_id in seen:
            continue
        if _as_bool(control.get("aria_required")) or _as_bool(control.get("required")):
            result.append(dict(control))
            seen.add(control_id)
    return result


@dataclass
class RunRecord:
    job_key: str
    state: str = FlowState.DISCOVERED.value
    reason: str = ""
    submission_attempts: int = 0
    retry_budget: int = 1
    recovery_authorized: bool = False
    application_request_seen: bool = False
    receipt: bool = False
    page_lifecycle: list[str] = field(default_factory=list)
    last_action: dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=_now)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AtomicRunStateStore:
    """Small JSON store for the experimental flow.

    Writes use a temporary file in the same directory followed by ``os.replace``
    so a crash cannot leave a partially written run record.  The store is kept
    separate from the production flags and queues by the caller-provided path.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self, job_key: str) -> Optional[dict[str, Any]]:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        record = payload.get(job_key) if isinstance(payload, dict) else None
        return dict(record) if isinstance(record, dict) else None

    def save(self, record: Mapping[str, Any]) -> None:
        job_key = str(record.get("job_key") or "").strip()
        if not job_key:
            raise ValueError("run record requires job_key")
        payload: dict[str, Any] = {}
        if self.path.exists():
            try:
                current = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(current, dict):
                    payload.update(current)
            except (OSError, json.JSONDecodeError):
                payload = {}
        saved = dict(record)
        saved["updated_at"] = _now()
        payload[job_key] = saved
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            try:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            except OSError:
                pass


def handoff_decision(observation: FormObservation | Mapping[str, Any]) -> tuple[bool, str]:
    """Return whether a human challenge handoff is safe right now."""

    snap = observation if isinstance(observation, FormObservation) else FormObservation.from_mapping(observation)
    if snap.page_lost:
        return False, FlowState.PAGE_LOST.value
    if snap.missing_required or not snap.ready_to_submit or not snap.upload_retained:
        return False, FlowState.FORM_INCOMPLETE.value
    if not snap.visible_tab_stable:
        return False, "page_not_visible_or_unstable"
    if not snap.interactive_challenge:
        return False, FlowState.ANTI_ABUSE_STALL.value
    return True, FlowState.CHALLENGE_HANDOFF.value


def classify_submission(observation: SubmissionObservation | Mapping[str, Any]) -> tuple[str, str]:
    """Classify post-submit evidence without inferring success from a click."""

    evidence = (
        observation
        if isinstance(observation, SubmissionObservation)
        else SubmissionObservation.from_mapping(observation)
    )
    if evidence.receipt_visible or evidence.success_url or evidence.success_text:
        if evidence.human_completed:
            return FlowState.SUBMITTED_CONFIRMED_AFTER_HANDOFF.value, "employer_receipt_after_handoff"
        return FlowState.SUBMITTED_CONFIRMED.value, "employer_receipt"
    if evidence.page_lost:
        return FlowState.PAGE_LOST.value, "page_lost_after_submit"
    if evidence.quota_exhausted:
        return FlowState.QUOTA_EXHAUSTED.value, "codex_quota_exhausted"
    if evidence.application_request_seen:
        return FlowState.SUBMISSION_UNCONFIRMED.value, "application_request_without_receipt"
    if evidence.visible_challenge:
        return FlowState.CHALLENGE_HANDOFF.value, "visible_challenge"
    if evidence.runtime_error and not evidence.application_request_seen:
        return FlowState.SUBMISSION_UNCONFIRMED.value, "recoverable_runtime_error_without_request"
    return FlowState.SUBMISSION_UNCONFIRMED.value, "no_receipt"


class ExperimentalSubmissionFlow:
    """Replayable, test-only controller for the safer submission contract."""

    def __init__(self, store: AtomicRunStateStore, job_key: str, retry_budget: int = 1):
        self.store = store
        existing = store.load(job_key)
        if existing:
            self.record = RunRecord(
                job_key=job_key,
                state=str(existing.get("state", FlowState.DISCOVERED.value)),
                reason=str(existing.get("reason", "")),
                submission_attempts=int(existing.get("submission_attempts", 0) or 0),
                retry_budget=int(existing.get("retry_budget", retry_budget) or 0),
                recovery_authorized=bool(existing.get("recovery_authorized", False)),
                application_request_seen=bool(existing.get("application_request_seen", False)),
                receipt=bool(existing.get("receipt", False)),
                page_lifecycle=list(existing.get("page_lifecycle", []) or []),
                last_action=dict(existing.get("last_action", {}) or {}),
                updated_at=str(existing.get("updated_at", _now())),
            )
        else:
            self.record = RunRecord(job_key=job_key, retry_budget=max(0, int(retry_budget)))
            self._save()

    @property
    def state(self) -> str:
        return self.record.state

    def _save(self) -> None:
        self.store.save(self.record.as_dict())

    def observe_form(self, observation: FormObservation | Mapping[str, Any]) -> str:
        snap = observation if isinstance(observation, FormObservation) else FormObservation.from_mapping(observation)
        if snap.page_lost:
            state = FlowState.PAGE_LOST.value
            reason = "page_lost_before_submit"
        elif snap.missing_required or not snap.ready_to_submit or not snap.upload_retained:
            state = FlowState.FORM_INCOMPLETE.value
            reason = "required_fields_or_upload_missing"
        else:
            state = FlowState.READY_TO_SUBMIT.value
            reason = "required_fields_and_upload_ready"
        self.record.state = state
        self.record.reason = reason
        self.record.page_lifecycle.append(state)
        self._save()
        return state

    def request_handoff(self, observation: FormObservation | Mapping[str, Any]) -> tuple[bool, str]:
        allowed, reason = handoff_decision(observation)
        self.record.state = reason if not allowed else FlowState.CHALLENGE_HANDOFF.value
        self.record.reason = reason
        self.record.page_lifecycle.append(self.record.state)
        self._save()
        return allowed, reason

    def begin_submit(self) -> bool:
        """Authorize one click only when the persisted state says it is safe."""

        if self.record.state in _FINAL_STATES and not self.record.recovery_authorized:
            return False
        if self.record.state not in {
            FlowState.READY_TO_SUBMIT.value,
            FlowState.CHALLENGE_HANDOFF.value,
        } and not self.record.recovery_authorized:
            return False
        self.record.submission_attempts += 1
        self.record.state = FlowState.SUBMITTING.value
        self.record.reason = "instrumented_final_click_authorized"
        self.record.last_action = {"type": "submit_final", "attempt": self.record.submission_attempts}
        self.record.recovery_authorized = False
        self.record.page_lifecycle.append(FlowState.SUBMITTING.value)
        self._save()
        return True

    def observe_submission(self, observation: SubmissionObservation | Mapping[str, Any]) -> tuple[str, str]:
        state, reason = classify_submission(observation)
        evidence = observation if isinstance(observation, SubmissionObservation) else SubmissionObservation.from_mapping(observation)
        self.record.state = state
        self.record.reason = reason
        self.record.application_request_seen = self.record.application_request_seen or evidence.application_request_seen
        self.record.receipt = self.record.receipt or state in {
            FlowState.SUBMITTED_CONFIRMED.value,
            FlowState.SUBMITTED_CONFIRMED_AFTER_HANDOFF.value,
        }
        self.record.page_lifecycle.append(state)
        self._save()
        return state, reason

    def authorize_recovery(self, observation: SubmissionObservation | Mapping[str, Any]) -> tuple[bool, str]:
        """Allow at most one controlled retry when no employer request exists."""

        evidence = observation if isinstance(observation, SubmissionObservation) else SubmissionObservation.from_mapping(observation)
        if self.record.receipt:
            return False, "receipt_already_confirmed"
        if self.record.application_request_seen or evidence.application_request_seen:
            return False, "application_request_seen"
        if evidence.page_lost or evidence.quota_exhausted:
            return False, "safe_stop_after_page_or_quota_failure"
        if self.record.retry_budget <= 0:
            return False, "retry_budget_exhausted"
        if not evidence.runtime_error or not evidence.form_valid:
            return False, "no_explicit_correctable_runtime_error"
        self.record.retry_budget -= 1
        self.record.recovery_authorized = True
        self.record.state = FlowState.SUBMISSION_UNCONFIRMED.value
        self.record.reason = "controlled_recovery_authorized_without_request"
        self.record.page_lifecycle.append("recovery_authorized")
        self._save()
        return True, self.record.reason

