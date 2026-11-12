#!/usr/bin/env python3
"""
Codex mode supervisor.

This process is the bridge between the existing flag protocol and local
`codex exec`. It does not replace monitor_parallel_v3.py; it runs beside it.

Flow:
  Python automation writes data/{platform}/*_needed.flag + context JSON
  this supervisor reads the context and invokes `codex exec`
  Codex returns schema-constrained JSON
  this supervisor writes the existing *_instructions.json / *_done.flag files
"""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
# When this file is launched as ``python scripts\\codex_supervisor.py``,
# Python puts ``scripts`` (not the project root) on sys.path.  Most response
# types do not import project modules until they are applied, which made this
# latent path bug appear only when a cover-letter response arrived.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DATA_ROOT = PROJECT_ROOT / "data"
CODEX_ROOT = DATA_ROOT / "codex"
RUNS_DIR = CODEX_ROOT / "runs"
OUTBOX_DIR = CODEX_ROOT / "outbox"
SCHEMA_FILE = PROJECT_ROOT / "scripts" / "codex_supervisor_response.schema.json"
CONFIG_FILE = Path(os.environ.get("LINKEDIN_AUTOAPPLY_CONFIG", PROJECT_ROOT / "config.yaml"))
QA_PREFERENCES_FILE = DATA_ROOT / "user_qa_preferences.json"
# Public builds inherit the local Codex model unless the user configures one.
# A private deployment can still pass an explicit model through its launcher.
CODEX_MODEL = ""

PLATFORMS = ("linkedin", "glassdoor", "magnet")

ALLOWED_ACTIONS = [
    "fill_input",
    "select_option",
    "click_radio",
    "click_radio_by_index",
    "click_checkbox",
    "click_button",
    "upload_file",
    "press_key",
    "wait",
    "react_fill",
    "fill_combobox",
]


@dataclass(frozen=True)
class FlagSpec:
    kind: str
    need_flag: str
    payload_file: str
    output_file: str
    done_flag: str
    timeout_s: int


SPECS = [
    FlagSpec("form_help", "form_help_needed.flag", "form_help.json", "form_instructions.json", "form_help_done.flag", 110),
    FlagSpec("ext_help", "ext_help_needed.flag", "ext_help.json", "ext_instructions.json", "ext_help_done.flag", 260),
    FlagSpec("cover_letter", "cover_letter_needed.flag", "cover_letter_context.json", "cover_letter_instructions.json", "cover_letter_done.flag", 260),
    FlagSpec("scoring", "scoring_needed.flag", "jds_for_review.json", "agent_scores.json", "scoring_done.flag", 150),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as exc:
        return {"_read_error": str(exc), "_path": str(path)}


def load_ai_config() -> dict[str, Any]:
    """Read the public AI backend settings without exposing secrets."""
    try:
        import yaml

        data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        section = data.get("ai", {}) if isinstance(data, dict) else {}
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Batch scoring can invoke several Codex workers at once.  A shared
    # ``incident.json.tmp`` name is unsafe on Windows: another worker (or a
    # stale antivirus/indexer handle) can still own it when replace() runs.
    # Give each writer a unique temp path and retry the final replace briefly.
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        last_error: OSError | None = None
        for attempt in range(6):
            try:
                tmp.replace(path)
                last_error = None
                break
            except PermissionError as exc:
                last_error = exc
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
        if last_error is not None:
            raise last_error
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def load_config_summary() -> dict[str, Any]:
    try:
        import yaml

        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}

    resume = cfg.get("resume", {}) if isinstance(cfg, dict) else {}
    resume_path = str(resume.get("path", ""))
    resume_text = ""
    if resume_path:
        try:
            sys.path.insert(0, str(PROJECT_ROOT))
            from src.resume_parser import load_resume

            resume_text = load_resume(str(PROJECT_ROOT / resume_path))[:7000]
        except Exception:
            resume_text = ""

    qa_preferences = load_json(QA_PREFERENCES_FILE, default={})
    requirements = cfg.get("user_requirements", {}) if isinstance(cfg, dict) else {}
    requirements_path = str(requirements.get("path", ""))
    requirements_text = ""
    if requirements_path:
        try:
            candidate = Path(requirements_path)
            candidate = candidate if candidate.is_absolute() else PROJECT_ROOT / candidate
            requirements_text = candidate.read_text(encoding="utf-8")[:7000]
        except OSError:
            requirements_text = ""

    return {
        "profile": cfg.get("profile", {}) if isinstance(cfg, dict) else {},
        "resume": {
            "path": resume_path,
            "summary": resume.get("summary", {}),
            "text_excerpt": resume_text,
        },
        "matching": cfg.get("matching", {}) if isinstance(cfg, dict) else {},
        "user_requirements": {
            "path": requirements_path,
            "text_excerpt": requirements_text,
        },
        "cover_letter": cfg.get("cover_letter", {}) if isinstance(cfg, dict) else {},
        "user_qa_preferences": qa_preferences.get("preferences", {}) if isinstance(qa_preferences, dict) else {},
    }


def trim_payload(kind: str, payload: Any) -> Any:
    """Keep prompts bounded while preserving the useful decision surface."""
    if not isinstance(payload, dict):
        return payload

    if kind == "scoring":
        jobs = payload.get("jobs", [])
        trimmed_jobs = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            jd_text = str(job.get("jd_text", ""))
            # A single employer page can contain a very long legal/benefits
            # appendix.  Keep the opening duties and the closing requirements
            # while bounding each scoring item so one pathological JD cannot
            # consume the entire Codex output budget.
            if len(jd_text) > 9000:
                jd_text = (
                    jd_text[:6000]
                    + "\n...[middle of JD omitted for scoring]...\n"
                    + jd_text[-3000:]
                )
            trimmed_jobs.append(
                {
                    "job_id": str(job.get("job_id", "")),
                    "title": job.get("title", ""),
                    "company": job.get("company", ""),
                    "application_method": job.get("application_method", "external"),
                    "location": job.get("location", ""),
                    "salary": job.get("salary", ""),
                    "seniority": job.get("seniority", ""),
                    "employment_type": job.get("employment_type", ""),
                    "jd_text": jd_text,
                }
            )
        return {
            "total_jobs": payload.get("total_jobs", len(trimmed_jobs)),
            "jobs": trimmed_jobs,
        }

    if kind == "form_help":
        trimmed = dict(payload)
        trimmed["form_state"] = str(trimmed.get("form_state", ""))[:12000]
        return trimmed

    if kind == "ext_help":
        trimmed = dict(payload)
        state = str(trimmed.get("form_state", ""))
        # The raw document source is mostly duplicated by the readable body and
        # the form-specific sections. Cutting the whole state at 12k previously
        # discarded file status, validation errors, and sometimes the fields.
        start = state.find("\n=== PAGE HTML EXCERPT ===")
        end = state.find("\n=== FORM ELEMENTS ===", start + 1) if start >= 0 else -1
        if start >= 0 and end > start:
            state = state[:start] + "\n=== PAGE HTML EXCERPT ===\n(omitted; see form elements and key outer HTML)" + state[end:]
        start = state.find("\n=== KEY OUTER HTML ===")
        end = state.find("\n=== COMBOBOX FIELDS ===", start + 1) if start >= 0 else -1
        if start >= 0 and end > start:
            marker = "\n=== KEY OUTER HTML ==="
            snippets = state[start + len(marker):end].splitlines()
            # Prefer control markup over navigation links when a page is large.
            snippets.sort(key=lambda line: 0 if re.match(r"\s*(?:\[visible=(?:true|false)\]\s*)?<(?:input|select|textarea|button)\b", line, re.I) else 1)
            kept, size = [], 0
            for line in snippets:
                if size + len(line) + 1 > 8000:
                    continue
                kept.append(line)
                size += len(line) + 1
            if len(kept) < len(snippets):
                kept.append("(additional outer HTML omitted)")
            state = state[:start] + marker + "\n" + "\n".join(kept) + state[end:]
        trimmed["form_state"] = state
        return trimmed

    if kind == "cover_letter":
        trimmed = dict(payload)
        trimmed["jd_text"] = str(trimmed.get("jd_text", ""))
        trimmed["resume_path"] = str(trimmed.get("resume_path", ""))
        trimmed["candidate_profile"] = trimmed.get("candidate_profile", {})
        return trimmed

    return payload


def build_event_id(platform: str, spec: FlagSpec, flag_path: Path, payload_path: Path, payload: Any) -> str:
    flag_stat = flag_path.stat()
    payload_mtime = payload_path.stat().st_mtime_ns if payload_path.exists() else 0
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)[:20000]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{platform}-{spec.kind}-{flag_stat.st_mtime_ns}-{payload_mtime}-{digest}"


def build_incident(platform: str, spec: FlagSpec, payload: Any, event_id: str) -> dict[str, Any]:
    if spec.kind == 'scoring':
        rubric_path = DATA_ROOT / 'scoring_rubric.md'
        try:
            import yaml

            configured = (yaml.safe_load(CONFIG_FILE.read_text(encoding='utf-8')) or {}).get('matching', {}).get('rubric_path', '')
            if configured:
                candidate = Path(str(configured))
                rubric_path = candidate if candidate.is_absolute() else PROJECT_ROOT / candidate
        except Exception:
            pass
        rubric = rubric_path.read_text(encoding='utf-8') if rubric_path.exists() else 'Use the candidate profile and resume evidence; do not invent personal facts.'
        return {'event_id': event_id, 'created_at': utc_now(), 'mode': 'codex',
                'platform': platform, 'kind': spec.kind, 'rubric': rubric,
                'payload': trim_payload(spec.kind, payload)}
    if spec.kind == 'ext_help':
        return {'event_id': event_id, 'created_at': utc_now(), 'mode': 'codex',
                'platform': platform, 'kind': spec.kind,
                'context_map': 'data/applicant_context/README.md',
                'payload': trim_payload(spec.kind, payload)}
    if spec.kind == 'cover_letter':
        return {'event_id': event_id, 'created_at': utc_now(), 'mode': 'codex',
                'platform': platform, 'kind': spec.kind,
                'context_map': 'data/applicant_context/README.md',
                'payload': trim_payload(spec.kind, payload)}
    return {
        "event_id": event_id,
        "created_at": utc_now(),
        "mode": "codex",
        "platform": platform,
        "kind": spec.kind,
        "flag": f"data/{platform}/{spec.need_flag}",
        "expected_output_file": f"data/{platform}/{spec.output_file}",
        "expected_done_flag": f"data/{platform}/{spec.done_flag}",
        "config_summary": load_config_summary(),
        "payload": trim_payload(spec.kind, payload),
    }


def build_prompt(incident: dict[str, Any]) -> str:
    kind = incident["kind"]
    platform = incident["platform"]
    event_id = incident["event_id"]

    task_rules = {
        "scoring": (
            "Evaluate every job using the rubric and candidate context supplied in the incident. "
            "Do not use hard-coded names, countries, languages, salary expectations, visa facts, "
            "industry assumptions, or past application outcomes. First decode the actual role, "
            "seniority, substantive duties, location, language, work authorization and explicit "
            "experience requirements. Apply only the user's written hard blockers; keep unknown "
            "facts unknown and use manual_review when they materially affect the decision. "
            "Use evidence from the supplied resume/profile and explain technical fit, career fit, "
            "practical fit, gaps, eligibility and confidence. Return response_type='scores', include "
            "every job_id exactly once, and fill every schema field. Do not inflate a score to cross "
            "an application gate."
        ),
        "form_help": (
            "Analyze the Easy Apply/Indeed form_state and return executable "
            "actions only. Prefer known truthful profile values. Do not invent "
            "answers for salary, legal declarations, demographic fields, or "
            "questions not answerable from the payload; use needs_user instead. "
            "Use only deterministic answers present in payload.known_answers, "
            "config_summary.user_qa_preferences, or the configured profile; do not "
            "assume country, work authorization, sponsorship, distance, or driving "
            "status for a new user. Apply configured distance/proximity answers when "
            "present. Use needs_user for a genuinely unknown question after checking "
            "the stored QA preferences. "
            "The embedded profile may include diploma_path and transcript_path. "
            "If a visible required file control is labeled diploma/degree/certificate, "
            "use profile.diploma_path; if it is labeled transcript/grade list/academic "
            "record, use profile.transcript_path. Use profile.resume_path or the configured "
            "resume path for CV/resume controls. Do not ask the user for these paths when "
            "they are present and the matching file exists. Before returning actions, cover "
            "every visible required field and use a different selector or label when an "
            "earlier action failed."
        ),
        "ext_help": (
            "Analyze the external ATS page state and return executable actions. "
            "Handle cookie banners, profile fields, CV upload, radio/selects, "
            "and navigation buttons. If the application form is not yet visible, click Apply first "
            "and wait for a new page state; hidden fields in HTML are not ready to fill. "
            "Use form_metadata for live visibility, field types, labels, requirements and select options; hidden fields are not fillable. "
            "Review failure_summary across previous rounds and recent_actions: choose a different strategy for repeated failures; "
            "do not repeat "
            "ineffective actions on an unchanged page; choose a different visible control "
            "or report needs_user if the form is inaccessible. Use payload.profile_fields for basic contact "
            "information and the CV file path, and payload.known_answers for deterministic "
            "questions; leave correctly selected controls unchanged and use click_radio with the full question text and Yes/No. "
            "For an open-ended response, read only the context map and the specific supporting "
            "file it directs you to; do not read unrelated project files. Do not invent answers. If a cover "
            "letter is required, use the context map before requesting user input. "
            "Use one stable page and let dynamic controls settle; do not refresh, open duplicate tabs, "
            "or issue speculative/rapid clicks. Before a final submit, verify all visible required controls "
            "are valid. Perform at most one final submit action on the page, then wait and inspect for a receipt. "
            "A passive or hidden hCaptcha iframe, hCaptcha frame text, or hidden token field is not a human handoff. "
            "If a challenge is reported while any visible required field, location/country selector, or resume upload "
            "is still missing, fill those fields first and return instructions. Return needs_user only when the form "
            "is complete and a visible interactive CAPTCHA is the remaining action; do not click, refresh, retry, solve, "
            "or bypass the CAPTCHA."
        ),
        "cover_letter": (
            "Read the exact context map, the configured resume_path in the payload, and only its named supporting files, then generate a truthful, role-specific English cover letter based on the JD "
            "and verified candidate facts. Focus on why this specific role is attractive, "
            "why one or two relevant experiences demonstrate fit, and the candidate's real "
            "motivation and working style. Do not repeat the CV or invent qualifications. "
            "Use candidate_profile only for contact/basic facts and keep all experience claims grounded in the CV and resume_facts.md. "
            "Use a professional greeting and closing. Return response_type "
            "'cover_letter', cover_letter_text, a .docx cover_letter_filename, and "
            "actions to fill/upload it when possible."
        ),
    }

    if kind == 'scoring':
        task_rules['scoring'] = (
            "Evaluate every job independently using the rubric in the incident. "
            "The rubric is the user's authoritative current preference. Return response_type='scores', "
            "one result for each exact job_id, and all required schema fields. "
            "Keep each reason to one sentence and evidence/gaps to at most three short bullets; "
            "never repeat the incident text or emit commentary outside the JSON object."
        )

    filesystem_rule = (
        "For external form help only, you may read the exact context-map path in the incident and the exact files it names. "
        "Do not inspect any other filesystem paths."
        if kind in {'ext_help', 'cover_letter'} else
        "Do not inspect the filesystem. Do not run commands. Use only the embedded incident JSON."
    )
    return (
        "You are the background AI for LinkedIn-AutoApply in CODEx mode.\n"
        "You were invoked by a local supervisor after a flag file was detected.\n"
        f"{filesystem_rule}\n"
        "Return exactly one JSON object matching the provided output schema.\n\n"
        f"Event id: {event_id}\n"
        f"Platform: {platform}\n"
        f"Task kind: {kind}\n"
        f"Task: {task_rules.get(kind, 'Analyze the incident and return a safe response.')}\n\n"
        "Allowed action types:\n"
        f"{json.dumps(ALLOWED_ACTIONS, ensure_ascii=False)}\n\n"
        "Action field semantics for the existing executor:\n"
        "- fill_input.target: plain name/label/placeholder substring, not CSS. Example target='First name', value='configured profile first name'.\n"
        "- select_option.target: plain select name/label substring, value is visible option text.\n"
        "- click_radio.value: visible radio text such as 'Yes' or 'No'; target may be ''.\n"
        "- click_button.value: visible button text such as 'Next', 'Review', or 'Submit application'; target may be ''.\n"
        "- upload_file.value: absolute or project-relative file path.\n"
        "- For Easy Apply upload_file, use the embedded profile.diploma_path or profile.transcript_path when the field label matches; never substitute the CV for a diploma/transcript.\n"
        "- press_key.value: key name such as 'ArrowDown' or 'Enter'.\n"
        "- wait.value: seconds as a string.\n"
        "- react_fill/fill_combobox.target may be CSS selector only when the form_state exposes one.\n\n"
        "Top-level output requirements:\n"
        "- response_type must be one of: instructions, scores, cover_letter, needs_user, error.\n"
        "- event_id must exactly match the input event_id.\n"
        "- For instructions: put actions in actions, scores must be [], cover_letter_text must be ''.\n"
        "- For scores: return every schema field, including dimension scores, eligibility, confidence, decision, evidence and gaps; actions must be [], cover_letter_text must be ''.\n"
        "- Before needs_user, check config_summary.user_qa_preferences and payload.qa_preferences for stored answers.\n"
        "- For needs_user: set needs_user=true and include specific questions; actions must be [].\n"
        "- Keep explanation to one short sentence.\n\n"
        "Incident JSON:\n"
        f"{json.dumps(incident, ensure_ascii=False, indent=2)}\n"
    )


def codex_command(args: argparse.Namespace, result_file: Path) -> list[str]:
    runtime = load_json(PROJECT_ROOT / "codex_runtime.json", default={})
    candidates = [
        os.environ.get("LINKEDIN_CODEX_CLI"),
        # A checked-in project runtime is the deterministic fallback when a
        # stale LINKEDIN_CODEX_CLI points at an old/deleted installation.  It
        # also keeps tests and unattended runs independent of the desktop
        # app's rotating versioned runtime directory.
        runtime.get("executable") if isinstance(runtime, dict) else None,
        # The desktop app exposes the currently installed CLI here.  Prefer
        # it over the npm shim: the shim resolves through Node and, under the
        # project sandbox, can fail before `codex exec` starts with EPERM on
        # C:\\Users\\PT.  It remains a fallback if the project runtime is
        # absent or stale.
        os.environ.get("CODEX_APP_RUNTIME_CLI_PATH"),
        shutil.which("codex"),
    ]
    codex = next((str(path) for path in candidates if path and Path(path).is_file()), None)
    if not codex:
        raise RuntimeError("codex CLI was not found on PATH")

    requested_model = getattr(args, "model", "") or os.environ.get("LINKEDIN_AUTOAPPLY_CODEX_MODEL", "").strip()
    if not requested_model:
        try:
            import yaml

            cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
            requested_model = str((cfg.get("ai") or {}).get("model") or "").strip()
        except Exception:
            requested_model = ""

    cmd = [codex]
    if requested_model:
        cmd.extend(["--model", requested_model])
    cmd.extend([
        # The installed CLI accepts only ``fast``/``flex`` service tiers;
        # override an older global config that still contains ``priority``.
        "-c",
        "service_tier=fast",
    ])
    cmd.extend(
        [
            "exec",
            "-C",
            str(PROJECT_ROOT),
            "-s",
            args.sandbox,
            "--skip-git-repo-check",
            "--ephemeral",
            "--output-schema",
            str(SCHEMA_FILE),
            "--output-last-message",
            str(result_file),
            "-",
        ]
    )
    return cmd


def parse_codex_result(result_file: Path, stdout_file: Path) -> dict[str, Any]:
    text = ""
    if result_file.exists():
        text = result_file.read_text(encoding="utf-8-sig").strip()
    if not text and stdout_file.exists():
        text = stdout_file.read_text(encoding="utf-8-sig").strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match:
            return json.loads(match.group(0))
        raise


def is_codex_quota_exhausted(stdout_file: Path) -> bool:
    """Recognize the CLI's terminal usage-limit response without parsing prose elsewhere."""
    try:
        message = stdout_file.read_text(encoding="utf-8-sig").lower()
    except OSError:
        return False
    return any(marker in message for marker in (
        "you've hit your usage limit",
        "you’ve hit your usage limit",
        "usage limit has been reached",
        "rate limit reached",
    ))


def codex_failure_explanation(stdout_file: Path, returncode: int) -> str:
    """Return a concise, actionable reason for a failed Codex invocation.

    The old generic ``codex exec returncode=1`` made an unavailable/old CLI
    look like an Easy Apply form error.  Preserve the raw stdout in the run
    directory, but expose the first useful diagnostic to the waiting process.
    """
    try:
        text = stdout_file.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        text = ""
    lowered = text.casefold()
    if "requires a newer version of codex" in lowered:
        return (
            "Configured Codex model requires a newer Codex CLI; "
            "upgrade the local Codex app/CLI before retrying."
        )
    if "model is unavailable" in lowered or "model_not_found" in lowered:
        return "Configured Codex model is unavailable to this Codex runtime."
    if "usage limit" in lowered or "rate limit" in lowered or "quota" in lowered:
        return "Codex usage limit reached; stop this run and retry after the limit resets."
    if "401 unauthorized" in lowered or "missing bearer" in lowered:
        return (
            "Codex CLI authentication failed (HTTP 401: missing bearer); "
            "the local CLI could not attach the logged-in account to this subprocess. "
            "Re-authenticate the Codex CLI, then retry."
        )
    match = re.search(r'"message"\s*:\s*"((?:\\.|[^"\\])*)"', text, flags=re.S)
    if match:
        try:
            message = json.loads('"' + match.group(1) + '"')
        except Exception:
            message = match.group(1)
        if message:
            return f"codex exec returncode={returncode}: {message}"
    return f"codex exec returncode={returncode}"


def invoke_codex(incident: dict[str, Any], args: argparse.Namespace) -> tuple[int, dict[str, Any] | None, Path]:
    event_id = incident["event_id"]
    run_dir = RUNS_DIR / event_id
    run_dir.mkdir(parents=True, exist_ok=True)
    incident_file = run_dir / "incident.json"
    prompt_file = run_dir / "prompt.txt"
    result_file = run_dir / "result.json"
    stdout_file = run_dir / "codex_stdout.log"

    write_json(incident_file, incident)
    prompt = build_prompt(incident)
    prompt_file.write_text(prompt, encoding="utf-8")

    # The public distribution can use an OpenAI-compatible API without
    # changing the existing flag protocol.  Keep this branch inside the same
    # function so scoring retries, cover letters and form-help events all
    # share identical success/error handling.
    ai_config = load_ai_config()
    if str(ai_config.get("backend", "codex")).strip().lower() in {"api", "openai", "openai_compatible"}:
        started = time.time()
        try:
            from linkedin_autoapply.ai_backend import call_openai_compatible

            response = call_openai_compatible(
                prompt=prompt,
                config=ai_config,
                timeout=args.codex_timeout,
                result_file=result_file,
                stdout_file=stdout_file,
            )
        except Exception as exc:
            stdout_file.write_text(f"api backend error: {exc}\n", encoding="utf-8")
            (run_dir / "meta.json").write_text(
                json.dumps({"backend": "api", "returncode": 1, "elapsed_s": round(time.time() - started, 2)}, indent=2),
                encoding="utf-8",
            )
            return 1, None, run_dir
        (run_dir / "meta.json").write_text(
            json.dumps({"backend": "api", "returncode": 0, "elapsed_s": round(time.time() - started, 2)}, indent=2),
            encoding="utf-8",
        )
        return 0, response, run_dir

    cmd = codex_command(args, result_file)
    # On Windows, launching the native CLI directly from Python can inherit a
    # restricted socket policy (os error 10013), while the same command via
    # cmd.exe is permitted. Keep stdin/stdout streaming unchanged, but use the
    # shell host for the child process so supervisor calls match the supported
    # interactive invocation.
    if os.name == "nt":
        cmd = ["cmd.exe", "/d", "/c", *cmd]
    started = time.time()
    with stdout_file.open("w", encoding="utf-8") as out:
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(PROJECT_ROOT),
                input=prompt,
                stdout=out,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                timeout=args.codex_timeout,
            )
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            out.write(f"\n[TIMEOUT] codex exec exceeded {args.codex_timeout}s\n")
            return 124, None, run_dir

    response = None
    if returncode == 0:
        try:
            response = parse_codex_result(result_file, stdout_file)
        except Exception as exc:
            (run_dir / "parse_error.txt").write_text(str(exc), encoding="utf-8")

    elapsed = time.time() - started
    (run_dir / "meta.json").write_text(
        json.dumps({"returncode": returncode, "elapsed_s": round(elapsed, 2)}, indent=2),
        encoding="utf-8",
    )
    return returncode, response, run_dir


def sanitize_actions(actions: Any) -> list[dict[str, str]]:
    safe: list[dict[str, str]] = []
    if not isinstance(actions, list):
        return safe
    for action in actions:
        if not isinstance(action, dict):
            continue
        atype = str(action.get("type", ""))
        if atype not in ALLOWED_ACTIONS:
            continue
        safe.append(
            {
                "type": atype,
                "target": str(action.get("target", "")),
                "value": str(action.get("value", "")),
            }
        )
    return safe


def normalize_score_array(scores: Any) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    if not isinstance(scores, list):
        return normalized
    for item in scores:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("job_id", "")).strip()
        if not job_id:
            continue
        try:
            score = float(item.get("score", 0))
        except (TypeError, ValueError):
            score = 0.0
        normalized[job_id] = {
            "score": max(0.0, min(10.0, score)),
            "technical_match": float(item.get("technical_match", 0)),
            "career_match": float(item.get("career_match", 0)),
            "practical_match": float(item.get("practical_match", 0)),
            "eligibility": str(item.get("eligibility", "review")),
            "confidence": float(item.get("confidence", 0)),
            "decision": str(item.get("decision", "manual_review")),
            "role_archetype": str(item.get("role_archetype", "unknown")),
            "genai_share": str(item.get("genai_share", "unknown")),
            "reason": str(item.get("reason", "")),
            "evidence": [str(value) for value in item.get("evidence", [])][:6],
            "gaps": [str(value) for value in item.get("gaps", [])][:6],
        }
    return normalized


def save_cover_letter_docx(path: Path, text: str) -> None:
    """Write a simple DOCX cover letter using python-docx."""
    from docx import Document

    doc = Document()
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        paragraph = paragraph.strip()
        if paragraph:
            doc.add_paragraph(paragraph)
    if not text.strip():
        doc.add_paragraph("")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))


def write_user_needed(platform: str, spec: FlagSpec, response: dict[str, Any], run_dir: Path) -> None:
    data_dir = DATA_ROOT / platform
    output_file = data_dir / spec.output_file
    done_flag = data_dir / spec.done_flag
    need_flag = data_dir / spec.need_flag

    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    event_id = str(response.get("event_id", "unknown"))
    questions = response.get("questions", [])
    explanation = response.get("explanation", "")
    outbox_file = OUTBOX_DIR / f"{event_id}.json"
    write_json(
        outbox_file,
        {
            "created_at": utc_now(),
            "platform": platform,
            "kind": spec.kind,
            "questions": questions,
            "explanation": explanation,
            "retryable": bool(response.get("retryable", False)),
            "codex_quota_exhausted": bool(response.get("codex_quota_exhausted", False)),
            "run_dir": str(run_dir),
        },
    )

    if spec.kind in {"form_help", "ext_help", "cover_letter"}:
        write_json(
            output_file,
            {
                "source": "codex",
                "needs_user": True,
                "questions": questions,
                "actions": [],
                "explanation": explanation,
                "retryable": bool(response.get("retryable", False)),
                "codex_quota_exhausted": bool(response.get("codex_quota_exhausted", False)),
                "codex_run_dir": str(run_dir),
            },
        )
        touch(done_flag)
        safe_unlink(need_flag)

    print(f"[codex-supervisor] needs user input: {outbox_file}", flush=True)


def apply_response(platform: str, spec: FlagSpec, response: dict[str, Any], run_dir: Path) -> None:
    data_dir = DATA_ROOT / platform
    output_file = data_dir / spec.output_file
    done_flag = data_dir / spec.done_flag
    need_flag = data_dir / spec.need_flag

    response_type = response.get("response_type", "error")
    if response.get("needs_user") or response_type == "needs_user":
        write_user_needed(platform, spec, response, run_dir)
        return

    if response_type == "error":
        write_user_needed(
            platform,
            spec,
            {
                "event_id": response.get("event_id", "unknown"),
                "questions": response.get("questions", []) or [
                    response.get("explanation", f"Codex returned error for {spec.kind}")
                ],
                "explanation": response.get("explanation", ""),
            },
            run_dir,
        )
        return

    if response_type == "scores" and spec.kind == "scoring":
        scores = normalize_score_array(response.get("scores", []))
        write_json(
            output_file,
            {
                "reviewed_at": utc_now(),
                "total_reviewed": len(scores),
                "scores": scores,
                "scored_by": "codex",
                "codex_run_dir": str(run_dir),
                "explanation": response.get("explanation", ""),
            },
        )
        touch(done_flag)
        safe_unlink(need_flag)
        return

    if response_type == "cover_letter" and spec.kind == "cover_letter":
        text = str(response.get("cover_letter_text", "")).strip()
        context = load_json(data_dir / spec.payload_file, default={})
        filename = str(context.get("output_filename", "")).strip() if isinstance(context, dict) else ""
        filename = filename or str(response.get("cover_letter_filename", "")).strip() or f"cover_letter_{response.get('event_id', 'codex')}.docx"
        filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename)[:120]
        if not filename.lower().endswith(".docx"):
            filename = re.sub(r"\.[A-Za-z0-9]+$", "", filename) + ".docx"
        from src.cover_letter_agent import COVER_LETTER_DIR
        cover_path = COVER_LETTER_DIR / filename
        if text:
            save_cover_letter_docx(cover_path, text)

        write_json(
            output_file,
            {
                "source": "codex",
                "cover_letter_text": text,
                "cover_letter_path": str(cover_path),
                "cover_letter_docx_path": str(cover_path),
                "actions": sanitize_actions(response.get("actions", [])),
                "explanation": response.get("explanation", ""),
                "codex_run_dir": str(run_dir),
            },
        )
        touch(done_flag)
        return

    if response_type == "instructions" and spec.kind in {"form_help", "ext_help"}:
        write_json(
            output_file,
            {
                "source": "codex",
                "actions": sanitize_actions(response.get("actions", [])),
                "explanation": response.get("explanation", ""),
                "codex_run_dir": str(run_dir),
            },
        )
        touch(done_flag)
        return

    write_user_needed(
        platform,
        spec,
        {
            "event_id": response.get("event_id", "unknown"),
            "questions": [f"Codex returned unsupported response_type={response_type} for {spec.kind}"],
            "explanation": response.get("explanation", ""),
        },
        run_dir,
    )


def pending_events() -> list[tuple[str, FlagSpec, Path]]:
    events = []
    for platform in PLATFORMS:
        data_dir = DATA_ROOT / platform
        for spec in SPECS:
            need_flag = data_dir / spec.need_flag
            done_flag = data_dir / spec.done_flag
            if not need_flag.exists():
                continue
            if done_flag.exists():
                continue
            events.append((platform, spec, need_flag))
    return events


def process_one(platform: str, spec: FlagSpec, flag_path: Path, args: argparse.Namespace) -> bool:
    data_dir = DATA_ROOT / platform
    payload_path = data_dir / spec.payload_file
    payload = load_json(payload_path, default={})
    try:
        event_id = build_event_id(platform, spec, flag_path, payload_path, payload)
    except FileNotFoundError:
        return False
    run_dir = RUNS_DIR / event_id
    lock_file = run_dir / ".processing"
    done_marker = run_dir / ".done"

    if lock_file.exists() or done_marker.exists():
        return False

    run_dir.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(str(os.getpid()), encoding="utf-8")
    print(f"[codex-supervisor] handling {platform}/{spec.kind}: {event_id}", flush=True)

    old_timeout = args.codex_timeout
    args.codex_timeout = min(args.codex_timeout, spec.timeout_s)
    try:
        incident = build_incident(platform, spec, payload, event_id)
        if args.dry_run:
            write_json(run_dir / "incident.json", incident)
            print(f"[codex-supervisor] dry-run wrote incident: {run_dir}", flush=True)
            return True

        jobs = incident.get('payload', {}).get('jobs', []) if spec.kind == 'scoring' else []
        if spec.kind == 'scoring' and jobs:
            # Three jobs keeps the schema response comfortably below the CLI's
            # output budget, including long employer pages. Even small pages
            # use this path so partial-but-valid JSON responses are checked and
            # missing job IDs are retried instead of being accepted silently.
            chunks = [jobs[i:i + 3] for i in range(0, len(jobs), 3)]
            def score_chunk(item):
                index, batch = item
                sub = copy.deepcopy(incident)
                sub['event_id'] = f'{event_id}-batch-{index + 1}'
                sub['payload']['jobs'] = batch
                sub['payload']['total_jobs'] = len(batch)
                return invoke_codex(sub, args)
            print(f'[codex-supervisor] scoring {len(jobs)} jobs in {len(chunks)} batches (up to 3 parallel)', flush=True)
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                batch_results = list(pool.map(score_chunk, enumerate(chunks)))

            # A successful CLI exit is not enough: Codex can return a valid
            # schema object containing only part of a requested batch. Retry
            # just the missing jobs in smaller groups rather than discarding
            # the whole keyword or accepting an incomplete score map.
            for index, batch in enumerate(chunks):
                rc, result, path = batch_results[index]
                expected_ids = {str(job.get('job_id', '')) for job in batch}
                returned_ids = {
                    str(item.get('job_id', ''))
                    for item in (result or {}).get('scores', [])
                    if isinstance(item, dict)
                }
                # A whole batch can fail because the CLI/model exhausts its
                # output budget. Retry the failed batch as pairs before
                # declaring the keyword unscorable; previously one bad batch
                # discarded all successful sibling batches.
                if rc != 0 or result is None or result.get('response_type') != 'scores':
                    retry_chunks = [batch[i:i + 2] for i in range(0, len(batch), 2)]

                    def retry_failed_chunk(item):
                        retry_index, retry_batch = item
                        sub = copy.deepcopy(incident)
                        sub['event_id'] = f'{event_id}-batch-{index + 1}-failed-retry-{retry_index + 1}'
                        sub['payload']['jobs'] = retry_batch
                        sub['payload']['total_jobs'] = len(retry_batch)
                        return invoke_codex(sub, args)

                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                        retry_results = list(pool.map(retry_failed_chunk, enumerate(retry_chunks)))
                    retry_scores = [
                        item
                        for retry_rc, retry_result, _ in retry_results
                        if retry_rc == 0 and retry_result and retry_result.get('response_type') == 'scores'
                        for item in retry_result.get('scores', [])
                    ]
                    retry_ids = {str(item.get('job_id', '')) for item in retry_scores if isinstance(item, dict)}
                    if retry_ids >= expected_ids:
                        batch_results[index] = (
                            0,
                            {'response_type': 'scores', 'scores': retry_scores,
                             'explanation': 'Recovered failed scoring batch in smaller chunks'},
                            retry_results[0][2] if retry_results else path,
                        )
                        continue
                    print(
                        f'[codex-supervisor] batch {index + 1} failed and smaller retry '
                        f'covered {len(retry_ids)}/{len(expected_ids)} jobs', flush=True,
                    )
                    continue

                missing_jobs = [job for job in batch if str(job.get('job_id', '')) not in returned_ids]
                if not missing_jobs:
                    continue
                retry_chunks = [missing_jobs[i:i + 3] for i in range(0, len(missing_jobs), 3)]
                print(
                    f'[codex-supervisor] batch {index + 1} returned '
                    f'{len(returned_ids)}/{len(expected_ids)} scores; retrying '
                    f'{len(missing_jobs)} missing jobs in {len(retry_chunks)} smaller batches',
                    flush=True,
                )

                def retry_chunk(item):
                    retry_index, retry_batch = item
                    sub = copy.deepcopy(incident)
                    sub['event_id'] = f'{event_id}-batch-{index + 1}-retry-{retry_index + 1}'
                    sub['payload']['jobs'] = retry_batch
                    sub['payload']['total_jobs'] = len(retry_batch)
                    return invoke_codex(sub, args)

                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    retry_results = list(pool.map(retry_chunk, enumerate(retry_chunks)))
                retry_scores = [
                    item
                    for retry_rc, retry_result, _ in retry_results
                    if retry_rc == 0 and retry_result and retry_result.get('response_type') == 'scores'
                    for item in retry_result.get('scores', [])
                ]
                retry_ids = {str(item.get('job_id', '')) for item in retry_scores if isinstance(item, dict)}
                if retry_ids >= {str(job.get('job_id', '')) for job in missing_jobs}:
                    merged = dict(result)
                    merged['scores'] = list(result.get('scores', [])) + retry_scores
                    batch_results[index] = (0, merged, path)
                else:
                    # Leave the batch as-is so the normal failure path emits a
                    # clear diagnostic and the caller stops safely.
                    print(
                        f'[codex-supervisor] batch {index + 1} still missing '
                        f'{len(expected_ids - returned_ids - retry_ids)} scores after retry',
                        flush=True,
                    )
            failures = [(rc, result, path) for rc, result, path in batch_results
                        if rc != 0 or result is None or result.get('response_type') != 'scores']
            for index, batch in enumerate(chunks):
                rc, result, path = batch_results[index]
                expected_ids = {str(job.get('job_id', '')) for job in batch}
                returned_ids = {
                    str(item.get('job_id', ''))
                    for item in (result or {}).get('scores', [])
                    if isinstance(item, dict)
                }
                if rc == 0 and result and result.get('response_type') == 'scores' and not expected_ids <= returned_ids:
                    failures.append((1, None, path))
            failure = next((entry for entry in failures if is_codex_quota_exhausted(entry[2] / 'codex_stdout.log')), None)
            failure = failure or (failures[0] if failures else None)
            if failure:
                returncode, response, actual_run_dir = failure
                if returncode == 0:
                    returncode, response = 1, None
            else:
                returncode, actual_run_dir = 0, run_dir
                response = {'response_type': 'scores', 'event_id': event_id,
                            'scores': [item for _, result, _ in batch_results for item in result.get('scores', [])],
                            'explanation': 'Combined independent AI batches'}
        else:
            returncode, response, actual_run_dir = invoke_codex(incident, args)
        if spec.kind == 'scoring' and response is not None and response.get('response_type') != 'scores':
            returncode, response = 1, None
        if returncode != 0 or response is None:
            quota_exhausted = is_codex_quota_exhausted(actual_run_dir / "codex_stdout.log")
            explanation = (
                "Codex usage limit reached; stop this run and retry after the Codex limit resets"
                if quota_exhausted else codex_failure_explanation(
                    actual_run_dir / "codex_stdout.log", returncode
                )
            )
            write_user_needed(
                platform,
                spec,
                {
                    "event_id": event_id,
                    "questions": [explanation],
                    "explanation": explanation,
                    "retryable": True,
                    "codex_quota_exhausted": quota_exhausted,
                },
                actual_run_dir,
            )
            if spec.kind == 'scoring':
                write_json(DATA_ROOT / platform / spec.output_file,
                           {'scores': {}, 'error': explanation, 'codex_quota_exhausted': quota_exhausted})
                touch(DATA_ROOT / platform / spec.done_flag)
                safe_unlink(flag_path)
            done_marker.write_text("failed: " + utc_now(), encoding="utf-8")
            return True

        apply_response(platform, spec, response, actual_run_dir)
        done_marker.write_text(utc_now(), encoding="utf-8")
        print(f"[codex-supervisor] completed {platform}/{spec.kind}: {event_id}", flush=True)
        return True
    finally:
        args.codex_timeout = old_timeout
        safe_unlink(lock_file)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Codex as the background AI for LinkedIn-AutoApply flags.")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--codex-timeout", type=int, default=260)
    parser.add_argument("--sandbox", default="read-only", choices=["read-only", "workspace-write"])
    parser.add_argument("--config", default="", help="Configuration file used by the AI backend")
    parser.add_argument(
        "--model",
        default="",
        help="Optional model override for the local Codex CLI.",
    )
    parser.add_argument("--once", action="store_true", help="Process at most one polling cycle, then exit.")
    parser.add_argument("--max-events", type=int, default=0, help="Exit after processing this many events; 0 means unlimited.")
    parser.add_argument("--dry-run", action="store_true", help="Build incident files without invoking Codex.")
    args = parser.parse_args()

    global CONFIG_FILE
    if args.config:
        config_path = Path(args.config)
        CONFIG_FILE = config_path if config_path.is_absolute() else PROJECT_ROOT / config_path

    CODEX_ROOT.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60, flush=True)
    print("CODEX SUPERVISOR MODE", flush=True)
    print(f"project: {PROJECT_ROOT}", flush=True)
    print(f"poll_interval: {args.poll_interval}s", flush=True)
    print(f"sandbox: {args.sandbox}", flush=True)
    print(f"model: {CODEX_MODEL}", flush=True)
    print("=" * 60, flush=True)

    processed = 0
    while True:
        events = pending_events()
        if not events:
            if args.once:
                break
            time.sleep(args.poll_interval)
            continue

        for platform, spec, flag_path in events:
            if process_one(platform, spec, flag_path, args):
                processed += 1
                if args.max_events and processed >= args.max_events:
                    return 0

        if args.once:
            break
        time.sleep(args.poll_interval)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
