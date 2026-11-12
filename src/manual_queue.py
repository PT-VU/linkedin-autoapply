"""Persistent queue for eligible jobs whose external application is manual.

The normal LinkedIn run no longer opens or submits external ATS forms.  It
still records the complete, AI-approved external opportunity in a small JSON
spool and in the user-facing workbook so the user can apply later.  The JSON
spool is the source of truth when Excel is locked by another application.
"""

from __future__ import annotations

from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Any, Dict

from .application_identity import jd_fingerprint, primary_job_identity

logger = logging.getLogger("linkedin_autoapply.manual_queue")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
QUEUE_JSON_PATH = PROJECT_ROOT / "data" / "manual_apply_queue.json"
DEFAULT_QUEUE_XLSX = PROJECT_ROOT / "data" / "manual_apply_queue.xlsx"
HEADERS = [
    "状态", "加入队列日期", "LinkedIn Job ID", "岗位名称", "公司名称", "地点",
    "AI评分", "筛选结论", "主要原因", "外部申请链接", "LinkedIn链接", "JD指纹",
    "来源关键词", "JD摘要",
]


def _queue_path(config: Dict[str, Any] | None = None) -> Path:
    configured = (config or {}).get("manual_apply_queue", {}).get("path", "")
    return Path(configured) if configured else DEFAULT_QUEUE_XLSX


def _load_records() -> list[dict[str, Any]]:
    if not QUEUE_JSON_PATH.exists():
        return []
    try:
        data = json.loads(QUEUE_JSON_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_records(records: list[dict[str, Any]]) -> None:
    QUEUE_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = QUEUE_JSON_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(QUEUE_JSON_PATH)


def _row(record: Dict[str, Any]) -> list[Any]:
    return [record.get(key, "") for key in (
        "status", "queued_at", "job_id", "title", "company", "location", "score",
        "decision", "reason", "external_apply_url", "linkedin_url", "jd_fingerprint",
        "keyword", "jd_summary",
    )]


def _write_workbook(path: Path, records: list[dict[str, Any]]) -> None:
    """Create/update the workbook without making queue persistence fatal."""
    try:
        from openpyxl import Workbook, load_workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.worksheet.datavalidation import DataValidation
    except Exception as exc:  # pragma: no cover - dependency is installed in runtime
        logger.warning("openpyxl unavailable; queue remains in %s: %s", QUEUE_JSON_PATH, exc)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        book = load_workbook(path) if path.exists() else Workbook()
        sheet = book["手动投递队列"] if "手动投递队列" in book.sheetnames else book.create_sheet("手动投递队列")
        if sheet.max_row == 1 and sheet.cell(1, 1).value is None:
            book.remove(book.active)
            sheet = book.create_sheet("手动投递队列")
        # Rebuild data rows from the JSON source to make retries/idempotency
        # deterministic.  The workbook is a user-facing projection, not the
        # deduplication store.
        sheet.delete_rows(1, sheet.max_row)
        sheet.merge_cells("A1:N1")
        sheet["A1"] = "手动投递队列"
        sheet.merge_cells("A2:N2")
        sheet["A2"] = "由 LinkedIn JD 筛选产生。外部自动投递未确认或无法完成的岗位会自动回退到这里。"
        for column, header in enumerate(HEADERS, 1):
            sheet.cell(4, column).value = header
        for row_index, record in enumerate(records, 5):
            for column, value in enumerate(_row(record), 1):
                sheet.cell(row_index, column).value = value
        sheet.freeze_panes = "A5"
        sheet.sheet_view.showGridLines = False
        status_validation = DataValidation(
            type="list", formula1='"待投递,已投递,跳过,需补充信息"', allow_blank=True,
        )
        sheet.add_data_validation(status_validation)
        status_validation.add("A5:A2000")
        sheet["A1"].font = Font(name="Arial", size=14, bold=True, color="1F2937")
        sheet["A2"].font = Font(name="Arial", size=10, italic=True, color="4B5563")
        for cell in sheet[4]:
            cell.fill = PatternFill("solid", fgColor="1F4E78")
            cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        widths = [14, 20, 16, 36, 26, 28, 10, 18, 48, 58, 58, 28, 24, 72]
        for index, width in enumerate(widths, 1):
            sheet.column_dimensions[chr(64 + index)].width = width
        for row_index in range(5, sheet.max_row + 1):
            sheet.cell(row_index, 2).number_format = "yyyy-mm-dd hh:mm"
            sheet.cell(row_index, 7).number_format = "0.0"
            for column in range(1, 15):
                sheet.cell(row_index, column).alignment = Alignment(vertical="top", wrap_text=False)
        book.save(path)
    except (OSError, PermissionError) as exc:
        logger.warning("Manual-queue workbook unavailable (%s); JSON queue is current: %s", path, exc)


def _reset_stale_easy_apply_status(
    existing: dict[str, Any],
    job: Dict[str, Any],
    reason: str,
    records: list[dict[str, Any]],
    config: Dict[str, Any] | None,
    write_workbook: bool,
) -> None:
    """Re-open a failed Easy Apply row unless the application ledger confirms it.

    Queue status is a user-facing projection.  ``applied_jobs.json`` remains
    authoritative, so a historical bulk status update cannot permanently hide
    a failed Easy Apply from the pending queue.
    """
    if existing.get("status") == "待投递":
        return
    from .storage import is_already_applied

    if is_already_applied(str(job.get("job_id", "")), job):
        return
    existing["status"] = "待投递"
    existing["reason"] = reason
    _save_records(records)
    if write_workbook:
        _write_workbook(_queue_path(config), records)
    logger.warning(
        "Reset stale Easy Apply queue status to 待投递: %s @ %s (%s)",
        existing.get("title", ""),
        existing.get("company", ""),
        existing.get("job_id", ""),
    )


def enqueue_manual_job(
    job: Dict[str, Any],
    score: float,
    reason: str,
    apply_url: str = "",
    keyword: str = "",
    config: Dict[str, Any] | None = None,
    *,
    decision: str = "manual_required",
    write_workbook: bool = True,
) -> bool:
    """Add a job that needs a person to the user-facing manual queue.

    The queue is intentionally workflow-neutral.  ``apply_url`` may be an
    external ATS URL or a LinkedIn Easy Apply detail URL.  Keeping both paths
    in one queue prevents a failed Easy Apply safety check from producing a
    misleading "human handoff" item that opens an empty/stale modal.
    """
    if config is not None and not (config.get("manual_apply_queue", {}).get("enabled", True)):
        return False
    identity = primary_job_identity(job)
    records = _load_records()
    for existing in records:
        if existing.get("identity_key") == identity:
            # A failed Easy Apply can be encountered again after an earlier
            # migration/blanket status update marked the queue row as
            # ``已投递``.  The application ledger is the source of truth for
            # confirmed submissions; if it has no record, make the failed
            # Easy Apply actionable again instead of silently hiding it.
            if decision == "easy_apply_manual_required":
                _reset_stale_easy_apply_status(existing, job, reason, records, config, write_workbook)
            return False
        if identity.startswith("linkedin:") and existing.get("job_id") == job.get("job_id"):
            if decision == "easy_apply_manual_required":
                _reset_stale_easy_apply_status(existing, job, reason, records, config, write_workbook)
            return False
    record = {
        "identity_key": identity,
        "status": "待投递",
        "queued_at": datetime.now().isoformat(timespec="minutes"),
        "job_id": str(job.get("job_id", "")),
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "score": float(score) if score is not None else "",
        "decision": decision,
        "reason": reason,
        "external_apply_url": apply_url or job.get("apply_url") or job.get("url") or job.get("link", ""),
        "linkedin_url": job.get("link", ""),
        "jd_fingerprint": jd_fingerprint(job),
        "keyword": keyword or job.get("source_keyword", ""),
        "jd_summary": " ".join(str(job.get("jd_text", "")).split())[:800],
    }
    records.append(record)
    _save_records(records)
    if write_workbook:
        _write_workbook(_queue_path(config), records)
    logger.info("Queued job for manual application (%s): %s @ %s", decision, record["title"], record["company"])
    return True


def enqueue_external_job(
    job: Dict[str, Any],
    score: float,
    reason: str,
    external_url: str = "",
    keyword: str = "",
    config: Dict[str, Any] | None = None,
) -> bool:
    """Backward-compatible wrapper for an eligible external application."""
    return enqueue_manual_job(
        job, score, reason, external_url, keyword, config,
        decision="eligible_external",
    )


def queue_records() -> list[dict[str, Any]]:
    return _load_records()
