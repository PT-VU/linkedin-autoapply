"""Stable identity helpers for LinkedIn application records.

LinkedIn's numeric job-card ID is the primary identity.  A normalized JD
fingerprint is the second identity for cases where a card was collected from
an AI result surface before its numeric ID was resolved.  Company/title is
kept only as a legacy fallback for the one-time imported email/application
history, which does not contain LinkedIn IDs or full JDs.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable
from urllib.parse import parse_qs, urlsplit


def normalize_text(value: Any) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def linkedin_job_id(job_id: Any = "", link: str = "") -> str:
    raw = str(job_id or "").strip()
    match = re.search(r"(?:jobPosting:|/jobs/view/)(\d+)", raw)
    if match:
        return match.group(1)
    if raw.isdigit():
        return raw
    link_text = str(link or "")
    match = re.search(r"(?:jobPosting:|/jobs/view/)(\d+)", link_text)
    if match:
        return match.group(1)
    try:
        query = parse_qs(urlsplit(link_text).query)
        for key in ("currentJobId", "jobId", "jobPostingId"):
            value = (query.get(key) or [""])[0]
            if str(value).isdigit():
                return str(value)
    except Exception:
        pass
    return ""


def jd_fingerprint(job: Dict[str, Any] | None) -> str:
    if not job:
        return ""
    text = normalize_text(job.get("jd_text", ""))
    if len(text) < 200:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def legacy_company_title_key(company: Any, title: Any) -> str:
    company_key = normalize_text(company)
    title_key = normalize_text(title)
    if not company_key or not title_key:
        return ""
    return f"legacy:{company_key}|{title_key}"


def job_identity_keys(job: Dict[str, Any] | None) -> list[str]:
    if not job:
        return []
    keys: list[str] = []
    numeric_id = linkedin_job_id(job.get("job_id", ""), job.get("link", ""))
    if numeric_id:
        keys.append(f"linkedin:{numeric_id}")
    fingerprint = jd_fingerprint(job)
    if fingerprint:
        keys.append(f"jd:{fingerprint}")
    return keys


def primary_job_identity(job: Dict[str, Any] | None) -> str:
    keys = job_identity_keys(job)
    if keys:
        return keys[0]
    return legacy_company_title_key(
        (job or {}).get("company", ""), (job or {}).get("title", "")
    )


def identities_from_records(records: Iterable[Dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for record in records:
        result.update(job_identity_keys(record))
        legacy = legacy_company_title_key(record.get("company", ""), record.get("title", ""))
        if legacy and record.get("legacy_history"):
            result.add(legacy)
    return result
