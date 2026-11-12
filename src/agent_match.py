"""
Agent matching bridge module — JD collection → Agent scoring → apply.
Updated 2026-07-16: AI-driven scoring with user preferences + keyword fallback.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("linkedin_autoapply.agent_match")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "linkedin"
REVIEW_FILE = DATA_DIR / "jds_for_review.json"
SCORES_FILE = DATA_DIR / "agent_scores.json"
PREFERENCES_FILE = PROJECT_ROOT / "data" / "scoring_preferences.json"


def save_jds_for_review(jobs: List[Dict], resume_text: str) -> str:
    """Save JDs for agent review.

    Args:
        jobs: list of job dicts with job_id, title, company, jd_text, info
        resume_text: full resume text

    Returns:
        file path
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    review_data = {
        "resume_text": resume_text,
        "total_jobs": len(jobs),
        "jobs": [],
    }

    for j in jobs:
        jd_text = j.get("jd_text", "")
        info = j.get("info", {})
        review_data["jobs"].append({
            "job_id": j.get("job_id", ""),
            "title": j.get("title", ""),
            "company": j.get("company", ""),
            "application_method": j.get("application_method", "external"),
            "location": j.get("location", "") or info.get("location", ""),
            "salary": j.get("salary_preview", "") or info.get("salary", ""),
            "jd_text": jd_text,
            "seniority": info.get("seniority_level", ""),
            "employment_type": info.get("employment_type", ""),
        })

    with open(REVIEW_FILE, "w", encoding="utf-8") as f:
        json.dump(review_data, f, ensure_ascii=False, indent=2)

    logger.info(f"Saved {len(jobs)} JDs to {REVIEW_FILE} for agent review")
    return str(REVIEW_FILE)


def load_agent_scores() -> Optional[Dict[str, Any]]:
    """Load agent scoring results.

    Handles both formats:
      - Old: {"job_id": 7.0}
      - New: {"job_id": {"score": 7.0, "reason": "..."}}

    Returns:
        {job_id: score_value} dict (normalized to float), or None
    """
    if not SCORES_FILE.exists():
        return None

    try:
        with open(SCORES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw_scores = data.get("scores", data) if isinstance(data, dict) else {}

        return normalize_scores(raw_scores)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Failed to load agent scores: {e}")
        return None


def load_agent_scores_with_reasons() -> Optional[Dict[str, Dict]]:
    """Load agent scores WITH reasons (new format).

    Returns:
        {job_id: {score: float, reason: str}} dict, or None
    """
    if not SCORES_FILE.exists():
        return None

    try:
        with open(SCORES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        raw_scores = data.get("scores", data) if isinstance(data, dict) else {}

        result = {}
        for jid, val in raw_scores.items():
            if isinstance(val, dict):
                result[jid] = {
                    "score": float(val.get("score", 0)),
                    "reason": val.get("reason", ""),
                    "decision": str(val.get("decision", "")),
                }
            else:
                result[jid] = {"score": float(val), "reason": ""}
        return result
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Failed to load agent scores: {e}")
        return None


def save_agent_scores(scores: Dict[str, Any]) -> None:
    """Save agent scoring results.

    Supports both:
      - scores = {"job_id": 7.0}
      - scores = {"job_id": {"score": 7.0, "reason": "..."}}
    """
    from datetime import datetime

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "reviewed_at": datetime.now().isoformat(),
        "total_reviewed": len(scores),
        "scores": scores,
    }
    with open(SCORES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"Saved {len(scores)} agent scores to {SCORES_FILE}")


# ── AI-driven scoring prompt ──

SCORING_PROMPT_TEMPLATE = """You are a rigorous career-fit evaluator.

Use only the candidate profile, resume text, user requirements, and rubric
provided by the caller. Do not invent identity, language, work authorization,
salary, experience, or education facts. Do not optimize for previous application
outcomes or reward keyword density. Decode the actual role, seniority,
responsibilities, explicit requirements, and working conditions before scoring.

Apply the user's written hard blockers. Unknown facts remain unknown and should
lower confidence or produce manual_review rather than becoming an invented
rejection or acceptance. Explain technical fit, career fit, practical fit,
eligibility, confidence, evidence, gaps, and a 0-10 score for every job.

Return the complete structured schema for every job ID.

## Candidate context
{candidate_context}

## User requirements and rubric
{rubric}

## Jobs to score
{jobs_json}"""


def build_scoring_prompt(jobs: List[Dict]) -> str:
    """Build the AI scoring prompt with jobs embedded."""
    import json as _json
    candidate_context = {}
    rubric = ""
    try:
        from .config import load_config, get_resume_path, get_user_requirements_path

        config = load_config()
        candidate_context = config.get("profile", {})
        candidate_context = {
            "profile": candidate_context,
            "resume_path": str(get_resume_path(config)),
        }
        requirements_path = get_user_requirements_path(config)
        if requirements_path.is_file():
            candidate_context["user_requirements"] = requirements_path.read_text(encoding="utf-8")[:7000]
        rubric_path = Path(str((config.get("matching") or {}).get("rubric_path") or "data/scoring_rubric.md"))
        if not rubric_path.is_absolute():
            rubric_path = PROJECT_ROOT / rubric_path
        if rubric_path.is_file():
            rubric = rubric_path.read_text(encoding="utf-8")[:7000]
    except Exception as exc:
        logger.debug("Could not load candidate context for scoring prompt: %s", exc)
    jobs_data = []
    for j in jobs:
        jobs_data.append({
            "job_id": j.get("job_id", ""),
            "title": j.get("title", ""),
            "company": j.get("company", ""),
            "application_method": j.get("application_method", "external"),
            "location": j.get("location", ""),
            "salary": j.get("salary", ""),
            "jd_text": j.get("jd_text", "")[:3000],
        })
    return (
        SCORING_PROMPT_TEMPLATE
        .replace("{candidate_context}", _json.dumps(candidate_context, ensure_ascii=False, indent=2))
        .replace("{rubric}", rubric)
        .replace("{jobs_json}", _json.dumps(jobs_data, ensure_ascii=False, indent=2))
    )


# ── Theory-driven local fallback ──

def score_jobs_locally(jobs: List[Dict]) -> Dict[str, Any]:
    """Evaluate jobs without interview-outcome feedback when AI is unavailable."""
    from src.job_evaluation import score_jobs_theoretically

    scores = score_jobs_theoretically(jobs)
    n_pass = sum(1 for value in scores.values() if value.get("score", 0) >= 6.5)
    n_reject = sum(1 for value in scores.values() if value.get("score", 0) == 0)
    logger.info(
        "Theory scoring: %s/%s pass, %s hard-rejected, threshold=6.5",
        n_pass,
        len(scores),
        n_reject,
    )
    return scores


def normalize_scores(raw: Dict[str, Any]) -> Dict[str, float]:
    """Return application-gated scores from either old or rich score records.

    The original theoretical score remains intact in ``agent_scores.json``. Rich results that
    require review are capped below the automatic-application threshold here, preventing an
    uncertain assessment from silently becoming an application decision.
    """
    result = {}
    for jid, val in (raw or {}).items():
        if isinstance(val, (int, float)):
            result[jid] = float(val)
        elif isinstance(val, dict):
            score = float(val.get("score", 0))
            decision = str(val.get("decision", "")).strip().lower()
            eligibility = str(val.get("eligibility", "pass")).strip().lower()
            try:
                confidence = float(val.get("confidence", 1.0))
            except (TypeError, ValueError):
                confidence = 0.0
            if decision in {"manual_review", "skip"} or eligibility != "pass" or confidence < 0.6:
                score = min(score, 6.4)
            result[jid] = score
    return result


def has_pending_review() -> bool:
    """Check if there are JDs waiting for review."""
    return REVIEW_FILE.exists()


def has_scores() -> bool:
    """Check if scoring results exist."""
    return SCORES_FILE.exists()


def generate_jd_review_md(jobs: List[Dict], scores: Dict[str, Any]) -> str:
    """Generate JD review markdown with scores and reasons."""
    from datetime import datetime

    lines = [
        "# JD Review Records",
        f"> Updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "| # | Score | Decision | Company | Title | Reason |",
        "|---|------|------|------|------|------|",
    ]

    scored = []
    for j in jobs:
        jid = j.get("job_id", "")
        s = scores.get(jid, 0)
        if isinstance(s, dict):
            score_val = s.get("score", 0)
        else:
            score_val = float(s)
        scored.append((score_val, j, s))

    scored.sort(key=lambda x: x[0], reverse=True)

    for i, (score_val, j, s) in enumerate(scored, 1):
        if isinstance(s, dict) and s.get("decision"):
            decision = str(s["decision"]).replace("_", " ").title()
        elif score_val >= 7.5:
            decision = "Apply First"
        elif score_val >= 6.5:
            decision = "Apply Standard"
        elif score_val >= 5.5:
            decision = "Manual Review"
        elif score_val == 0:
            decision = "Hard-Reject"
        else:
            decision = "Skip"

        title = (j.get("title", "?") or "?").replace("\n", " ")[:40]
        company = (j.get("company", "?") or "?")[:20]
        if isinstance(s, dict):
            reason = s.get("reason", "")[:60]
        else:
            reason = _get_decision_reason(score_val, j)

        lines.append(f"| {i} | {score_val:.1f} | {decision} | {company} | {title} | {reason} |")

    gated_scores = normalize_scores({j.get("job_id", ""): s for _, j, s in scored})
    n_apply = sum(1 for value in gated_scores.values() if value >= 6.5)
    n_skip = len(gated_scores) - n_apply
    lines.extend([
        "",
        "---",
        f"> Total: {len(scored)} jobs | Apply: {n_apply} | Skip: {n_skip}",
    ])

    filepath = DATA_DIR / "jd_review.md"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logger.info(f"JD review saved: {filepath}")
    return str(filepath)


def _get_decision_reason(score: float, job: Dict) -> str:
    """Generate a short decision reason based on score."""
    if score >= 8.5:
        return "High match: skills + experience aligned"
    elif score >= 7.0:
        return "Good match: most criteria met"
    elif score >= 5.0:
        return "Partial match: some criteria unmet"
    elif score == 0:
        return "Hard-rejected by filter rule"
    else:
        return "Low match: unrelated field"
