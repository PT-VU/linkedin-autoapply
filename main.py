#!/usr/bin/env python3
"""
LinkedIn AutoApply v3 — AI 驱动的单页循环架构
重构要点:
  - 删除本地匹配引擎，评分 100% 交给 AI
  - 单页收集 → AI 评分 → 立即投递，无页面刷新
  - 工作记忆 + JD 全景记录 + 关键词库管理
  - External Apply: AI 三段式 (读取→判断→填写)
  - Cover Letter: 按需 AI 生成
"""

import argparse, os, sys, time, subprocess, json, logging, re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import get_cover_letter_dir, get_resume_path, load_config
from src.logger import setup_logger
from src.storage import (
    is_already_applied,
    is_already_skipped,
    is_duplicate_company_title,
    get_cached_external_decision,
    record_external_decision,
    record_skipped,
    save_for_manual_apply,
)
from src.tracker import log_application
from src.job_card_audit import record_card, update_card
from src.resume_parser import load_resume
from src.browser import save_cookies
from src.linkedin_auth import is_logged_in, ensure_logged_in
from src.job_search import build_search_url, collect_job_cards, go_to_next_page
from src.jd_parser import get_full_job_context, open_job_detail, extract_jd_text
from src.agent_match import save_jds_for_review, load_agent_scores, load_agent_scores_with_reasons, normalize_scores, has_scores
from src.easy_apply import is_easy_apply_available, handle_easy_apply_flow, get_cover_letter_status
from src.external_apply import (
    is_external_apply, handle_external_apply_flow, click_external_apply,
    detect_ats_type, extract_external_apply_url, resolve_external_apply_url,
)
from src.external_handoff_queue import save_external_handoff
from src.manual_queue import enqueue_external_job, enqueue_manual_job
from src.external_agent_runner import run_external_agent
from src.cover_letter import generate_cover_letter, save_cover_letter
from src.rate_limit import linkedin_rate_limit
from src.keyword_optimizer import record_keyword_result
from src.job_evaluation import hard_exclusion_reasons


# LinkedIn occasionally ignores a broad country location query and returns a
# neighboring market (for example, Paris when the query says Belgium).  Keep
# the requested-country run safe by rejecting explicit non-Belgian locations at
# collection time.  Empty/remote/region-only locations remain eligible for AI
# review because the card may not expose a country yet.
_BELGIUM_LOCATION_SIGNALS = (
    "belgium", "belgian", "brussels", "antwerp", "anvers", "ghent",
    "gent", "bruges", "brugge", "leuven", "louvain", "liege", "liège",
    "namur", "charleroi", "mechelen", "malines", "hasselt", "kortrijk",
    "courtrai", "mons", "bergen", "aalst", "alost", "genk", "ostend",
    "oostende", "turnhout", "sint-niklaas", "waterloo", "wavre", "wavre",
    "flemish region", "walloon region", "brussels region", "brussels metropolitan",
)
_BELGIUM_NONLOCAL_LOCATION_SIGNALS = (
    "france", "paris", "levallois-perret", "courbevoie",
    "boulogne-billancourt", "neuilly-sur-seine", "saint-denis",
    "nanterre", "clichy", "montreuil", "issy-les-moulineaux", "puteaux",
    "vélizy-villacoublay", "velizy-villacoublay", "la défense", "la defense",
    "st.-cloud", "saint-cloud", "lisses", "clamart", "moissy-cramayel",
    "netherlands", "amsterdam", "rotterdam", "utrecht", "de lier",
    "germany", "berlin", "munich", "luxembourg", "united kingdom",
    "london", "ireland", "dublin",
)


def _is_out_of_scope_location(location: str, requested_location: str) -> bool:
    """Return True only when a requested Belgium run has explicit other-market evidence."""
    if "belg" not in str(requested_location or "").casefold():
        return False
    text = str(location or "").casefold()
    if not text:
        return False
    if any(signal in text for signal in _BELGIUM_NONLOCAL_LOCATION_SIGNALS):
        return True
    # A Belgium run must not queue an arbitrary city returned after LinkedIn's
    # redirect.  Keep genuinely country/region-labelled and explicitly remote
    # cards, while rejecting unknown concrete city strings.
    if any(signal in text for signal in _BELGIUM_LOCATION_SIGNALS):
        return False
    if text.strip() in {"remote", "worldwide", "europe", "european union"}:
        return False
    return True

PROJECT_ROOT = Path(__file__).resolve().parent
DEBUG_PORT = 9222
EDGE_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE_DIR = r"C:\Temp\edge-cdp-profile"
CURRENT_PAGE_JDS = PROJECT_ROOT / "data" / "current_page_jds.json"


class CodexServiceUnavailable(RuntimeError):
    """The background Codex service failed before it could create a letter."""


# The current keyword cannot be safely abandoned when an Easy Apply modal is
# waiting for the user.  This dedicated exit code lets run_all_keywords.py
# stop the outer loop instead of opening the next keyword over that modal.
HUMAN_HANDOFF_EXIT = 74


def is_codex_mode() -> bool:
    return os.environ.get("LINKEDIN_AUTOAPPLY_AI_MODE", "").strip().lower() == "codex"


def skip_human_handoffs_enabled() -> bool:
    """Whether an unattended run should queue and skip human-only forms.

    The default remains the safety-first behavior (pause with exit code 74).
    An overnight run can opt in explicitly; the job is still recorded in the
    human-intervention queue and the live modal is closed before continuing.
    """
    return os.environ.get("LINKEDIN_AUTOAPPLY_SKIP_HUMAN", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _discard_human_handoff(page, logger, reason: str) -> None:
    """Close a queued Easy Apply modal so an unattended run can continue."""
    try:
        from src.easy_apply import close_easy_apply_modal
        close_easy_apply_modal(page, reason=f"auto_skip_human:{reason}")
    except Exception as exc:
        logger.debug("Could not close skipped Easy Apply modal: %s", exc)
    for attr in (
        "_cover_letter_required",
        "_easy_apply_human_intervention_required",
        "_easy_apply_submit_clicked",
    ):
        try:
            setattr(page.context, attr, False)
        except Exception:
            pass
    logger.warning("Unattended mode: queued human-intervention job and continuing (%s)", reason)


def _route_easy_apply_to_manual_queue(
    page,
    job: dict,
    reason: str,
    apply_url: str,
    score,
    logger,
    config=None,
) -> None:
    """Route an Easy Apply form that cannot be completed to the manual list.

    The active workflow no longer has a useful live human-handoff queue.  A
    failed document safety check, missing answer, or other incomplete Easy
    Apply form must instead be reopened later from the durable manual queue.
    We close the modal and clear transient flags so the page loop can continue
    without emitting an alert for a form that is not ready for human action.
    """
    manual_url = apply_url or safe_page_url(page, job.get("link", ""))
    queued_job = {**job, "link": manual_url, "apply_url": manual_url}
    try:
        enqueue_manual_job(
            queued_job,
            score if score is not None else 0,
            reason or "easy_apply_manual_required",
            manual_url,
            str(job.get("source_keyword", "")),
            config,
            decision="easy_apply_manual_required",
        )
    except Exception:
        logger.exception("Could not persist Easy Apply job to the manual queue: %s", job.get("job_id", ""))
    try:
        from src.easy_apply import close_easy_apply_modal
        close_easy_apply_modal(page, reason="route_easy_apply_to_manual_queue")
    except Exception as exc:
        logger.debug("Could not close Easy Apply modal after manual routing: %s", exc)
    for attr in (
        "_cover_letter_required",
        "_easy_apply_human_intervention_required",
        "_easy_apply_submit_clicked",
    ):
        try:
            setattr(page.context, attr, False)
        except Exception:
            pass
    logger.warning(
        "Easy Apply routed to manual queue (job_id=%s, reason=%s)",
        job.get("job_id", ""), reason or "easy_apply_manual_required",
    )


def _route_external_to_manual_queue(
    job: dict,
    score,
    reason: str,
    external_url: str,
    keyword: str,
    config,
    logger,
) -> None:
    """Persist every non-confirmed external attempt for later manual work.

    A CAPTCHA-ready page is also retained in the separate handoff queue, but
    the user-facing manual workbook is the single fallback list for all
    external outcomes that were not confirmed by an employer receipt.
    """
    queue_job = {**job, "apply_url": external_url, "link": job.get("link", "")}
    try:
        enqueue_external_job(
            queue_job,
            score if score is not None else 0,
            reason or "external_apply_unconfirmed",
            external_url,
            keyword,
            config,
        )
    except Exception:
        logger.exception(
            "Could not persist external job to the manual queue: %s",
            job.get("job_id", ""),
        )


def safe_page_url(page, fallback: str = "") -> str:
    try:
        return page.url
    except Exception:
        return fallback


def _manual_external_domain(external_url: str, config: dict) -> str:
    """Return a configured external host that must stay in the manual queue."""
    host = (urlsplit(str(external_url or "").strip()).hostname or "").casefold().strip(".")
    if not host:
        return ""
    configured = (config or {}).get("auto_apply", {}).get("manual_external_domains", [])
    for raw_domain in configured or []:
        domain = str(raw_domain or "").casefold().strip().strip(".")
        if domain and (host == domain or host.endswith("." + domain)):
            return host
    return ""


def _job_id_from_linkedin_url(url: str) -> str:
    match = re.search(r"/jobs/view/(\d+)", url or "")
    return match.group(1) if match else url


def _extract_text_first(page, selectors):
    for selector in selectors:
        try:
            el = page.query_selector(selector)
            if el:
                text = el.inner_text().strip()
                if text:
                    return text
        except Exception:
            continue
    return ""


def _hydrate_job_from_open_page(page, job):
    from src.jd_parser import extract_job_info

    title = job.get("title") or _extract_text_first(page, [
        ".job-details-jobs-unified-top-card__job-title",
        ".jobs-unified-top-card__job-title",
        "h1",
    ])
    info = extract_job_info(page)
    return {
        **job,
        "title": title or job.get("title") or "Unknown",
        "company": job.get("company") or info.get("company") or "Unknown",
        "location": job.get("location") or info.get("location") or "",
        "salary": job.get("salary") or info.get("salary") or "",
        "link": job.get("link") or safe_page_url(page, ""),
    }


def apply_direct_urls(page, urls, profile, resume_path, logger, args, config):
    applied = 0
    cl_dir = get_cover_letter_dir(config)
    score = float(getattr(args, "direct_score", 10.0) or 10.0)

    def recover_closed_page() -> bool:
        """Recreate a tab when LinkedIn closes the CDP target mid-batch.

        LinkedIn occasionally closes the current target after an Easy Apply
        modal is abandoned.  The surrounding browser context can remain
        alive, so recovering a fresh tab is safe and avoids turning every
        remaining direct URL into a misleading TargetClosedError.
        """
        nonlocal page
        try:
            if not page.is_closed():
                return True
        except Exception:
            pass
        try:
            context = page.context
            page = context.new_page()
            page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(1500)
            logger.warning("Recovered a fresh LinkedIn tab after the previous CDP target closed")
            return True
        except Exception as exc:
            logger.error("Could not recover a LinkedIn tab after TargetClosedError: %s", exc)
            return False

    def target_closed(exc: Exception) -> bool:
        return "target page, context or browser has been closed" in str(exc).casefold()

    for idx, url in enumerate(urls, 1):
        url = (url or "").strip()
        if not url:
            continue
        job_id = _job_id_from_linkedin_url(url)
        logger.info(f"=== Direct URL {idx}/{len(urls)}: {url} ===")

        force_direct = bool(getattr(args, "force_direct", False))

        if is_already_applied(job_id):
            logger.info(f"  ⏭ already applied: {job_id}")
            continue
        if not force_direct and is_already_skipped(job_id):
            logger.info(f"  ⏭ already skipped/manual pending: {job_id}")
            continue

        job = {
            "job_id": job_id,
            "title": "Unknown",
            "company": "Unknown",
            "location": "",
            "link": url,
        }
        completed = False
        for attempt in range(1, 3):
            try:
                if not recover_closed_page():
                    raise RuntimeError("LinkedIn tab is unavailable")
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(3000)
                job = _hydrate_job_from_open_page(page, job)

                ok = _apply_single_job(page, job, score, profile, resume_path, cl_dir, logger, args, config=config)
                if ok:
                    applied += 1
                completed = True
                break
            except Exception as e:
                if target_closed(e) and attempt == 1 and recover_closed_page():
                    logger.warning("Retrying direct URL %s after a closed LinkedIn target", job_id)
                    continue
                logger.error(f"  Direct URL failed: {e}", exc_info=True)
                try:
                    save_for_manual_apply(job, "direct_url_failed", safe_page_url(page, url), score=score)
                except Exception:
                    logger.debug("Could not persist direct URL failure for %s", job_id)
                break
        time.sleep(2)

    return applied


def connect_browser(config=None):
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
    linkedin_cfg = ((config or {}).get("linkedin", {}) or {})
    debug_port = int(linkedin_cfg.get("debug_port", os.environ.get("LINKEDIN_DEBUG_PORT", DEBUG_PORT)))
    edge_path = str(linkedin_cfg.get("edge_path") or os.environ.get("LINKEDIN_EDGE_PATH") or EDGE_PATH)
    profile_dir = str(linkedin_cfg.get("profile_path") or os.environ.get("LINKEDIN_EDGE_PROFILE") or PROFILE_DIR)
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    endpoint = f"http://127.0.0.1:{debug_port}"
    try:
        browser = pw.chromium.connect_over_cdp(endpoint)
    except Exception:
        # 使用 shell=True 避免 Git Bash 下 /F 被误解析为路径。
        # Codex 并行模式共享同一个 CDP Edge，子任务不能杀掉整个浏览器。
        if not is_codex_mode():
            subprocess.run("taskkill /F /IM msedge.exe", shell=True, capture_output=True, check=False)
        time.sleep(2)
        subprocess.Popen(
            [edge_path, f"--remote-debugging-port={debug_port}", f"--user-data-dir={profile_dir}",
             "--no-first-run", "--no-default-browser-check"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        time.sleep(4)
        browser = pw.chromium.connect_over_cdp(endpoint)
    # CDP can expose an empty utility context before the profile context. Prefer a
    # context that already has a tab, which also preserves the logged-in session.
    contexts = browser.contexts
    context = next((item for item in contexts if item.pages), contexts[0])
    # 找到已有的 LinkedIn 页面（保留登录态），否则创建新页面并导航
    page = None
    for p in context.pages:
        try:
            if 'linkedin.com' in (p.url or '').lower():
                page = p
                break
        except Exception:
            continue
    if not page:
        if context.pages:
            page = context.pages[0]
        else:
            # Target.createTarget occasionally fails while Edge is still exposing
            # its CDP targets. Retry once instead of failing an entire keyword.
            try:
                page = context.new_page()
            except PlaywrightError:
                time.sleep(2)
                page = context.new_page()
        page.goto("https://www.linkedin.com/jobs/", wait_until="domcontentloaded")
    return pw, browser, context, page


def collect_one_page(page, keyword, location, page_num, logger, config=None, navigate_to_page=True, easy_apply_callback=None, easy_apply_first=False):
    """收集并准备单页中需要评分的岗位。

    In the active page-by-page workflow, Easy Apply cards are deliberately
    deferred until their JD has been read and scored.  ``easy_apply_callback``
    remains accepted for compatibility with the legacy collector, but the
    ``easy_apply_first`` path no longer invokes it before scoring.

    Returns:
        list of job dicts with ``jd_text`` extracted and
        ``application_method`` set where known.
    """
    config = config or {}
    collection_started = time.monotonic()
    if navigate_to_page:
        linkedin_rate_limit(config, "search", logger, f"{keyword} @ {location}")
        url = build_search_url(
            keyword,
            location,
            config.get("search", {}).get("date_posted", ""),
        )
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        logger.info("Search URL loaded with date filter: %s", page.url)

        # 翻到指定页。go_to_next_page 接收的是当前页，而非目标页。
        for current_page in range(1, page_num):
            if not go_to_next_page(page, current_page):
                logger.warning(f"Could not reach page {page_num}")
                return None
            page.wait_for_timeout(2000)

    search_url = page.url  # 记录当前搜索页 URL

    jobs = collect_job_cards(page)
    raw_card_count = len(jobs)
    if "belg" in str(location or "").casefold():
        out_of_scope = [job for job in jobs if _is_out_of_scope_location(job.get("location", ""), location)]
        if out_of_scope:
            logger.info(
                "Belgium location guard removed %s/%s cards with explicit non-Belgian locations",
                len(out_of_scope), raw_card_count,
            )
            jobs = [job for job in jobs if not _is_out_of_scope_location(job.get("location", ""), location)]
    logger.info(f"'{keyword}' Page {page_num}: {len(jobs)} job cards (raw={raw_card_count})")

    if easy_apply_first:
        # Pass 1: inspect application method only.  Easy Apply is not submitted
        # here.  All not-yet-applied cards must first go through JD scoring so
        # the normal workflow respects the Easy Apply score threshold.
        easy_candidates = []
        external_candidates = []
        for job in jobs:
            job_id = str(job.get("job_id", ""))
            job.setdefault("source_keyword", keyword)
            record_card(
                job,
                "LinkedIn indicates this role was already applied"
                if job.get("linkedin_applied")
                else "Collected; awaiting application method",
            )
            if job.get("linkedin_applied"):
                update_card(job, applied=True, reason="LinkedIn already marks this role as Applied")
                continue
            if is_already_applied(job_id, job):
                logger.info("  ⏭ %s — already applied", job.get("title", "?")[:50])
                update_card(job, applied=True, reason="Already recorded as submitted by this workflow")
                continue
            from src.jd_parser import open_job_detail
            linkedin_rate_limit(config, "job_read", logger, f"{job.get('title','?')} @ {job.get('company','?')}")
            if not open_job_detail(page, job, verify_description=False):
                logger.warning("Could not open job to identify application method: %s", job.get("title"))
                continue
            from urllib.parse import parse_qs, urlsplit
            native_id = parse_qs(urlsplit(page.url).query).get("currentJobId", [""])[0]
            resolved_id = native_id if job_id.startswith("li_ai_") and native_id.isdigit() else job_id
            resolved_job = {**job, "job_id": resolved_id, "link": page.url}
            if resolved_id != job_id and is_already_applied(resolved_id, resolved_job):
                logger.info("  Already applied under LinkedIn job ID: %s", resolved_id)
                continue
            if is_easy_apply_available(page):
                resolved_job["application_method"] = "easy_apply"
                easy_candidates.append(resolved_job)
                logger.info(
                    "  Easy Apply detected for %s; deferring submission until JD scoring",
                    job.get("title", "?")[:50],
                )
            else:
                method = "external" if is_external_apply(page) else "unknown"
                resolved_job["application_method"] = method
                if method == "external" and is_already_skipped(resolved_id):
                    logger.info("  ⏭ %s — external job already queued/skipped", job.get("title", "?")[:50])
                elif method == "external" and get_cached_external_decision(resolved_id, resolved_job) is not None:
                    cached = get_cached_external_decision(resolved_id, resolved_job)
                    logger.info("  ⏭ %s — cached external AI decision (score=%s)", job.get("title", "?")[:50], cached.get("score"))
                    update_card(resolved_job, score=cached.get("score"), applied=False, reason=f"Cached external decision: {cached.get('decision', 'skip')}", link=page.url)
                else:
                    external_candidates.append(resolved_job)
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(400)
            except Exception:
                pass

        # Pass 2: read every not-yet-applied JD on this page, including Easy
        # Apply cards.  This is intentionally before any Easy Apply submission.
        result = []
        for job in [*easy_candidates, *external_candidates]:
            if not open_job_detail(page, job, verify_description=False):
                logger.warning("Could not reopen job for JD extraction: %s", job.get("title"))
                continue
            linkedin_rate_limit(config, "job_read", logger, f"{job.get('title','?')} @ {job.get('company','?')}")
            jc = get_full_job_context(page, job, already_open=True)
            if len(str(jc.get("jd_text", "") or "").strip()) < 100:
                logger.info("  ⚠ %s — JD too short", job.get("title", "?")[:50])
                update_card(job, applied=False, reason="Could not read a usable job description for AI scoring")
                continue
            info = jc.get("info", {})
            collected_job = {
                **job,
                "link": page.url,
                "location": job.get("location", "") or info.get("location", ""),
                "salary": job.get("salary_preview", "") or info.get("salary", ""),
                "jd_text": jc.get("jd_text", ""),
                "seniority": info.get("seniority_level", ""),
                "employment_type": info.get("employment_type", ""),
            }
            if _is_out_of_scope_location(collected_job.get("location", ""), location):
                logger.info(
                    "  ⏭ %s — Belgium location guard: %s",
                    collected_job.get("title", "?")[:50],
                    collected_job.get("location", ""),
                )
                update_card(
                    collected_job,
                    applied=False,
                    reason="Excluded: explicit non-Belgian location during Belgium run",
                )
                try:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(300)
                except Exception:
                    pass
                continue
            result.append(collected_job)
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(400)
            except Exception:
                pass
        logger.info(
            "Page collection duration: keyword=%r page=%s cards=%s new_jds=%s elapsed=%.1fs (method pass completed; Easy Apply submission deferred until JD score)",
            keyword, page_num, len(jobs), len(result), time.monotonic() - collection_started,
        )
        return result

    result = []
    for job in jobs:
        job_id = job.get("job_id", "")
        job.setdefault("source_keyword", keyword)
        record_card(job, "LinkedIn indicates this role was already applied" if job.get('linkedin_applied') else "Collected; awaiting application method")

        if job.get('linkedin_applied'):
            update_card(job, applied=True, reason="LinkedIn already marks this role as Applied")
            continue

        # 去重检查
        if is_already_applied(job_id, job):
            logger.info(f"  ⏭ {job.get('title','?')[:50]} — already applied")
            update_card(job, applied=True, reason="Already recorded as submitted by this workflow")
            continue
        # Do not use a historical skip record before identifying the apply
        # method.  An Easy Apply job that previously failed or waited for a
        # human is still eligible for a fresh attempt; only a confirmed
        # submission blocks it.  External jobs are filtered after their detail
        # page is opened, without reading the JD when a cached decision exists.
        detected_application_method = "unknown"
        if easy_apply_callback is not None:
            from src.jd_parser import open_job_detail
            linkedin_rate_limit(config, "job_read", logger, f"{job.get('title','?')} @ {job.get('company','?')}")
            if not open_job_detail(page, job, verify_description=False):
                logger.warning("Could not open job to identify application method: %s", job.get('title'))
                continue
            from urllib.parse import parse_qs, urlsplit
            native_id = parse_qs(urlsplit(page.url).query).get("currentJobId", [""])[0]
            resolved_id = native_id if job_id.startswith('li_ai_') and native_id.isdigit() else job_id
            resolved_job = {**job, "job_id": resolved_id, "link": page.url}
            if resolved_id != job_id and is_already_applied(resolved_id, resolved_job):
                logger.info("  Already applied or pending under LinkedIn job ID: %s", resolved_id)
                continue
            if is_easy_apply_available(page):
                # Easy Apply is intentionally never JD-scored.  A prior failed
                # or human-pending attempt does not suppress a new attempt.
                easy_apply_callback({**resolved_job, 'unscored_easy_apply': True})
                if (
                    getattr(page.context, "_cover_letter_required", False) is True
                    or getattr(page.context, "_easy_apply_human_intervention_required", False) is True
                ):
                    if skip_human_handoffs_enabled():
                        _discard_human_handoff(page, logger, "easy_apply_callback")
                        continue
                    logger.warning(
                        "Easy Apply requires human intervention; stopping page collection "
                        "with the current modal preserved for the handoff queue"
                    )
                    return None
                continue
            detected_application_method = "external" if is_external_apply(page) else "unknown"
            if detected_application_method == "external":
                if is_already_skipped(resolved_id):
                    logger.info("  ⏭ %s — external job already queued/skipped", job.get('title', '?')[:50])
                    update_card(resolved_job, applied=False, reason="External decision already cached/queued", link=page.url)
                    continue
                cached = get_cached_external_decision(resolved_id, resolved_job)
                if cached is not None:
                    logger.info(
                        "  ⏭ %s — cached external AI decision (score=%s; reason=%s)",
                        job.get("title", "?")[:50], cached.get("score"), cached.get("reason", "")[:100],
                    )
                    update_card(
                        resolved_job,
                        score=cached.get("score"),
                        applied=False,
                        reason=f"Cached external decision: {cached.get('decision', 'skip')}",
                        link=page.url,
                    )
                    continue
        # 提取 JD
        linkedin_rate_limit(config, "job_read", logger, f"{job.get('title','?')} @ {job.get('company','?')}")
        # The method-detection branch above already opened this card. Reuse
        # that detail pane instead of clicking the same card and parsing the
        # JD a second time. If the pane did not load a usable JD, the helper's
        # normal path below retries with a fresh card open.
        jc = get_full_job_context(page, job, already_open=True)
        if len(str(jc.get("jd_text", "") or "").strip()) < 100:
            jc = get_full_job_context(page, job)
        jd_text = jc.get("jd_text", "")
        info = jc.get("info", {})

        if len(jd_text.strip()) < 100:
            logger.info(f"  ⚠ {job.get('title','?')[:50]} — JD too short")
            update_card(job, applied=False, reason="Could not read a usable job description for AI scoring")
            continue

        from urllib.parse import parse_qs, urlsplit
        detail_params = parse_qs(urlsplit(page.url).query)
        native_id = detail_params.get("currentJobId", [""])[0]
        resolved_id = native_id if job_id.startswith("li_ai_") and native_id.isdigit() else job_id
        if resolved_id != job_id and is_already_applied(resolved_id, {**job, "job_id": resolved_id, "jd_text": jd_text}):
            logger.info("  Already applied under LinkedIn job ID: %s", resolved_id)
            continue
        if resolved_id != job_id and is_already_skipped(resolved_id):
            logger.info("  Already skipped/manual pending under LinkedIn job ID: %s", resolved_id)
            continue

        collected_job = {
            **job,
            "job_id": resolved_id,
            "link": page.url,
            "title": job.get("title", ""),
            "company": job.get("company", ""),
            "location": job.get("location", "") or info.get("location", ""),
            "salary": job.get("salary_preview", "") or info.get("salary", ""),
            "jd_text": jd_text,
            "seniority": info.get("seniority_level", ""),
            "employment_type": info.get("employment_type", ""),
            "application_method": detected_application_method,
        }
        result.append(collected_job)

        # 不重载搜索页——关闭侧边栏后点击下一个卡片
        # 通过点击搜索页空白区域或按 Escape 关闭侧边栏
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception:
            pass

    logger.info(
        "Page collection duration: keyword=%r page=%s cards=%s new_jds=%s elapsed=%.1fs",
        keyword, page_num, len(jobs), len(result), time.monotonic() - collection_started,
    )
    return result


def collect_up_to_pages(page, keyword, location, start_page, max_pages, logger, config=None, easy_apply_callback=None):
    """Collect consecutive LinkedIn result pages until exhausted or capped."""
    collected = []
    config = config or {}
    linkedin_rate_limit(config, "search", logger, f"{keyword} @ {location}")
    page.goto(
        build_search_url(keyword, location, config.get("search", {}).get("date_posted", "")),
        wait_until="domcontentloaded",
    )
    page.wait_for_timeout(4000)
    logger.info("Search URL loaded with date filter: %s", page.url)

    for current_page in range(1, start_page):
        if not go_to_next_page(page, current_page):
            logger.info("Reached the last available LinkedIn results page at page %s", current_page)
            return collected
        page.wait_for_timeout(2000)

    for page_num in range(start_page, start_page + max_pages):
        jobs = collect_one_page(page, keyword, location, page_num, logger, config, navigate_to_page=False, easy_apply_callback=easy_apply_callback)
        if jobs is None:
            logger.info("Reached the last available LinkedIn results page at page %s", page_num - 1)
            break
        collected.extend(jobs)
        if page_num == start_page + max_pages - 1:
            break
        # A failed Easy Apply dialog can intercept the pagination button.
        # Clean it before asking LinkedIn for the next results page.
        try:
            from src.easy_apply import close_easy_apply_modal
            close_easy_apply_modal(page, reason="before_pagination")
        except Exception as exc:
            logger.debug("Easy Apply pagination cleanup failed: %s", exc)
        if not go_to_next_page(page, page_num):
            logger.info("Reached the last available LinkedIn results page at page %s", page_num)
            break
        page.wait_for_timeout(2000)
    # LinkedIn can surface the same posting on consecutive result pages. Use its
    # resolved job ID only; company/title is not a stable identity for reposts.
    unique = []
    seen_ids = set()
    for job in collected:
        job_id = str(job.get("job_id", "")).strip()
        if job_id and job_id in seen_ids:
            logger.info("  ⏭ duplicate LinkedIn job ID: %s", job_id)
            continue
        if job_id:
            seen_ids.add(job_id)
        unique.append(job)
    return unique


def apply_scored_jobs(page, keyword, location, page_num, profile, resume_path, logger, dry_run=False, config=None):
    """在当前搜索页面上，对 agent_scores.json 中 ≥7 的岗位执行投递。

    页面不刷新，直接在当前搜索结果上操作。
    """
    if not has_scores():
        logger.error("No agent_scores.json found — run --collect first, then AI scoring")
        return 0

    scores = load_agent_scores()
    if not scores:
        logger.error("agent_scores.json is empty")
        return 0

    logger.info(f"Loaded {len(scores)} AI scores")

    config = config or load_config()

    # 导航到搜索页.  Include the configured date window in the URL so the
    # legacy entry point follows the same 24-hour restriction as --run.
    url = build_search_url(keyword, location, config.get("search", {}).get("date_posted", ""))
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)

    # 翻到指定页
    if page_num > 1:
        for _ in range(page_num - 1):
            go_to_next_page(page, page_num)
            page.wait_for_timeout(2000)

    search_url = page.url
    threshold = float(config.get("matching", {}).get("threshold", 6.5))
    applied = 0

    # 收集当前页岗位卡片
    jobs = collect_job_cards(page)

    for job in jobs:
        job_id = job.get("job_id", "")
        if is_already_applied(job_id, job):
            continue
        company = job.get("company", "Unknown")
        title = job.get("title", "Unknown")

        # Keep the legacy --collect/--apply-page entry point consistent with
        # the normal --run architecture: Easy Apply is score-gated before any
        # submission.  A missing score is a safe skip, never an unscored
        # application.
        if not open_job_detail(page, job, verify_description=False):
            logger.warning("  Could not open detail: %s", title[:50])
            continue
        explicit_exclusions = hard_exclusion_reasons(job)
        if explicit_exclusions:
            reason = "; ".join(explicit_exclusions)
            logger.info("  ⏭ %s — explicit user exclusion: %s", title[:50], reason)
            record_skipped(job, "explicit_user_exclusion")
            continue
        score = scores.get(job_id)
        if is_easy_apply_available(page):
            easy_threshold = float(config.get("matching", {}).get("easy_apply_threshold", 5.0))
            if score is None or score < easy_threshold:
                logger.info(
                    "  ⏭ %s — Easy Apply score %s below threshold %.1f",
                    title[:50], "missing" if score is None else f"{float(score):.1f}", easy_threshold,
                )
                continue
            logger.info("\n  🎯 %s @ %s | Easy Apply AI score: %.1f/10", title[:50], company, float(score))
            easy_job = {**job, "jd_text": str(job.get("jd_text", "") or ""), "link": safe_page_url(page, job.get("link", ""))}
            try:
                success = True if dry_run else _apply_single_job(
                    page, easy_job, score, profile, resume_path, get_cover_letter_dir(config),
                    logger, argparse.Namespace(dry_run=dry_run), already_open=True, config=config,
                )
            except Exception as exc:
                logger.warning("  Easy Apply failed: %s", exc)
                success = False
            if success and not dry_run:
                applied += 1
            # A failed Easy Apply is intentionally left for the human queue;
            # it must not be converted into a score-based skip.
            if (
                getattr(page.context, "_cover_letter_required", False)
                or getattr(page.context, "_easy_apply_human_intervention_required", False)
            ):
                logger.warning("  Easy Apply handoff is active; pausing the legacy apply-page loop")
                return applied
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
            except Exception:
                pass
            continue

        if score is None or score < threshold:
            continue
        if is_already_skipped(job_id):
            continue
        logger.info(f"\n  🎯 {title[:50]} @ {company} | AI score: {score}/10")

        # Detail is already open for method detection. Reuse it for external
        # JD extraction instead of reopening the card.
        jc = get_full_job_context(page, job, already_open=True)
        jd_text = jc.get("jd_text", "")
        info = jc.get("info", {})

        if len(jd_text.strip()) < 100:
            logger.warning("  JD too short — skip")
            page.goto(search_url, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            continue
        detail_url = safe_page_url(page, search_url)

        easy_available = is_easy_apply_available(page)
        # Easy Apply handles its own optional/required cover-letter path.  The
        # update2 external agent reads the live form and candidate context; a
        # required external cover-letter upload that it cannot complete is
        # deliberately treated as an external failure and routed to the
        # manual queue rather than spending a separate generation request here.
        cl_path = ""

        # 执行投递
        success = False
        method = "none"
        cover_letter_status = "not applicable"
        external_metadata = {}

        if easy_available:
            logger.info("  Easy Apply...")
            if dry_run:
                logger.info(f"  [DRY RUN] Would Easy Apply")
                success = True; method = "easy_apply"
            else:
                success = handle_easy_apply_flow(
                    page, profile, str(resume_path), cl_path,
                    job_context=job,
                )
                method = "easy_apply"
                # A required cover letter may be generated inside the live
                # Easy Apply modal.  The helper stores the resulting DOCX on
                # the page context so the ledger records the actual file,
                # rather than the initially empty `cl_path`.
                generated_cl_path = str(
                    getattr(page.context, "_easy_apply_cover_letter_path", "") or ""
                ).strip()
                if generated_cl_path:
                    cl_path = generated_cl_path
                cover_letter_status = get_cover_letter_status(page)
                if (
                    getattr(page.context, '_cover_letter_required', False) is True
                    or getattr(page.context, '_easy_apply_human_intervention_required', False) is True
                ):
                    reason = (
                        "easy_apply_cover_letter_required_human_intervention"
                        if getattr(page.context, '_cover_letter_required', False) is True
                        else "easy_apply_ai_needs_user_human_intervention"
                    )
                    _route_easy_apply_to_manual_queue(
                        page, job, reason, detail_url, score, logger, config,
                    )
                    record_skipped(job, f"manual_pending: {reason}")
                    update_card(job, score=score, applied=False, reason=reason, link=detail_url)
                    continue

        elif is_external_apply(page):
            method = "external_apply"
            queue_job = {**job, "jd_text": jd_text, "source_keyword": keyword, "link": detail_url}
            external_url = resolve_external_apply_url(page, logger)
            manual_domain = _manual_external_domain(external_url, config)
            if manual_domain:
                queue_reason = f"manual_external_domain:{manual_domain}"
                logger.info("  External domain %s is configured for manual application; skipping AI agent", manual_domain)
                if not dry_run:
                    _route_external_to_manual_queue(
                        queue_job, score, queue_reason, external_url, keyword, config, logger,
                    )
                    record_skipped(queue_job, queue_reason)
                    update_card(queue_job, score=score, applied=False, reason=queue_reason, link=detail_url)
                continue
            if not bool(config.get("auto_apply", {}).get("external_apply", False)):
                queue_reason = "AI评分达标；外部自动投递未启用，转入手动投递队列"
                if dry_run:
                    logger.info("  [DRY RUN] Would queue eligible external job for manual application")
                else:
                    _route_external_to_manual_queue(
                        queue_job, score, queue_reason, external_url, keyword, config, logger,
                    )
                    record_skipped(queue_job, "external_manual_queue")
                    update_card(queue_job, score=score, applied=False, reason=queue_reason, link=detail_url)
                continue
            if not external_url:
                success = False
                external_metadata["handoff_reason"] = "external_apply_url_missing"
                logger.warning("  External apply URL could not be read; routing to manual queue")
            elif dry_run:
                logger.info("  [DRY RUN] Would run the update2 Edge/Codex external agent")
                success = True
            else:
                logger.info("  External Apply via update2 Edge/Codex agent (score %.1f)...", float(score))
                result = run_external_agent(queue_job, external_url, float(score), config, logger)
                success = result.success
                detail_url = external_url
                external_metadata.update({
                    "handoff_required": result.handoff_required,
                    "handoff_reason": result.reason,
                    "failure_code": result.failure_code,
                    "run_dir": result.run_dir,
                })
        else:
            logger.warning("  No apply button found")
            record_skipped(job, "no_apply_button")
            save_for_manual_apply(job, "no_apply_button", safe_page_url(page, detail_url), score=score)

        if success and not dry_run:
            logger.info(f"  ✅ Submitted via {method}!")
            # The update2 external runner writes the confirmed submission to
            # the ledger itself.  Avoid appending a duplicate row when the
            # parent LinkedIn process resumes after the subprocess exits.
            if not is_already_applied(job_id, job):
                log_application(
                    job_id=job_id, company=company or info.get("company", "Unknown"),
                    title=title, match_score=score, apply_method=method,
                    jd_summary=jd_text[:500],
                    salary=info.get("salary", "") or job.get("salary_preview", ""),
                    location=info.get("location", "") or job.get("location", ""),
                    responsibilities_summary=jd_text[:200],
                    cover_letter_path=cl_path, cover_letter_status=cover_letter_status, status="Submitted",
                    jd_text=jd_text, link=detail_url,
                )
            update_card(job, score=score, applied=True, reason=f"Submitted via {method}", link=detail_url)
            applied += 1
        elif not success:
            logger.warning(f"  Failed")
            failure_reason = external_metadata.get("handoff_reason", "apply_failed")
            record_skipped(job, failure_reason)
            if method == "easy_apply":
                # The active workflow routes incomplete Easy Apply forms to
                # the durable manual queue and closes the transient modal.
                _route_easy_apply_to_manual_queue(
                    page, job, failure_reason, detail_url, score, logger, config,
                )
                record_skipped(job, f"manual_pending: {failure_reason}")
            elif method == "external_apply":
                # A visible CAPTCHA may also have a retained Edge page in the
                # separate handoff queue, but every non-confirmed external
                # result must be actionable from the user-facing manual list.
                _route_external_to_manual_queue(
                    {**job, "jd_text": jd_text, "link": job.get("link", "")},
                    score,
                    failure_reason,
                    detail_url,
                    keyword,
                    config,
                    logger,
                )
                if external_metadata.get("handoff_required"):
                    save_external_handoff(
                        job, failure_reason, detail_url, score=score, source="linkedin", notify=False,
                    )
            else:
                # Preserve the captured ATS URL for ordinary manual follow-up.
                # ``page`` is still the LinkedIn detail tab after the popup
                # closes; using safe_page_url(page, ...) here silently sent
                # missing-answer cases back to LinkedIn instead of the form.
                manual_url = detail_url if method == "external_apply" else safe_page_url(page, detail_url)
                save_for_manual_apply(job, failure_reason, manual_url, score=score)

        if (
            method == "easy_apply" and not success and
            (getattr(page.context, "_cover_letter_required", False)
             or getattr(page.context, "_easy_apply_human_intervention_required", False))
        ):
            logger.warning("Stopping scored-page application: Easy Apply handoff form remains visible")
            return applied

        # 关闭侧边栏，回到卡片列表（不重载搜索页）
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception:
            pass
        time.sleep(0.5)

    return applied


def _score_and_apply_external_page(page, keyword, jobs, profile, resume_path, logger, args, config, easy_stats=None):
    """Score and process one result page before pagination continues.

    Both Easy Apply and external jobs are scored here. Easy Apply uses its own
    lower application threshold; external jobs retain the configured external
    threshold and queue behavior. Keeping this boundary at one page makes the
    score-before-submit decision observable and prevents cross-page state leak.
    """
    if not jobs:
        logger.info("No scoreable jobs on this page — continuing to next page")
        return 0, False

    save_jds_for_review(jobs, "")
    logger.info("Saved %s Easy Apply/external JDs for this page.", len(jobs))
    li_dir = PROJECT_ROOT / "data" / "linkedin"
    li_dir.mkdir(parents=True, exist_ok=True)
    scoring_flag = li_dir / "scoring_needed.flag"
    scoring_done = li_dir / "scoring_done.flag"
    for stale in (scoring_flag, scoring_done):
        if stale.exists():
            stale.unlink()
    scoring_flag.touch()
    logger.info("Waiting for Agent to score %s external JDs on this page...", len(jobs))

    started = time.monotonic()
    last_logged = 0
    while not scoring_done.exists():
        time.sleep(1)
        elapsed = int(time.monotonic() - started)
        if elapsed >= last_logged + 30:
            logger.info("   ...waiting for Agent scoring (%ss)", elapsed)
            last_logged = elapsed
        if elapsed > 900:
            logger.error("AI scoring timed out; stopping without rule-based scoring")
            scoring_flag.unlink(missing_ok=True)
            raise SystemExit(1)

    score_output = li_dir / "agent_scores.json"
    if score_output.exists():
        metadata = json.loads(score_output.read_text(encoding="utf-8"))
        if metadata.get("error"):
            logger.error("AI scoring failed: %s", metadata["error"])
            raise SystemExit(75 if metadata.get("codex_quota_exhausted") else 1)
    scores = load_agent_scores()
    scoring_flag.unlink(missing_ok=True)
    expected = {str(job.get("job_id", "")) for job in jobs}
    if not scores or set(scores) != expected:
        logger.error("AI scores incomplete or invalid for this page; stopping safely")
        raise SystemExit(1)

    threshold = float(config.get("matching", {}).get("threshold", 6.5))
    easy_threshold = float(config.get("matching", {}).get("easy_apply_threshold", 5.0))
    details = load_agent_scores_with_reasons() or {}
    for job in jobs:
        job_id = str(job.get("job_id", ""))
        detail = details.get(job_id, {})
        score = detail.get("score", scores.get(job_id, 0))
        decision = detail.get("decision", "")
        reason = detail.get("reason", "") or (
            "AI score is below the application threshold"
            if score < threshold else "AI marked role eligible; awaiting application"
        )
        update_card(job, score=score, applied=False if score < threshold or decision in {"skip", "manual_review"} else None, reason=reason)
        if job.get("application_method") == "external":
            record_external_decision(job, score, decision, reason, threshold)

    def _job_threshold(job):
        return easy_threshold if job.get("application_method") == "easy_apply" else threshold

    def _passes_application_gate(job, score):
        # AI remains the scorer; these explicit user constraints are a
        # deterministic safety gate against malformed/over-generous scores.
        if hard_exclusion_reasons(job):
            return False
        if score < _job_threshold(job):
            return False
        detail = details.get(str(job.get("job_id", "")), {})
        # Protect the lower Easy Apply threshold from a malformed model result
        # that reports a high numeric score together with an explicit blocker.
        return str(detail.get("eligibility", "pass")).strip().lower() != "fail"

    eligible = sum(
        1 for job in jobs
        if _passes_application_gate(job, scores.get(str(job.get("job_id", "")), 0.0))
    )
    logger.info(
        "Agent scored: %s/%s jobs at application thresholds (Easy Apply >= %.1f; external >= %.1f)",
        eligible, len(scores), easy_threshold, threshold,
    )
    try:
        # Keyword replacement feedback is based on external roles, preserving
        # the historical 6.5 external threshold. Easy Apply is now scored for
        # safety, but should not distort external keyword quality statistics.
        external_jobs = [job for job in jobs if job.get("application_method") != "easy_apply"]
        external_ids = {str(job.get("job_id", "")) for job in external_jobs}
        external_scores = {jid: scores[jid] for jid in external_ids if jid in scores}
        if external_jobs:
            state = record_keyword_result("linkedin", keyword, external_jobs, external_scores, threshold, config)
            logger.info("Keyword feedback recorded; active keywords=%s", len(state.get("active_keywords", [])))
    except Exception as exc:
        logger.warning("Keyword feedback failed: %s", exc)

    applied = 0
    for score, job in sorted(((scores.get(str(j.get("job_id", "")), 0), j) for j in jobs), reverse=True, key=lambda item: item[0]):
        job_threshold = _job_threshold(job)
        is_easy_apply = job.get("application_method") == "easy_apply"
        detail = details.get(str(job.get("job_id", "")), {})
        explicit_exclusions = hard_exclusion_reasons(job)
        if explicit_exclusions:
            reason = "; ".join(explicit_exclusions)
            logger.info("  ⏭ %s — explicit user exclusion: %s", job.get("title", "?")[:50], reason)
            record_skipped(job, "explicit_user_exclusion")
            update_card(job, score=score, applied=False, reason=reason, link=job.get("link", ""))
            continue
        if str(detail.get("eligibility", "pass")).strip().lower() == "fail":
            if is_easy_apply:
                reason = "Easy Apply blocked by an explicit JD eligibility failure"
                logger.info("  ⏭ %s — %s", job.get("title", "?")[:50], reason)
                record_skipped(job, "easy_apply_explicit_eligibility_blocker")
                update_card(job, score=score, applied=False, reason=reason, link=job.get("link", ""))
            continue
        if score < job_threshold:
            if is_easy_apply:
                reason = f"Easy Apply AI score {float(score):.1f} below threshold {easy_threshold:.1f}"
                logger.info("  ⏭ %s — %s", job.get("title", "?")[:50], reason)
                record_skipped(job, "easy_apply_score_below_threshold")
                update_card(job, score=score, applied=False, reason=reason, link=job.get("link", ""))
            continue
        job_id = str(job.get("job_id", ""))
        # A previous Easy Apply failure is not a confirmed submission and must
        # not suppress a later score-qualified retry. External jobs retain the
        # existing queued/skip de-duplication behavior.
        if is_already_applied(job_id, job) or (not is_easy_apply and is_already_skipped(job_id)):
            logger.info("  ⏭ %s — already recorded/queued", job.get("title", "?")[:50])
            continue
        if is_easy_apply and easy_stats is not None:
            easy_stats["attempted"] = int(easy_stats.get("attempted", 0)) + 1
        try:
            success = _apply_single_job(
                page, job, score, profile, resume_path,
                get_cover_letter_dir(config), logger, args,
                config=config,
            )
            if success:
                applied += 1
                if is_easy_apply and easy_stats is not None:
                    easy_stats["submitted"] = int(easy_stats.get("submitted", 0)) + 1
        except CodexServiceUnavailable as exc:
            logger.error("Codex unavailable; stopping without marking this job manual: %s", exc)
            raise SystemExit(75)
        except Exception as exc:
            logger.exception("Page application failed for %s: %s", job.get("title", "?"), exc)
        if getattr(page.context, "_easy_apply_human_intervention_required", False):
            if skip_human_handoffs_enabled():
                _discard_human_handoff(page, logger, "scored_page")
                continue
            logger.warning("Easy Apply handoff active; pausing before next page")
            return applied, True

    from src.agent_match import generate_jd_review_md
    generate_jd_review_md(jobs, scores)
    logger.info("Page complete: applied=%s/%s eligible at method-specific thresholds", applied, eligible)
    return applied, False



def _prepare_application_cover_letter(job, jd_text, profile, logger):
    from src.cover_letter_agent import request_and_wait_cover_letter

    if not is_codex_mode():
        logger.warning("Cover letter generation requires Codex mode")
        return ""
    result = request_and_wait_cover_letter(
        platform="linkedin", profile=profile, jd_text=jd_text,
        job_title=job.get("title", ""), company=job.get("company", ""),
        job_id=str(job.get("job_id", "")),
    )
    if result and result.get("codex_quota_exhausted"):
        raise CodexServiceUnavailable("Codex usage limit reached; retry after the Codex limit resets")
    if result and result.get("retryable"):
        raise CodexServiceUnavailable("Codex did not complete the cover-letter request; retry this run later")
    if not result or result.get("needs_user"):
        return ""
    path = str(result.get("cover_letter_docx_path") or "").strip()
    if not path or not Path(path).is_file():
        logger.warning("No generated cover letter file available")
        return ""
    logger.info("  Tailored cover letter ready: %s", path)
    return path


def _apply_single_job(page, job, score, profile, resume_path, cl_dir, logger, args, already_open=False, config=None):
    """处理单个岗位投递。任何异常上抛由调用方 try/except 捕获。"""
    from src.jd_parser import open_job_detail, extract_jd_text
    from src.easy_apply import (
        is_easy_apply_available, handle_easy_apply_flow, get_cover_letter_status,
        close_easy_apply_modal,
    )
    from src.external_apply import (
        is_external_apply, handle_external_apply_flow, click_external_apply,
        detect_ats_type, extract_external_apply_url, resolve_external_apply_url,
    )
    config = config or load_config()

    job_id = job.get("job_id", "")
    company = job.get("company", "?")
    title = job.get("title", "?")

    logger.info("\n  🎯 %s @ %s | %s", title[:50], company, f"AI: {score}/10" if score is not None else "Easy Apply, unscored")

    if not already_open and not open_job_detail(page, job):
        logger.warning("  Could not open detail")
        return False

    # The active workflow already extracted and scored the JD before calling
    # this function. Reuse that text to avoid a second page parse. The legacy
    # explicit ``unscored_easy_apply`` mode remains compatible for callers that
    # deliberately opt into it.
    jd_text = str(job.get("jd_text", "") or "").strip()
    if not jd_text and not job.get('unscored_easy_apply'):
        jd_text = extract_jd_text(page)
    if not job.get('unscored_easy_apply') and len(jd_text.strip()) < 100:
        logger.warning("  JD too short")
        return False
    detail_url = safe_page_url(page, "")

    easy_available = is_easy_apply_available(page)
    # External forms request a cover letter only after they expose a matching
    # field; their AI path then reads the needed context and generates it.
    cl_path = ""

    success = False
    method = "none"
    cover_letter_status = "not applicable"
    external_metadata = {}
    if easy_available:
        logger.info("  Easy Apply...")
        if args.dry_run:
            success = True; method = "easy_apply"
        else:
            # Optional cover-letter fields are skipped.  If the field is
            # explicitly required, Easy Apply now requests a role-specific
            # DOCX from the Codex cover-letter worker and uploads it in-place.
            success = handle_easy_apply_flow(
                page, profile, str(resume_path), cl_path,
                job_context=job,
            )
            method = "easy_apply"
            generated_cl_path = str(
                getattr(page.context, "_easy_apply_cover_letter_path", "") or ""
            ).strip()
            if generated_cl_path:
                cl_path = generated_cl_path
            cover_letter_status = get_cover_letter_status(page)
            if (
                getattr(page.context, '_cover_letter_required', False) is True
                or getattr(page.context, '_easy_apply_human_intervention_required', False) is True
            ):
                reason = (
                    "easy_apply_cover_letter_required_human_intervention"
                    if getattr(page.context, '_cover_letter_required', False) is True
                    else "easy_apply_ai_needs_user_human_intervention"
                )
                _route_easy_apply_to_manual_queue(
                    page, job, reason, detail_url, score, logger, config,
                )
                record_skipped(job, f"manual_pending: {reason}")
                update_card(job, score=score, applied=False, reason=reason, link=detail_url)
                return False
    elif job.get('unscored_easy_apply'):
        logger.warning("  Easy Apply is no longer available; not applying externally without AI scoring")
        return False
    elif is_external_apply(page):
        method = "external_apply"
        external_url = resolve_external_apply_url(page, logger)
        manual_domain = _manual_external_domain(external_url, config)
        if manual_domain:
            queue_reason = f"manual_external_domain:{manual_domain}"
            logger.info("  External domain %s is configured for manual application; skipping AI agent", manual_domain)
            if not args.dry_run:
                _route_external_to_manual_queue(
                    job, score, queue_reason, external_url,
                    str(job.get("source_keyword", "")), config, logger,
                )
                record_skipped(job, queue_reason)
                update_card(job, score=score, applied=False, reason=queue_reason, link=detail_url)
            return False
        if not bool(config.get("auto_apply", {}).get("external_apply", False)):
            queue_reason = "AI评分达标；外部自动投递未启用，转入手动投递队列"
            if args.dry_run:
                logger.info("  [DRY RUN] Would queue eligible external job for manual application")
            else:
                _route_external_to_manual_queue(
                    job, score, queue_reason, external_url,
                    str(job.get("source_keyword", "")), config, logger,
                )
                record_skipped(job, "external_manual_queue")
                update_card(job, score=score, applied=False, reason=queue_reason, link=detail_url)
            return False
        if not external_url:
            external_metadata["handoff_reason"] = "external_apply_url_missing"
            success = False
            logger.warning("  External apply URL could not be read; routing to manual queue")
        elif args.dry_run:
            logger.info("  [DRY RUN] Would run the update2 Edge/Codex external agent")
            success = True
        else:
            logger.info("  External Apply via update2 Edge/Codex agent (score %.1f)...", float(score))
            result = run_external_agent(job, external_url, float(score), config, logger)
            success = result.success
            detail_url = external_url
            external_metadata.update({
                "handoff_required": result.handoff_required,
                "handoff_reason": result.reason,
                "failure_code": result.failure_code,
                "run_dir": result.run_dir,
            })
    else:
        logger.warning("  No apply button")
        save_for_manual_apply(job, "no_apply_button", safe_page_url(page, detail_url), score=score)
        return False

    if success and not args.dry_run:
        logger.info(f"  ✅ Submitted via {method}!")
        if not is_already_applied(job_id, job):
            log_application(
                job_id=job_id, company=company, title=title,
                match_score=score, apply_method=method,
                jd_summary=jd_text[:500],
                salary=job.get("salary", ""), location=job.get("location", ""),
                responsibilities_summary=jd_text[:200],
                cover_letter_path=cl_path, cover_letter_status=cover_letter_status, status="Submitted",
                jd_text=jd_text, link=detail_url,
            )
        update_card(job, score=score, applied=True, reason=f"Submitted via {method}", link=detail_url)
        return True
    elif success:
        logger.info(f"  [DRY RUN] Would submit via {method}; not recorded as an application")
        return True
    elif not success:
        logger.warning("  Failed")
        failure_reason = external_metadata.get(
            "handoff_reason", "external_apply_failed" if method == "external_apply" else "easy_apply_failed"
        )
        if method == "external_apply":
            _route_external_to_manual_queue(
                job, score, failure_reason, detail_url,
                str(job.get("source_keyword", "")), config, logger,
            )
            if external_metadata.get("handoff_required"):
                save_external_handoff(
                    job, failure_reason, detail_url, score=score, source="linkedin", notify=False,
                )
        elif method == "easy_apply":
            # Easy Apply failures belong to the same durable manual queue as
            # external links; no stale live human-handoff modal is emitted.
            _route_easy_apply_to_manual_queue(
                page, job, failure_reason, safe_page_url(page, detail_url), score, logger, config,
            )
            record_skipped(job, f"manual_pending: {failure_reason}")
        else:
            manual_url = detail_url if method == "external_apply" else safe_page_url(page, detail_url)
            save_for_manual_apply(job, failure_reason, manual_url, score=score)
        update_card(job, score=score, applied=False, reason=failure_reason, link=safe_page_url(page, detail_url))
        return False


def main():
    parser = argparse.ArgumentParser(description="LinkedIn AI Job AutoApply v3")
    parser.add_argument("--collect", action="store_true",
                        help="Collect JDs from one page for AI scoring")
    parser.add_argument("--apply-page", action="store_true",
                        help="Apply scored jobs on the current search page")
    parser.add_argument("--run", action="store_true",
                        help="Single-pass: collect → wait for AI scores → apply (one process)")
    parser.add_argument("--keyword", default=None, help="Search keyword")
    parser.add_argument("--location", default=None, help="Search location override")
    parser.add_argument(
        "--date-posted",
        choices=["Any Time", "Past 24 hours", "Past Week", "Past 2 weeks", "Past Month"],
        default=None,
        help="Search date window override; omitted means use config.yaml",
    )
    parser.add_argument("--page", type=int, default=1, help="Page number")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Maximum consecutive search pages for --run; 0 means all available pages",
    )
    parser.add_argument("--apply-urls", nargs="*", default=None,
                        help="Apply directly to one or more LinkedIn job URLs")
    parser.add_argument("--apply-urls-file", default=None,
                        help="Text file containing one LinkedIn job URL per line")
    parser.add_argument("--direct-score", type=float, default=10.0,
                        help="Score to record for direct URL applications")
    parser.add_argument("--force-direct", action="store_true",
                        help="Retry direct URLs even if they are already in skipped/manual-pending records")
    parser.add_argument("--direct-start-index", type=int, default=0,
                        help="Skip this many entries from --apply-urls/--apply-urls-file (for resuming a batch)")
    parser.add_argument("--skip-direct-job-ids", nargs="*", default=[],
                        help="Skip direct LinkedIn URLs whose /jobs/view/<id>/ matches one of these IDs")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    if args.max_pages < 0:
        parser.error("--max-pages must be 0 or greater")

    config = load_config(args.config)
    if args.date_posted is not None:
        config.setdefault("search", {})["date_posted"] = args.date_posted
    logger = setup_logger(level=config.get("logging", {}).get("level", "INFO"))

    logger.info(f"LinkedIn AutoApply v3 — {datetime.now().isoformat()}")

    # Load config
    location = args.location or config.get("search", {}).get("location", "")
    keyword = args.keyword or config.get("search", {}).get("keywords", ["AI Engineer"])[0]
    if isinstance(keyword, list): keyword = keyword[0]

    profile = config.get("profile", {})
    resume_path = get_resume_path(config)

    # Connect browser
    pw, browser, context, page = connect_browser(config)

    try:
        # Login
        auth_cfg = ((config.get("linkedin", {}) or {}).get("auth", {}) or {})
        success_url = str(auth_cfg.get("success_url") or "https://www.linkedin.com/feed/")
        page.goto(success_url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        if not is_logged_in(page):
            if not bool(auth_cfg.get("interactive_login", True)):
                logger.error("No active LinkedIn session and interactive_login is disabled")
                return 2
            timeout = int(auth_cfg.get("login_timeout_seconds", 300) or 300)
            logger.info("No active LinkedIn session — waiting up to %ss for first-run login", timeout)
            if not ensure_logged_in(page, timeout_seconds=timeout, config=config):
                logger.error("LinkedIn login was not completed before the timeout")
                return 2
        logger.info("Logged in!")

        direct_urls = list(args.apply_urls or [])
        if args.apply_urls_file:
            with open(args.apply_urls_file, "r", encoding="utf-8") as f:
                direct_urls.extend(line.strip() for line in f if line.strip())

        if args.direct_start_index:
            start = max(0, int(args.direct_start_index))
            direct_urls = direct_urls[start:]
            logger.info("Direct URL resume: skipped %s already-processed entries", start)

        skip_ids = {str(value).strip() for value in (args.skip_direct_job_ids or []) if str(value).strip()}
        if skip_ids:
            before = len(direct_urls)
            direct_urls = [url for url in direct_urls
                           if not any(f"/jobs/view/{job_id}/" in url for job_id in skip_ids)]
            logger.info("Direct URL skip list: removed %s already-confirmed job(s)", before - len(direct_urls))

        if direct_urls:
            applied = apply_direct_urls(page, direct_urls, profile, resume_path, logger, args, config)
            logger.info(f"Direct URL applications submitted: {applied}/{len(direct_urls)}")

        elif args.run:
            # ── 单次运行: 收集 → 等待 AI 评分 → 投递 (同一进程，同一页面) ──
            logger.info(f"=== SINGLE-PASS RUN: '{keyword}' page {args.page} ===")

            # ── Phase 0: 清理所有旧 flag (防止跨运行污染) (P0-1) ──
            logger.info("Phase 0: Cleaning stale flags...")
            data_dir = PROJECT_ROOT / "data" / "linkedin"
            data_dir.mkdir(parents=True, exist_ok=True)
            for stale in ["form_help_needed.flag", "form_help_done.flag",
                          "form_help.json", "form_instructions.json",
                          "ext_help_needed.flag", "ext_help_done.flag",
                          "ext_help.json", "ext_instructions.json",
                          "scoring_needed.flag", "scoring_done.flag",
                          "cover_letter_needed.flag", "cover_letter_done.flag",
                          "cover_letter_context.json", "cover_letter_instructions.json"]:
                fp = data_dir / stale
                if fp.exists():
                    fp.unlink()
                    logger.info(f"  Cleaned: {stale}")

            # Phase 1: 收集 JD（保持在搜索页，不离开）
            page_limit = None if args.max_pages == 0 else args.max_pages
            page_limit_label = "all available" if page_limit is None else str(page_limit)
            logger.info("Phase 1: Collecting up to %s pages...", page_limit_label)

            # Page-by-page pipeline: collect and score this page's Easy Apply
            # and external JDs first, then submit only score-qualified Easy
            # Apply roles before moving to the next page.
            page_easy_totals = {'attempted': 0, 'submitted': 0}

            offset = 0
            seen_page_signatures = set()
            while page_limit is None or offset < page_limit:
                page_num = args.page + offset
                page_total_label = "all available" if page_limit is None else str(args.page + page_limit - 1)
                logger.info("--- PAGE %s/%s: Easy Apply then external JD scoring ---", page_num, page_total_label)
                jobs = collect_one_page(
                    page, keyword, location, page_num, logger, config,
                    navigate_to_page=(offset == 0),
                    easy_apply_callback=None,
                    easy_apply_first=True,
                )
                if jobs is None:
                    if skip_human_handoffs_enabled() and getattr(context, '_easy_apply_human_intervention_required', False):
                        _discard_human_handoff(page, logger, "page_collection")
                        jobs = []
                    else:
                        logger.warning("Page %s stopped because Easy Apply needs human intervention or page navigation failed", page_num)
                        raise SystemExit(HUMAN_HANDOFF_EXIT if getattr(context, '_easy_apply_human_intervention_required', False) else 1)

                # LinkedIn can expose a clickable pagination control even after
                # the result list is empty (especially after an SPA redirect).
                # An empty page is the end of this keyword's usable result set;
                # continuing to page 5 only repeats empty DOM scans.
                if not jobs:
                    logger.info("No job cards on page %s; stopping pagination for this keyword", page_num)
                    break

                # In unbounded mode, stop if LinkedIn serves the same result
                # page again instead of advancing. This prevents an SPA
                # pagination loop while still allowing every distinct page.
                page_signature = tuple(
                    str(job.get("job_id") or job.get("link") or "").strip()
                    for job in jobs
                )
                if page_signature and page_signature in seen_page_signatures:
                    logger.warning("Repeated LinkedIn result page detected at page %s; stopping pagination", page_num)
                    break
                if page_signature:
                    seen_page_signatures.add(page_signature)

                applied_on_page, handoff = _score_and_apply_external_page(
                    page, keyword, jobs, profile, resume_path, logger, args, config,
                    easy_stats=page_easy_totals,
                )
                logger.info(
                    "Page %s summary: Easy Apply %s submitted/%s score-qualified attempts; external applied/queued phase complete (%s direct successes)",
                    page_num, page_easy_totals['submitted'], page_easy_totals['attempted'], applied_on_page,
                )
                if handoff:
                    if skip_human_handoffs_enabled():
                        _discard_human_handoff(page, logger, "page_scoring")
                    else:
                        raise SystemExit(HUMAN_HANDOFF_EXIT)
                if page_limit is not None and offset >= page_limit - 1:
                    break
                try:
                    from src.easy_apply import close_easy_apply_modal
                    close_easy_apply_modal(page, reason="before_pagination")
                except Exception as exc:
                    logger.debug("Easy Apply pagination cleanup failed: %s", exc)
                if not go_to_next_page(page, page_num):
                    logger.info("Reached the last available LinkedIn results page at page %s", page_num)
                    break
                page.wait_for_timeout(2000)
                offset += 1

            logger.info(
                "Easy Apply total: %s submitted / %s score-qualified attempts (JD scored first; threshold >= %.1f)",
                page_easy_totals['submitted'], page_easy_totals['attempted'],
                float(config.get("matching", {}).get("easy_apply_threshold", 5.0)),
            )
            return

            # Legacy batch pipeline retained below for reference only.  The
            # page-by-page return above is the active default path.
            easy_totals = {'attempted': 0, 'submitted': 0}
            def apply_easy_now(job):
                easy_totals['attempted'] += 1
                try:
                    if _apply_single_job(page, job, None, profile, resume_path,
                                         get_cover_letter_dir(config), logger, args,
                                         already_open=True, config=config):
                        easy_totals['submitted'] += 1
                        update_card(job, applied=True, reason='Submitted through LinkedIn Easy Apply; JD scoring was intentionally skipped')
                except CodexServiceUnavailable as exc:
                    logger.error("Codex is unavailable: %s", exc)
                    raise SystemExit(75)
                except Exception:
                    logger.exception("Easy Apply failed for %s", job.get('title'))
                    context._easy_apply_human_intervention_required = True
                    _route_easy_apply_to_manual_queue(
                        page, job, 'easy_apply_failed', safe_page_url(page, job.get('link', '')),
                        score=0, logger=logger, config=config,
                    )
                    record_skipped(job, 'manual_pending: easy_apply_failed')
            jobs = collect_up_to_pages(page, keyword, location, args.page, args.max_pages, logger, config,
                                       easy_apply_callback=apply_easy_now)
            logger.info("Easy Apply: %s submitted / %s attempted (not scored)", easy_totals['submitted'], easy_totals['attempted'])
            if (
                getattr(context, "_cover_letter_required", False)
                or getattr(context, "_easy_apply_human_intervention_required", False)
            ):
                logger.warning("Run paused: Easy Apply human-intervention form is still visible")
                raise SystemExit(HUMAN_HANDOFF_EXIT)
            if not jobs:
                logger.info("No external jobs to score — done")
                return

            save_jds_for_review(jobs, "")
            logger.info(f"Saved {len(jobs)} JDs.")

            # Phase 2: 等待主 Agent 评分 (通过文件标志通信)
            threshold = config.get("matching", {}).get("threshold", 6.5)

            # 创建评分请求标志 (LinkedIn 专用目录)
            li_dir = PROJECT_ROOT / "data" / "linkedin"
            li_dir.mkdir(parents=True, exist_ok=True)
            scoring_flag = li_dir / "scoring_needed.flag"
            scoring_done = li_dir / "scoring_done.flag"
            # Clean stale flags from previous run to prevent cross-keyword contamination
            for f in [scoring_flag, scoring_done]:
                if f.exists():
                    f.unlink()
            scoring_flag.touch()
            logger.info(f"Phase 2: Waiting for Agent to score {len(jobs)} JDs...")

            # 等待主 Agent 评分 (最长 3 分钟), 超时后用代码 fallback
            start = time.monotonic()
            last_logged = 0
            scored_by = "agent"
            while not scoring_done.exists():
                try: time.sleep(1)
                except: pass
                elapsed = time.monotonic() - start
                if int(elapsed) >= last_logged + 30:
                    logger.info(f"   ...waiting for Agent scoring ({int(elapsed)}s)")
                    last_logged = int(elapsed)
                if elapsed > 900:
                    logger.error("AI scoring timed out; stopping without rule-based scoring")
                    if scoring_flag.exists(): scoring_flag.unlink()
                    raise SystemExit(1)

            # 读取 Agent 评分 (或 fallback)
            score_output = li_dir / 'agent_scores.json'
            if score_output.exists():
                with score_output.open(encoding='utf-8') as f:
                    score_metadata = json.load(f)
                if score_metadata.get('error'):
                    logger.error("AI scoring failed: %s", score_metadata['error'])
                    raise SystemExit(75 if score_metadata.get('codex_quota_exhausted') else 1)
            scores = load_agent_scores()
            if scoring_flag.exists(): scoring_flag.unlink()

            # ── 验证评分完整性 ──
            if not scores or set(scores) != {str(j.get('job_id', '')) for j in jobs}:
                logger.error("AI scores incomplete or invalid; stopping without rule-based scoring")
                raise SystemExit(1)

            if not scores:
                logger.warning("Agent scores empty — skipping this keyword")
                return

            detailed_scores = load_agent_scores_with_reasons() or {}
            for job in jobs:
                detail = detailed_scores.get(str(job.get('job_id', '')), {})
                score = detail.get('score', scores.get(job.get('job_id'), 0))
                decision = detail.get('decision', '')
                reason = detail.get('reason', '') or ('AI score is below the application threshold' if score < threshold else 'AI marked role eligible; awaiting application')
                update_card(job, score=score,
                            applied=False if score < threshold or decision in {'skip', 'manual_review'} else None,
                            reason=reason)
                if job.get("application_method") == "external":
                    record_external_decision(job, score, decision, reason, threshold)

            n = sum(1 for s in scores.values() if s >= threshold)
            logger.info(f"Agent scored: {n}/{len(scores)} jobs >= {threshold}")
            logger.info(f"Agent scores loaded: {len(scores)} jobs")
            try:
                keyword_state = record_keyword_result(
                    "linkedin", keyword, jobs, scores, threshold, config
                )
                logger.info(
                    "Keyword feedback recorded; active keywords=%s",
                    len(keyword_state.get("active_keywords", [])),
                )
            except Exception as exc:
                logger.warning(f"Keyword feedback failed: {exc}")

            # Phase 3: 在同一页面投递（不重新搜索！）
            scored_jobs = [(scores.get(j.get("job_id",""), 0), j) for j in jobs]
            scored_jobs.sort(key=lambda x: x[0], reverse=True)

            applied = 0
            codex_unavailable = False
            for score, job in scored_jobs:
                if score < threshold:
                    continue
                if applied >= 25:
                    break

                job_id = job.get("job_id", "")
                company = job.get("company", "?")
                title = job.get("title", "?")

                if is_already_applied(job_id, job):
                    logger.info(f"  ⏭ {title[:40]} — already applied")
                    continue
                if is_already_skipped(job_id):
                    logger.info(f"  ⏭ {title[:40]} — already skipped/manual pending")
                    continue
                try:
                    ok = _apply_single_job(page, job, score, profile, resume_path,
                                          get_cover_letter_dir(config), logger, args,
                                          config=config)
                except CodexServiceUnavailable as exc:
                    logger.error("Codex is unavailable; stopping the full run without marking jobs as manual: %s", exc)
                    codex_unavailable = True
                    break
                except Exception as e:
                    logger.error(f"  CRASH: {e}")
                    continue

                if ok:
                    applied += 1

                time.sleep(1)

            # 生成 JD review
            from src.agent_match import generate_jd_review_md
            generate_jd_review_md(jobs, scores)
            logger.info(f"Done! Applied: {applied}/{len([s for s,_ in scored_jobs if s>=threshold])} eligible")
            if codex_unavailable:
                raise SystemExit(75)

        elif args.collect:
            # ── Phase 1 only: Collect JDs for AI scoring ──
            logger.info(f"Collecting: '{keyword}' page {args.page}")
            # Keep the legacy collection command from sending Easy Apply
            # cards into the scoring batch.  It only records that the card was
            # discovered; a later --apply-page (or the preferred --run path)
            # will handle the actual Easy Apply submission.
            def note_easy_apply(card):
                logger.info(
                    "  Easy Apply discovered: %s @ %s — skipped JD scoring in --collect mode",
                    card.get("title", "?"), card.get("company", "?"),
                )
            jobs = collect_one_page(
                page, keyword, location, args.page, logger, config,
                easy_apply_callback=note_easy_apply,
            )
            if jobs:
                resume = load_resume(str(resume_path))
                save_jds_for_review(jobs, resume)
                logger.info(f"Saved {len(jobs)} JDs to data/jds_for_review.json")
                logger.info("Next: AI scores these JDs → python main.py --apply-page")
            else:
                logger.info("No new jobs on this page")

        elif args.apply_page:
            # ── Phase 2: Apply scored jobs ──
            logger.info(f"Applying on: '{keyword}' page {args.page}")
            applied = apply_scored_jobs(
                page, keyword, location, args.page,
                profile, resume_path, logger, args.dry_run, config=config
            )
            logger.info(f"Applied: {applied}")

        else:
            logger.info("Use --collect to gather JDs, then --apply-page after AI scoring")

    except KeyboardInterrupt:
        logger.info("\nInterrupted")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
    finally:
        save_cookies(context)
        try: pw.stop()
        except: pass


if __name__ == "__main__":
    raise SystemExit(main() or 0)
