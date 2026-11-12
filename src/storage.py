"""
数据持久化模块 — JSON 文件读写，记录已投递和跳过的岗位。
"""

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .application_identity import (
    jd_fingerprint,
    job_identity_keys,
    legacy_company_title_key,
    linkedin_job_id,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
HISTORY_INDEX_FILE = DATA_DIR / "application_history_baseline.json"
DECISION_CACHE_FILE = DATA_DIR / "job_decision_cache.json"


def _rubric_version() -> str:
    """Return a stable version for the AI scoring rubric.

    The cache intentionally ignores later JD edits for now, as requested by
    the user, but it must not silently reuse a decision made under a different
    rubric.  A missing rubric gets a stable fallback version.
    """
    rubric = DATA_DIR / "scoring_rubric.md"
    try:
        digest = hashlib.sha256(rubric.read_bytes()).hexdigest()
        return f"sha256:{digest[:16]}"
    except OSError:
        return "default"


def _ensure_data_dir() -> None:
    """确保 data 目录存在。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_json(filename: str) -> List[Dict[str, Any]]:
    """加载 JSON 文件，返回列表。

    Args:
        filename: 相对于 data/ 目录的文件名

    Returns:
        数据列表，文件不存在时返回空列表
    """
    _ensure_data_dir()
    filepath = DATA_DIR / filename
    if not filepath.exists():
        return []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, IOError):
        return []


def save_json(filename: str, data: List[Dict[str, Any]]) -> None:
    """保存数据到 JSON 文件。

    Args:
        filename: 相对于 data/ 目录的文件名
        data: 要保存的数据列表
    """
    _ensure_data_dir()
    filepath = DATA_DIR / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def record_external_decision(
    job: Dict[str, Any],
    score: Any,
    decision: str,
    reason: str,
    threshold: Any = None,
) -> None:
    """Cache the AI decision for an external LinkedIn posting.

    Easy Apply postings never enter this cache: they are handled before JD
    collection and are governed only by ``applied_jobs.json``.  External
    decisions are keyed by the numeric LinkedIn job ID so a later run can
    avoid reopening and re-reading the same JD.
    """
    job_id = linkedin_job_id(job.get("job_id", ""), job.get("link", ""))
    if not job_id:
        return
    try:
        score_value = float(score) if score is not None else None
    except (TypeError, ValueError):
        score_value = None
    records = load_json("job_decision_cache.json")
    record = {
        "job_id": job_id,
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "location": job.get("location", ""),
        "link": job.get("link", ""),
        "application_method": "external",
        "decision": str(decision or "").strip() or (
            "eligible_external" if score_value is not None and score_value >= float(threshold or 6.5)
            else "score_below_threshold"
        ),
        "score": score_value,
        "threshold": float(threshold) if threshold is not None else None,
        "reason": str(reason or "").strip(),
        "jd_fingerprint": jd_fingerprint(job),
        "rubric_version": _rubric_version(),
        "recorded_at": datetime.now().isoformat(),
        "source_keyword": job.get("source_keyword", ""),
    }
    replaced = False
    for index, previous in enumerate(records):
        previous_id = linkedin_job_id(previous.get("job_id", ""), previous.get("link", ""))
        if previous_id == job_id and previous.get("application_method") == "external":
            records[index] = record
            replaced = True
            break
    if not replaced:
        records.append(record)
    save_json("job_decision_cache.json", records)


def get_cached_external_decision(job_id: Any, job: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Return a reusable external decision for a LinkedIn job ID.

    JD changes are deliberately not checked yet.  Rubric changes invalidate
    the cache so a new scoring policy can take effect without manual cleanup.
    """
    candidate_id = linkedin_job_id(job_id, (job or {}).get("link", ""))
    if not candidate_id:
        return None
    current_rubric = _rubric_version()
    for record in reversed(load_json("job_decision_cache.json")):
        if record.get("application_method") != "external":
            continue
        record_id = linkedin_job_id(record.get("job_id", ""), record.get("link", ""))
        if record_id != candidate_id:
            continue
        if record.get("rubric_version") and record.get("rubric_version") != current_rubric:
            return None
        return record
    return None


def record_applied(job: Dict[str, Any]) -> None:
    """记录一个已投递的岗位。

    Args:
        job: 岗位信息字典，至少包含：
            - job_id: LinkedIn 岗位 ID
            - company: 公司名称
            - title: 岗位名称
            - match_score: 匹配分数
            - apply_method: easy_apply / external
            - applied_at: 投递时间 (ISO 格式)
    """
    records = load_json("applied_jobs.json")
    records.append(job)
    save_json("applied_jobs.json", records)


def record_skipped(job: Dict[str, Any], reason: str) -> None:
    """记录一个跳过的岗位。

    Args:
        job: 岗位信息字典
        reason: 跳过原因 (e.g. 'score_below_threshold', 'already_applied')
    """
    records = load_json("skipped_jobs.json")
    records.append({
        **job,
        "reason": reason,
        "skipped_at": datetime.now().isoformat(),
    })
    save_json("skipped_jobs.json", records)


def _load_history_index() -> Dict[str, Any]:
    if not HISTORY_INDEX_FILE.exists():
        return {"records": [], "job_ids": [], "legacy_keys": [], "jd_keys": []}
    try:
        with HISTORY_INDEX_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {"records": []}
    except (OSError, json.JSONDecodeError):
        return {"records": []}


def _record_matches_identity(record: Dict[str, Any], job: Dict[str, Any]) -> bool:
    """Match by numeric LinkedIn ID or JD fingerprint.

    Company/title is accepted only for rows imported from the historical
    workbook because those rows have no LinkedIn ID or full JD text.
    """
    job_id = linkedin_job_id(job.get("job_id", ""), job.get("link", ""))
    record_id = linkedin_job_id(record.get("job_id", ""), record.get("link", ""))
    if job_id and record_id and job_id == record_id:
        return True
    current_keys = set(job_identity_keys(job))
    if current_keys:
        if current_keys & set(job_identity_keys(record)):
            return True
        if record.get("identity_key") in current_keys:
            return True
    if record.get("legacy_history"):
        return legacy_company_title_key(job.get("company", ""), job.get("title", "")) == record.get("legacy_key")
    return False


def is_already_applied(job_id: str, job: Optional[Dict[str, Any]] = None) -> bool:
    """检查岗位是否已经投递过（按 job_id 精确匹配）。

    Args:
        job_id: LinkedIn 岗位 ID

    Returns:
        True 如果已投递
    """
    job = dict(job or {})
    job.setdefault("job_id", job_id)
    records = load_json("applied_jobs.json")
    for r in records:
        if r.get("job_id") == job_id or _record_matches_identity(r, job):
            return True
    history = _load_history_index()
    normalized_id = linkedin_job_id(job_id, job.get("link", ""))
    if normalized_id and normalized_id in set(history.get("job_ids", [])):
        return True
    for record in history.get("records", []):
        if _record_matches_identity(record, job):
            return True
    return False


def is_duplicate_company_title(company: str, title: str) -> bool:
    """检查是否已处理过相同公司+标题的岗位（防止 URL/job_id 不同导致重复）。
    覆盖已投递、人工队列、已跳过记录。

    Args:
        company: 公司名
        title: 岗位标题

    Returns:
        True 如果疑似重复
    """
    company_lower = (company or "").strip().lower()
    title_lower = (title or "").strip().lower()[:60]
    if not company_lower or not title_lower:
        return False
    for filename in ["applied_jobs.json", "failed_jobs_for_manual_apply.json", "skipped_jobs.json"]:
        records = load_json(filename)
        for r in records:
            if filename == "skipped_jobs.json" and r.get("reason") == "score_below_external_threshold":
                continue
            r_company = (r.get("company") or "").strip().lower()
            r_title = (r.get("title") or "").strip().lower()[:60]
            if r_company == company_lower and r_title[:40] == title_lower[:40]:
                return True
    return False


def _same_company_title(left: Dict[str, Any], company: str, title: str) -> bool:
    company_lower = (company or "").strip().lower()
    title_lower = (title or "").strip().lower()[:60]
    if not company_lower or not title_lower:
        return False

    left_company = (left.get("company") or "").strip().lower()
    left_title = (left.get("title") or "").strip().lower()[:60]
    return left_company == company_lower and left_title[:40] == title_lower[:40]


def is_already_skipped(job_id: str) -> bool:
    """检查岗位是否已经跳过。

    Args:
        job_id: LinkedIn 岗位 ID

    Returns:
        True 如果已跳过
    """
    records = load_json("skipped_jobs.json")
    for r in records:
        if r.get("job_id") == job_id:
            # Easy Apply is intentionally retriable until a confirmed
            # submission is written to applied_jobs.json.  Human-pending and
            # failed Easy Apply records are audit history, not a permanent
            # duplicate block.
            reason = str(r.get("reason", "")).casefold()
            if (
                "easy_apply" in reason
                or r.get("workflow") == "easy_apply"
                or r.get("application_method") == "easy_apply"
                or r.get("unscored_easy_apply") is True
            ):
                continue
            # A previous assessment may have used an obsolete rubric. Re-score it.
            if reason.startswith("score_"):
                continue
            return True
    return False


MANUAL_APPLY_FILE = DATA_DIR / "failed_jobs_for_manual_apply.json"


def save_for_manual_apply(job: Dict[str, Any], reason: str, apply_url: str = "", score: float = 0) -> None:
    """记录失败但得分合格的岗位，供人工投递。

    只记录 score >= 5.5 的岗位（已通过评分阈值）。
    去重：同一 job_id 不重复记录。

    Args:
        job: 岗位信息 (含 job_id, title, company, jd_text, location 等)
        reason: 失败原因
        apply_url: 投递 URL（LinkedIn/Glassdoor 岗位链接）
        score: 岗位评分 (优先使用此参数，回退到 job dict 中的 score/ai_score)
    """
    score_val = score or job.get("score") or job.get("ai_score") or 0
    try:
        score_val = float(score_val)
    except (ValueError, TypeError):
        score_val = 0

    # 只记录通过评分阈值的
    if score_val < 5.5 and not job.get('unscored_easy_apply'):
        return

    job_id = str(job.get("job_id", ""))
    records = load_json("failed_jobs_for_manual_apply.json")

    company = job.get("company", "Unknown")
    title = job.get("title", "Unknown")

    # 去重：同一 job_id 或同公司+同标题都只进人工队列一次。
    for r in records:
        if r.get("job_id") == job_id:
            return
        if _same_company_title(r, company, title):
            return

    records.append({
        "job_id": job_id,
        "title": title,
        "company": company,
        "location": job.get("location", ""),
        "score": None if job.get('unscored_easy_apply') else score_val,
        "apply_url": apply_url or job.get("url", ""),
        "reason": reason,
        "failed_at": datetime.now().isoformat(),
    })
    save_json("failed_jobs_for_manual_apply.json", records)

    # 同时更新 skipped_jobs 供去重
    record_skipped(job, f"manual_pending: {reason}")


def get_application_count_today() -> int:
    """获取今天已投递的数量。"""
    records = load_json("applied_jobs.json")
    today = datetime.now().strftime("%Y-%m-%d")
    count = 0
    for r in records:
        applied_at = r.get("applied_at", "")
        if applied_at.startswith(today):
            count += 1
    return count
