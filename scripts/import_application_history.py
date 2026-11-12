"""One-time import of the user's historical application workbook.

The source workbook contains application IDs, title and company but no
LinkedIn job-card IDs or complete JDs.  We retain that limitation explicitly:
legacy rows use a company/title fallback only, while all new workflow rows use
numeric job IDs or JD fingerprints.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.application_identity import legacy_company_title_key

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "application_history_baseline.json"


def _json_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def import_history(source: Path, output: Path) -> dict:
    workbook = load_workbook(source, read_only=False, data_only=True)
    sheet = workbook["申请进度"] if "申请进度" in workbook.sheetnames else workbook.active
    headers = [sheet.cell(7, col).value for col in range(1, sheet.max_column + 1)]
    index = {str(value).strip(): pos for pos, value in enumerate(headers, start=1) if value}
    title_col = index.get("岗位名称（缺失时为公司）", 3)
    company_col = index.get("公司名称", 4)
    app_id_col = index.get("独立 ID", 1)
    date_col = index.get("确认邮件收信日期", 2)
    status_col = index.get("拒信状态（0/1/other/out date）", 5)

    records = []
    seen = set()
    for row in range(8, sheet.max_row + 1):
        title = str(sheet.cell(row, title_col).value or "").strip()
        company = str(sheet.cell(row, company_col).value or "").strip()
        app_id = str(sheet.cell(row, app_id_col).value or "").strip()
        if not title and not company:
            continue
        key = (app_id, company.casefold(), title.casefold())
        if key in seen:
            continue
        seen.add(key)
        records.append({
            "application_id": app_id,
            "title": title,
            "company": company,
            "legacy_key": legacy_company_title_key(company, title),
            "legacy_history": True,
            "history_status": _json_value(sheet.cell(row, status_col).value),
            "confirmation_date": _json_value(sheet.cell(row, date_col).value),
            "source": str(source),
        })

    payload = {
        "version": 1,
        "imported_at": datetime.now().isoformat(),
        "source": str(source),
        "limitations": [
            "Historical rows do not contain LinkedIn job-card IDs.",
            "Historical deduplication therefore uses legacy company/title keys only.",
        ],
        "records": records,
        "job_ids": [],
        "legacy_keys": sorted({r["legacy_key"] for r in records if r.get("legacy_key")}),
        "jd_keys": [],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = import_history(args.source, args.output)
    print(f"Imported {len(payload['records'])} historical applications to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
