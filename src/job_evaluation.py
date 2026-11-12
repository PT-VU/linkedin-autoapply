"""Theory-driven job evaluation used by the AI prompt and local fallback.

The score is deliberately not trained on interview outcomes. It expresses a
reasoned fit assessment against the candidate profile and career direction.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List


AGENT_SIGNALS = (
    "ai agent", "agentic", "multi-agent", "multi agent", "llm agent",
    "agent orchestration", "agent framework", "langgraph", "crewai", "autogen",
)
GENAI_SIGNALS = (
    "llm", "large language model", "generative ai", "genai", "rag",
    "retrieval augmented", "prompt engineering", "semantic search",
    "knowledge graph", "copilot studio", "claude code",
)
AUTOMATION_SIGNALS = (
    "workflow automation", "process automation", "power automate", "n8n",
    "zapier", "workato", "system integration", "api integration",
)
AI_CORE_SIGNALS = (
    "ai engineer", "machine learning engineer", "ml engineer", "data scientist",
    "deep learning", "pytorch", "model training", "fine-tuning", "fine tuning",
)
ENGINEERING_SIGNALS = (
    "python", "fastapi", "api", "docker", "kubernetes", "azure", "aws", "gcp",
    "ci/cd", "testing", "monitoring", "production", "git", "sql",
)
CUSTOMER_SIGNALS = (
    "client", "customer", "stakeholder", "discovery", "workshop", "consulting",
    "business requirement", "pre-sales", "presales", "translate business",
)
GOVERNANCE_SIGNALS = (
    "governance", "guardrail", "responsible ai", "risk", "compliance",
    "security", "human in the loop", "human-in-the-loop", "auditability",
)
ADJACENT_SIGNALS = (
    "data engineer", "data engineering", "data pipeline", "databricks", "etl",
    "analytics engineer", "business analyst", "mlops", "model deployment",
)
NON_TARGET_SIGNALS = (
    "sales representative", "account executive", "seo", "sea specialist",
    "financial controller", "controller", "campaign manager", "project manager",
    "sap consultant", "sap production", "online marketeer", "marketing manager",
    "recruiter", "customer support", "business development",
)
TARGET_TITLE_SIGNALS = (
    "ai engineer", "artificial intelligence engineer", "applied ai", "agent engineer",
    "agentic engineer", "ai agent", "llm engineer", "generative ai", "genai",
    "machine learning engineer", "ml engineer", "data scientist", "data engineer",
    "ai solution", "ai consultant", "ai enablement", "ai automation",
    "automation specialist", "workflow specialist", "forward deployed engineer",
    "prompt engineer", "ai platform",
)
AI_WORK_TERMS = (
    "ai agent", "ai-agent", "agents", "agentic", "llm", "large language model",
    "generative ai", "genai", "rag", "copilot studio", "machine learning model",
)
BUILD_ACTIONS = (
    "build", "develop", "design", "implement", "deploy", "productionize", "create",
    "architect", "orchestrate", "evaluate", "monitor", "fine-tune", "train",
    "bouwen", "ontwikkel", "ontwerp", "implementeer", "uitrollen", "testen",
)

JUNIOR_SIGNALS = (
    "junior", "entry level", "entry-level", "graduate", "trainee", "associate",
    "0-2 years", "0 to 2 years", "1-2 years", "1 to 2 years",
)
HIGH_SENIORITY_TITLE = (
    "senior", "lead", "principal", "staff", "architect", "manager", "head of",
    "director", "cto", "tech lead",
)

# The candidate is targeting junior/early-career individual-contributor roles.
# Keep this title gate separate from the scoring penalty below so an explicit
# senior/leadership title cannot pass merely because the JD contains many AI
# keywords.
SENIOR_SCOPE_TITLE = HIGH_SENIORITY_TITLE

# Government and public-sector employers are intentionally out of scope: in
# practice these roles commonly require Dutch and/or civil-service eligibility,
# and the user explicitly asked us not to send them to the application flow.
PUBLIC_EMPLOYER_SIGNALS = (
    "government", "governmental", "municipality", "municipal", "gemeente",
    "gemeentelijke", "provincie", "provincial", "rijksoverheid", "ministry",
    "ministerie", "police", "politie", "marechaussee", "law enforcement",
)
PUBLIC_SECTOR_JD_SIGNALS = (
    "public sector", "public service", "public authority", "government agency",
    "government department", "government organisation", "government organization",
    "overheidsorganisatie", "overheidsinstantie", "police department",
)

DUTCH_REQUIRED = (
    "dutch required", "dutch is required", "dutch is a must", "must speak dutch",
    "fluent dutch", "nederlands vereist", "vloeiend nederlands",
    "nederlandstalig", "nl-sprekend", "nederlandse taal vaardig",
    "goede beheersing van de nederlandse taal",
    "uitstekende beheersing van de nederlandse taal",
)
FRENCH_REQUIRED = (
    "french required", "french is required", "french is a must", "must speak french",
    "fluent french", "native french", "français requis", "francais requis",
    "français obligatoire", "francais obligatoire",
)
EU_CLEARANCE_REQUIRED = (
    "eu citizenship required", "eu national required", "must be an eu citizen",
    "security clearance from an eu member state", "nato clearance required",
)
STUDENT_REQUIRED = (
    "currently enrolled", "must be enrolled", "registered student", "wo-student",
    "hbo-student", "university student", "student status required",
)

_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10",
}


def _contains(text: str, patterns: Iterable[str]) -> bool:
    return any(pattern in text for pattern in patterns)


def _clamp(value: float) -> float:
    return round(max(0.0, min(10.0, value)), 1)


def _experience_requirement(text: str) -> int | None:
    matches = re.findall(
        r"(?:at least|minimum|min\.?|over|more than)?\s*(\d+)\s*\+?\s*(?:years?|yrs?|jaar|jaren)",
        text,
        flags=re.I,
    )
    if not matches:
        return None
    return max(int(value) for value in matches)


def _minimum_experience_requirement(text: str) -> int | None:
    """Extract an explicit minimum experience requirement.

    We intentionally do not treat every occurrence of ``3 years`` as a hard
    blocker (a JD may mention a project timeline or a preferred qualification).
    Only minimum/range/plus phrasing is a gate.  This keeps the deterministic
    fallback aligned with the AI rubric while handling common word-number forms.
    """
    normalized = text.lower()
    for word, number in _NUMBER_WORDS.items():
        normalized = re.sub(rf"\b{word}\b", number, normalized)
    patterns = (
        r"(?:at least|minimum|min\.?|more than|over|above)\s*(\d+)\s*\+?\s*(?:years?|yrs?|jaar|jaren)",
        r"(\d+)\s*\+\s*(?:years?|yrs?|jaar|jaren)",
        r"(\d+)\s*(?:or more|plus)\s*(?:years?|yrs?|jaar|jaren)",
        r"(\d+)\s*(?:-|to)\s*\d+\s*(?:years?|yrs?|jaar|jaren)",
    )
    values: List[int] = []
    for pattern in patterns:
        values.extend(int(value) for value in re.findall(pattern, normalized, flags=re.I))
    return max(values) if values else None


def _is_public_sector_role(title: str, company: str, jd: str) -> bool:
    employer_text = f"{title}\n{company}".lower()
    jd_text = jd.lower()
    return _contains(employer_text, PUBLIC_EMPLOYER_SIGNALS) or _contains(
        jd_text, PUBLIC_SECTOR_JD_SIGNALS
    )


def hard_exclusion_reasons(job: Dict[str, Any]) -> List[str]:
    """Return deterministic eligibility gates that must override an AI score.

    These are not a replacement for AI scoring.  They are safety gates for
    explicit user constraints, so a malformed or overly-generous model result
    cannot submit a role the user has already ruled out.
    """
    title = str(job.get("title", "") or "").strip()
    company = str(job.get("company", "") or "").strip()
    jd = str(job.get("jd_text", "") or "").strip()
    combined = f"{title}\n{company}\n{jd}".lower()
    reasons: List[str] = []
    if _contains(combined, DUTCH_REQUIRED):
        reasons.append("Dutch is explicitly required")
    if _contains(combined, FRENCH_REQUIRED):
        reasons.append("French is explicitly required")
    if _contains(combined, EU_CLEARANCE_REQUIRED):
        reasons.append("EU citizenship/security-clearance eligibility is required")
    if _contains(combined, STUDENT_REQUIRED):
        reasons.append("Current student status is explicitly required")
    if _is_public_sector_role(title, company, jd):
        reasons.append("Government/public-sector role is outside the target search")
    title_lower = title.lower()
    if _contains(title_lower, SENIOR_SCOPE_TITLE):
        reasons.append("Senior/leadership title is outside the junior-to-early-career target")
    minimum_years = _minimum_experience_requirement(combined)
    if minimum_years is not None and minimum_years >= 3:
        reasons.append(f"At least {minimum_years} years of experience is required")
    return reasons


def _substantive_ai_work(text: str) -> bool:
    sentences = re.split(r"[\n.!?;•]+", text)
    return sum(
        1 for sentence in sentences
        if _contains(sentence, AI_WORK_TERMS) and _contains(sentence, BUILD_ACTIONS)
    ) >= 2


def _role_archetype(text: str, title: str = "") -> str:
    target_title = _contains(title, TARGET_TITLE_SIGNALS)
    substantive_ai = _substantive_ai_work(text)
    if _contains(title, NON_TARGET_SIGNALS) and not substantive_ai:
        return "other"
    if not target_title and not substantive_ai and not _contains(title, ("data engineer", "data scientist")):
        if _contains(text, ADJACENT_SIGNALS):
            return "data_engineering_adjacent"
        return "other"
    if _contains(text, AGENT_SIGNALS):
        return "agentic_ai"
    if _contains(text, GENAI_SIGNALS) and _contains(text, CUSTOMER_SIGNALS):
        return "ai_solutions_consulting"
    if _contains(text, GENAI_SIGNALS) and _contains(text, ENGINEERING_SIGNALS):
        return "applied_ai_engineering"
    if _contains(text, AUTOMATION_SIGNALS):
        return "ai_automation_enablement"
    if "platform" in text and (_contains(text, GENAI_SIGNALS) or _contains(text, AI_CORE_SIGNALS)):
        return "ai_platform_llmops"
    if _contains(text, AI_CORE_SIGNALS):
        return "ml_data_science"
    if _contains(text, ADJACENT_SIGNALS):
        return "data_engineering_adjacent"
    return "other"


def evaluate_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """Return an explainable theoretical assessment for one job."""
    title = str(job.get("title", "") or "").strip()
    company = str(job.get("company", "") or "").strip()
    jd = str(job.get("jd_text", "") or "").strip()
    combined = f"{title}\n{company}\n{jd}".lower()
    title_lower = title.lower()
    evidence: List[str] = []
    gaps: List[str] = []

    eligibility = "pass"
    hard_blockers = hard_exclusion_reasons(job)

    years = _experience_requirement(combined)
    senior_title = _contains(title_lower, SENIOR_SCOPE_TITLE)
    junior_signal = _contains(combined, JUNIOR_SIGNALS)
    if senior_title:
        hard_blockers.append("Senior/leadership title is outside the junior-to-early-career target")

    if hard_blockers:
        return {
            "score": 0.0,
            "technical_match": 0.0,
            "career_match": 0.0,
            "practical_match": 0.0,
            "eligibility": "fail",
            "confidence": 0.9 if len(jd) >= 500 else 0.6,
            "decision": "skip",
            "role_archetype": _role_archetype(combined, title_lower),
            "genai_share": "unknown",
            "reason": "; ".join(hard_blockers),
            "evidence": hard_blockers,
            "gaps": [],
        }

    archetype = _role_archetype(combined, title_lower)
    target_title = _contains(title_lower, TARGET_TITLE_SIGNALS)
    substantive_ai = _substantive_ai_work(combined)
    role_is_target = target_title or substantive_ai
    has_agent = role_is_target and _contains(combined, AGENT_SIGNALS)
    has_genai = role_is_target and _contains(combined, GENAI_SIGNALS)
    has_automation = role_is_target and _contains(combined, AUTOMATION_SIGNALS)
    has_ai_core = role_is_target and _contains(combined, AI_CORE_SIGNALS)
    has_adjacent = role_is_target and _contains(combined, ADJACENT_SIGNALS)
    has_engineering = _contains(combined, ENGINEERING_SIGNALS)
    has_customer = _contains(combined, CUSTOMER_SIGNALS)
    has_governance = _contains(combined, GOVERNANCE_SIGNALS)

    technical = 3.5
    if has_agent:
        technical += 3.0
        evidence.append("explicit agentic/agent workflow duties")
    elif has_genai:
        technical += 2.5
        evidence.append("explicit LLM/GenAI/RAG duties")
    elif has_ai_core:
        technical += 1.8
        evidence.append("core AI/ML duties")
    elif has_adjacent:
        technical += 1.0
        evidence.append("transferable data/ML engineering duties")
    if has_engineering:
        technical += 0.8
        evidence.append("Python/cloud/API/production engineering overlap")
    if has_customer:
        technical += 0.5
        evidence.append("stakeholder or client-facing work")
    if years is not None and years >= 5:
        technical -= 2.5
        gaps.append(f"explicit {years}+ years requirement")
    elif years is not None and years >= 3:
        technical -= 1.2
        gaps.append(f"stretch: explicit {years}+ years requirement")
    if senior_title and not junior_signal:
        technical -= 1.5
        gaps.append("senior title/scope")
    if not role_is_target:
        technical -= 2.5
        gaps.append("AI appears incidental rather than a core delivery responsibility")
    technical = _clamp(technical)

    career = 4.0
    if has_agent:
        career = 9.2
    elif has_genai and (has_engineering or has_automation):
        career = 8.2
    elif has_automation:
        career = 7.5
    elif has_ai_core:
        career = 6.8
    elif has_adjacent:
        career = 5.8
    if has_customer:
        career += 0.5
    if has_governance:
        career += 0.5
        evidence.append("governance/risk/reliability responsibilities")
    if not role_is_target:
        career = min(career, 4.0)
    career = _clamp(career)

    practical = 7.0
    if "dutch is a plus" in combined or "nederlands is een pre" in combined:
        practical -= 0.3
    # Location fit is user-specific and belongs to the AI rubric/configured
    # search context; do not award a country-specific bonus in a public
    # fallback evaluator.
    practical = _clamp(practical)

    confidence = 0.9 if len(jd) >= 1200 else 0.7 if len(jd) >= 500 else 0.4
    score = _clamp(technical * 0.45 + career * 0.40 + practical * 0.15)
    if confidence < 0.6:
        score = min(score, 5.9)
        gaps.append("JD is incomplete; manual review required")

    if has_agent:
        genai_share = "50%+"
    elif has_genai or has_automation:
        genai_share = "30-50%"
    elif has_ai_core:
        genai_share = "10-30%"
    else:
        genai_share = "<10%"

    if eligibility == "review" or confidence < 0.6:
        decision = "manual_review"
    elif score >= 7.5 and technical >= 6.5:
        decision = "apply_first"
    elif score >= 6.5:
        decision = "apply_standard"
    elif score >= 5.5:
        decision = "manual_review"
    else:
        decision = "skip"

    reason_bits = [f"{archetype}", f"technical {technical}", f"career {career}"]
    if gaps:
        reason_bits.append("gap: " + gaps[0])
    return {
        "score": score,
        "technical_match": technical,
        "career_match": career,
        "practical_match": practical,
        "eligibility": eligibility,
        "confidence": confidence,
        "decision": decision,
        "role_archetype": archetype,
        "genai_share": genai_share,
        "reason": "; ".join(reason_bits),
        "evidence": evidence[:5],
        "gaps": gaps[:4],
    }


def score_jobs_theoretically(jobs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {
        str(job.get("job_id", "")): evaluate_job(job)
        for job in jobs
        if str(job.get("job_id", "")).strip()
    }
