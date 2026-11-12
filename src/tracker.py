"""
投递跟踪模块 — 维护 applied_jobs.md 和 applied_jobs.json 双格式记录。
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from .storage import load_json, save_json

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MD_TRACKER_PATH = PROJECT_ROOT / "data" / "applied_jobs.md"
SUCCESS_TRACKER_PATH = PROJECT_ROOT / "data" / "successful_applications.xlsx"
SUCCESS_TRACKER_SHEET = "成功投递记录"
VALID_COVER_LETTER_STATUSES = {"yes", "no", "not applicable"}


def record_successful_application(
    applied_at: datetime,
    title: str,
    company: str,
    match_score: float,
    cover_letter_status: str,
    apply_method: str,
    job_id: str = "",
) -> None:
    """Append one confirmed submission to the user-facing Excel ledger."""
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    status = str(cover_letter_status or "not applicable").strip().lower()
    if status not in VALID_COVER_LETTER_STATUSES:
        raise ValueError(f"Unsupported cover letter status: {cover_letter_status}")
    method = {"easy_apply": "easy", "easy": "easy", "external_apply": "external", "external": "external"}.get(str(apply_method).strip().lower())
    if method is None:
        raise ValueError(f"Unsupported application method: {apply_method}")

    SUCCESS_TRACKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SUCCESS_TRACKER_PATH.exists():
        workbook = load_workbook(SUCCESS_TRACKER_PATH)
    else:
        workbook = Workbook()
        workbook.remove(workbook.active)

    worksheet = workbook[SUCCESS_TRACKER_SHEET] if SUCCESS_TRACKER_SHEET in workbook.sheetnames else workbook.create_sheet(SUCCESS_TRACKER_SHEET)
    if all(worksheet.cell(1, column).value is None for column in range(1, 6)):
        headers = ["投递日期", "岗位名称", "公司名称", "评估分数", "是否定制Cover Letter", "投递方式"]
        for column, value in enumerate(headers, 1):
            worksheet.cell(1, column).value = value
        for cell in worksheet[1]:
            cell.fill = PatternFill("solid", fgColor="1F4E78")
            cell.font = Font(name="Aptos", bold=True, color="FFFFFF")
            cell.alignment = Alignment(horizontal="center", vertical="center")
        worksheet.freeze_panes = "A2"
        for column, width in {"A": 14, "B": 34, "C": 26, "D": 12, "E": 28, "F": 16}.items():
            worksheet.column_dimensions[column].width = width

    if worksheet.cell(1, 6).value is None:
        from copy import copy
        worksheet.cell(1, 6).value = "投递方式"
        worksheet.cell(1, 6)._style = copy(worksheet.cell(1, 5)._style)
        worksheet.column_dimensions["F"].width = 16

    # New records carry the stable LinkedIn card ID.  Keep the old six-column
    # layout intact for legacy callers/tests that do not provide one.
    if job_id and worksheet.cell(1, 7).value is None:
        from copy import copy
        worksheet.cell(1, 7).value = "LinkedIn Job ID"
        worksheet.cell(1, 7)._style = copy(worksheet.cell(1, 6)._style)
        worksheet.column_dimensions["G"].width = 18

    row_values = [applied_at.date(), title, company, float(match_score) if match_score is not None else None, status, method]
    if worksheet.cell(1, 7).value is not None:
        row_values.append(job_id or "")
    worksheet.append(row_values)
    row = worksheet.max_row
    worksheet.cell(row, 1).number_format = "yyyy-mm-dd"
    worksheet.cell(row, 4).number_format = "0.0"
    workbook.save(SUCCESS_TRACKER_PATH)


def update_md_tracker() -> None:
    """根据 applied_jobs.json 重新生成 applied_jobs.md。"""
    records = load_json("applied_jobs.json")

    # 统计
    total = len(records)
    easy_apply_count = sum(1 for r in records if r.get("apply_method") == "easy_apply")
    external_count = sum(1 for r in records if r.get("apply_method") == "external")
    high_match = sum(1 for r in records if isinstance(r.get("match_score"), (int, float)) and r["match_score"] >= 8)
    mid_match = sum(1 for r in records if isinstance(r.get("match_score"), (int, float)) and 7 <= r["match_score"] < 8)

    lines = [
        "# 📋 已投递岗位跟踪表",
        "",
        f"> 最后更新：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "> 自动生成，请勿手动编辑",
        "",
        "---",
        "",
        "## 统计概览",
        "",
        "| 指标 | 数量 |",
        "|------|------|",
        f"| 总扫描岗位数 | — |",
        f"| 总投递数 | {total} |",
        f"| Easy Apply 投递 | {easy_apply_count} |",
        f"| 外部网站投递 | {external_count} |",
        f"| 匹配度 ≥ 8/10 | {high_match} |",
        f"| 匹配度 7-7.9/10 | {mid_match} |",
        "",
        "---",
        "",
        "## 投递记录",
        "",
        "| # | 投递日期 | 公司 | 岗位名称 | 匹配度 | 投递方式 | 薪资待遇 | 工作地点 | 主要职责 | 状态 |",
        "|---|----------|------|----------|--------|----------|----------|----------|----------|------|",
    ]

    if not records:
        lines.append("| — | — | — | — | — | — | — | — | — | — |")

    def _sanitize(text: str, max_len: int = 100) -> str:
        """清理文本以安全放入 Markdown 表格：
        - 去除换行符
        - 去除管道符
        - 截断过长文本
        """
        if not text:
            return "—"
        # 替换换行和管道符
        text = text.replace("\n", " ").replace("\r", " ").replace("|", "/")
        # 合并多余空格
        import re
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > max_len:
            text = text[:max_len-3] + "..."
        return text

    for i, r in enumerate(records, 1):
        applied_at = _sanitize(r.get("applied_at", "")[:10], 15)
        company = _sanitize(r.get("company", "—"), 30)
        title = _sanitize(r.get("title", "—"), 50)
        score = r.get("match_score", 0)
        score_str = f"{score:.1f}/10" if isinstance(score, (int, float)) else "未评分"
        apply_method = _sanitize(r.get("apply_method", "—"), 15)
        salary = _sanitize(r.get("salary", ""), 25)
        location = _sanitize(r.get("location", ""), 30)
        responsibilities = _sanitize(r.get("responsibilities_summary", "—"), 80)
        status = _sanitize(r.get("status", "Submitted"), 20)

        lines.append(
            f"| {i} | {applied_at} | {company} | {title} | {score_str} | "
            f"{apply_method} | {salary} | {location} | {responsibilities} | {status} |"
        )

    lines.extend([
        "",
        "---",
        "",
        "> 详细投递记录同时保存在 `data/applied_jobs.json` 中供程序读取。",
    ])

    MD_TRACKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(MD_TRACKER_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def log_application(
    job_id: str,
    company: str,
    title: str,
    match_score: float,
    apply_method: str,
    jd_summary: str = "",
    salary: str = "",
    location: str = "",
    responsibilities_summary: str = "",
    cover_letter_path: str = "",
    cover_letter_status: str = "not applicable",
    status: str = "Submitted",
    jd_text: str = "",
    link: str = "",
) -> None:
    """记录一笔投递，同时更新 JSON 和 MD。

    Args:
        job_id: LinkedIn 岗位 ID
        company: 公司名称
        title: 岗位名称
        match_score: 匹配度分数
        apply_method: easy_apply / external
        jd_summary: JD 摘要
        salary: 薪资信息
        location: 工作地点
        responsibilities_summary: 主要职责摘要
        cover_letter_path: Cover Letter 文件路径
        status: 投递状态
    """
    record: Dict[str, Any] = {
        "job_id": job_id,
        "company": company,
        "title": title,
        "match_score": match_score,
        "apply_method": apply_method,
        "jd_summary": jd_summary,
        "salary": salary,
        "location": location,
        "responsibilities_summary": responsibilities_summary,
        "cover_letter_path": cover_letter_path,
        "cover_letter_status": cover_letter_status,
        "status": status,
        "applied_at": datetime.now().isoformat(),
    }
    # Preserve the stable identity used by the collector.  The numeric
    # LinkedIn job-card ID remains primary; the JD fingerprint is useful when
    # a card was collected before LinkedIn exposed its numeric ID.
    from .application_identity import jd_fingerprint, primary_job_identity
    identity_job = {"job_id": job_id, "company": company, "title": title, "jd_text": jd_text, "link": link}
    record["identity_key"] = primary_job_identity(identity_job)
    record["jd_fingerprint"] = jd_fingerprint(identity_job)
    record["link"] = link

    from .storage import record_applied
    record_applied(record)
    update_md_tracker()
    if status.strip().lower() == "submitted":
        record_successful_application(
            applied_at=datetime.fromisoformat(record["applied_at"]),
            title=title,
            company=company,
            match_score=match_score,
            cover_letter_status=cover_letter_status,
            apply_method=apply_method,
            job_id=job_id,
        )
