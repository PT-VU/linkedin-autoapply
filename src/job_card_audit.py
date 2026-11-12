"""Auditable ledger for every LinkedIn result card seen by the workflow."""
from __future__ import annotations

from datetime import datetime
import json
import logging
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

AUDIT_PATH = Path(__file__).resolve().parent.parent / "data" / "job_card_audit.xlsx"
FALLBACK_PATH = Path(__file__).resolve().parent.parent / "data" / "linkedin" / "job_card_audit_fallback.jsonl"
SHEET = "岗位卡片审计"
HEADERS = ["岗位名称", "公司名称", "检索时间", "评分", "是否投递", "投递或不投递的主要原因", "LinkedIn 链接"]
logger = logging.getLogger(__name__)


def _sheet():
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    book = load_workbook(AUDIT_PATH) if AUDIT_PATH.exists() else Workbook()
    if SHEET not in book.sheetnames:
        if len(book.sheetnames) == 1 and book.active.max_row == 1 and book.active.cell(1, 1).value is None:
            ws = book.active
            ws.title = SHEET
        else:
            ws = book.create_sheet(SHEET)
    else:
        ws = book[SHEET]
    if ws.cell(1, 1).value is None:
        # A fresh openpyxl worksheet already has an empty row 1.  Writing
        # with append() would leave it blank and make every audit row appear
        # one row lower than intended.
        for index, header in enumerate(HEADERS, start=1):
            ws.cell(1, index).value = header
        for cell in ws[1]:
            cell.fill = PatternFill('solid', fgColor='1F4E78')
            cell.font = Font(name='Aptos', bold=True, color='FFFFFF')
            cell.alignment = Alignment(horizontal='center')
        ws.freeze_panes = 'A2'
        for column, width in {'A': 42, 'B': 28, 'C': 20, 'D': 10, 'E': 14, 'F': 62, 'G': 68}.items():
            ws.column_dimensions[column].width = width
    return book, ws


def _row(ws, job_id, title, company):
    marker = str(job_id or '').strip()
    for row in range(2, ws.max_row + 1):
        if marker and str(ws.cell(row, 8).value or '') == marker:
            return row
        if ws.cell(row, 1).value == title and ws.cell(row, 2).value == company:
            return row
    return None


def _save_or_fallback(book, event):
    """Persist the audit without allowing a locked desktop workbook to stop applying.

    Excel/LibreOffice commonly holds an ``.xlsx`` open with an exclusive lock.
    The audit is useful diagnostics, but it is not a prerequisite for a job
    application.  Keep the normal desktop workbook as the primary destination
    and append the exact event to a JSONL spool when it is temporarily locked.
    """
    try:
        book.save(AUDIT_PATH)
        return True
    except (PermissionError, OSError) as exc:
        try:
            FALLBACK_PATH.parent.mkdir(parents=True, exist_ok=True)
            with FALLBACK_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"timestamp": datetime.now().isoformat(), **event}, ensure_ascii=False) + "\n")
        except Exception as fallback_exc:
            logger.warning("Job-card audit workbook is locked and fallback spool failed: %s", fallback_exc)
        logger.warning("Job-card audit workbook unavailable (%s); event spooled to %s", exc, FALLBACK_PATH)
        return False


def record_card(job, reason='Collected; awaiting application method'):
    book, ws = _sheet()
    title, company, job_id = job.get('title', ''), job.get('company', ''), str(job.get('job_id', ''))
    row = _row(ws, job_id, title, company)
    if row is None:
        ws.append([title, company, datetime.now().strftime('%Y-%m-%d %H:%M'), None, '未决定', reason, job.get('link', ''), job_id])
        row = ws.max_row
        ws.column_dimensions['H'].hidden = True
    _save_or_fallback(book, {
        "operation": "record_card",
        "job_id": str(job.get('job_id', '')),
        "title": title,
        "company": company,
        "reason": reason,
        "link": job.get('link', ''),
    })


def update_card(job, score=None, applied=None, reason=None, link=None):
    book, ws = _sheet()
    row = _row(ws, str(job.get('job_id', '')), job.get('title', ''), job.get('company', ''))
    if row is None:
        ws.append([job.get('title', ''), job.get('company', ''), datetime.now().strftime('%Y-%m-%d %H:%M'), None, '未决定', '', link or job.get('link', ''), str(job.get('job_id', ''))])
        row = ws.max_row
        ws.column_dimensions['H'].hidden = True
    if score is not None:
        ws.cell(row, 4).value = float(score)
    if applied is not None:
        ws.cell(row, 5).value = '是' if applied else '否'
    if reason:
        ws.cell(row, 6).value = reason
    if link:
        ws.cell(row, 7).value = link
    _save_or_fallback(book, {
        "operation": "update_card",
        "job_id": str(job.get('job_id', '')),
        "title": job.get('title', ''),
        "company": job.get('company', ''),
        "score": score,
        "applied": applied,
        "reason": reason or '',
        "link": link or job.get('link', ''),
    })
