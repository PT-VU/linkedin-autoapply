"""
外部网站投递模块 v3 — AI 全权驱动。
每步: 提取页面 → 等 AI 分析 → 执行指令 → 循环。
不使用任何规则引擎或 auto_fill_form。
"""

import json, logging, time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from .i18n import EXTERNAL_APPLY_TEXTS, EASY_APPLY_TEXTS
from .external_browser import (
    ExternalApplyHandoff, ExternalApplyUnconfirmed, ExternalSession,
    application_page, blocking_reason, execute_action, frame_state,
    record_outcome, set_file_input, visible_receipts, _install_external_diagnostics,
    focus_external_browser,
)
from .external_handoff_queue import is_human_handoff_reason, notify_handoff_required

logger = logging.getLogger("linkedin_autoapply.external_apply")

# P0修复: 全局已声明页面集合, 防止 Glassdoor 劫持 LinkedIn 打开的页面
_claimed_external_pages = set()  # 存储 URL 子串


# ── 检测外部申请按钮 ──

def is_external_apply(page: Page) -> bool:
    """检测当前岗位是否需要跳转到外部网站申请 (多语言)。"""
    elements = page.query_selector_all("button, a")
    for el in elements:
        try:
            if not el.is_visible(): continue
            txt = (el.inner_text() or "").strip().lower()
            aria = (el.get_attribute("aria-label") or "").lower()
            combined = f"{txt} {aria}"
            # LinkedIn aria-label 标注 "on company website" 即为 External Apply
            if 'on company website' in combined or 'on company site' in combined:
                return True
            if any(ea in combined for ea in EASY_APPLY_TEXTS): continue
            for kw in EXTERNAL_APPLY_TEXTS:
                if kw in combined: return True
            tag = el.evaluate("el => el.tagName")
            href = (el.get_attribute("href") or "").lower()
            if tag == "a" and "apply" in txt and "linkedin.com" not in href and href.startswith("http"):
                return True
        except Exception: continue
    return False


def extract_external_apply_url(page: Page) -> str:
    """Read (but never open) the external application URL on a job detail page.

    This is used while external automation is paused.  It deliberately accepts
    relative links and button data attributes because LinkedIn and job boards
    commonly wrap the real ATS URL in a button rather than a visible anchor.
    """
    candidates = []
    apply_words = (
        "apply", "apply now", "apply on company", "company website",
        "external", "sollic", "solic", "postuler", "応募",
    )
    blocked = tuple(str(item).lower() for item in globals().get("_BLOCKED_URL_PATTERNS", ()))
    try:
        elements = page.query_selector_all("a, button, [data-url], [data-href]")
    except Exception:
        return ""
    for element in elements:
        try:
            if not element.is_visible():
                continue
            text = " ".join([
                element.inner_text() or "",
                element.get_attribute("aria-label") or "",
                element.get_attribute("title") or "",
            ]).strip().lower()
            href = (
                element.get_attribute("href")
                or element.get_attribute("data-url")
                or element.get_attribute("data-href")
                or ""
            ).strip()
            if not href:
                try:
                    child = element.query_selector("a[href]")
                    href = (child.get_attribute("href") if child else "") or ""
                except Exception:
                    pass
            if not href or not href.startswith(("http://", "https://")):
                continue
            lowered = href.lower()
            if "linkedin.com" in lowered or "google.com" in lowered or any(item in lowered for item in blocked):
                continue
            # An application-labelled link is strongest.  Keep generic external
            # links as a fallback because some cards have icon-only buttons.
            rank = 0 if any(word in text for word in apply_words) else 1
            candidates.append((rank, href))
        except Exception:
            continue
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1][:1000]


def resolve_external_apply_url(page: Page, logger=None) -> str:
    """Resolve an external ATS URL without leaving the LinkedIn workflow page.

    LinkedIn frequently renders the external-apply control as a JavaScript
    button with no usable ``href``.  Try the cheap static read first, then use
    the existing click/popup/navigation fallback once.  The resolved page is
    closed (or the original LinkedIn page is restored) after capturing its URL
    so the caller can safely continue processing the remaining jobs.
    """
    url = extract_external_apply_url(page)
    if url:
        if logger:
            logger.info("External apply URL discovered from static DOM: %s", url[:200])
        return url

    source_url = str(getattr(page, "url", "") or "")
    if logger:
        logger.info("External apply URL absent from static DOM; resolving via controlled click")
    target = click_external_apply(page)
    if target is None:
        if logger:
            logger.warning("External apply click fallback produced no target page")
        return ""

    target_url = str(getattr(target, "url", "") or "").strip()
    valid = target_url.startswith(("http://", "https://"))
    lowered = target_url.lower()
    if not valid or "linkedin.com" in lowered or "google.com" in lowered:
        if logger:
            logger.warning("External click fallback returned unusable URL: %s", target_url[:200])
        return ""

    if logger:
        logger.info("External apply URL resolved via click/navigation: %s", target_url[:240])

    # Keep the original LinkedIn detail page available for the next job.  A
    # popup can simply be closed; if the click reused the original tab, restore
    # its URL after capturing the ATS target.
    try:
        if target is page:
            if source_url and source_url != target_url:
                page.goto(source_url, wait_until="domcontentloaded", timeout=15000)
        else:
            target.close()
    except Exception as exc:
        if logger:
            logger.debug("Could not restore LinkedIn page after URL resolution: %s", exc)
    return target_url


def click_external_apply(page: Page) -> Optional[Page]:
    """点击外部申请按钮 (多语言) 并返回新标签页。

    五层 fallback:
      0. 扫描 <a> 标签直接获取 href (Glassdoor 专用)
      1. 检测新标签页
      2. 检测当前页跳转
      3. 扫描所有页面
      4. JS 拦截 window.open 捕获 URL
    """
    original_pages = page.context.pages

    # ── Layer 0: 扫描页面所有 <a> 标签获取外部链接 (绕过 popup blocker) ──
    all_external_links = []
    for a_el in page.query_selector_all("a"):
        try:
            a_href = (a_el.get_attribute("href") or "").strip()
            if not a_href.startswith("http"): continue
            if "glassdoor" in a_href.lower(): continue
            if "linkedin.com" in a_href.lower(): continue
            if "google" in a_href.lower(): continue
            # P0修复: 过滤已知死胡同 URL
            if any(b in a_href.lower() for b in _BLOCKED_URL_PATTERNS): continue
            a_txt = (a_el.inner_text() or "").strip().lower()
            all_external_links.append((a_txt[:100], a_href[:300], a_el.is_visible()))
        except Exception: continue
    for a_el in page.query_selector_all("a"):
        try:
            a_href = (a_el.get_attribute("href") or "").strip()
            if not a_href.startswith("http"): continue
            if "glassdoor" in a_href.lower(): continue
            if "linkedin.com" in a_href.lower(): continue
            if "google" in a_href.lower(): continue
            # P0修复: 过滤已知死胡同 URL
            if any(b in a_href.lower() for b in _BLOCKED_URL_PATTERNS): continue
            a_txt = (a_el.inner_text() or "").strip().lower()
            # 收集所有外部链接
            all_external_links.append((a_txt[:100], a_href[:300], a_el.is_visible()))
        except Exception: continue

    if all_external_links:
        logger.info(f"[Layer 0] Found {len(all_external_links)} external <a> links on page")
        for txt, href, vis in all_external_links[:10]:
            logger.info(f"[Layer 0]   {'VIS' if vis else 'HID'}: {txt[:60]} → {href[:80]}")

    # 优先匹配已知 apply 文本
    EXTERNAL_LINK_TEXTS = [
        "solliciteren op site", "apply on company", "apply now",
        "apply externally", "solliciteer", "apply through",
        "company website", "external apply",
    ]
    for a_el in page.query_selector_all("a"):
        try:
            if not a_el.is_visible(): continue
            a_txt = (a_el.inner_text() or "").strip().lower()
            a_href = (a_el.get_attribute("href") or "").strip()
            if not a_href.startswith("http"): continue
            if "glassdoor" in a_href.lower(): continue
            if "linkedin.com" in a_href.lower(): continue
            if any(kw in a_txt for kw in EXTERNAL_LINK_TEXTS):
                logger.info(f"Layer 0: found external <a> link '{a_txt[:60]}' → navigating directly")
                try:
                    page.goto(a_href, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(3000)
                    _claimed_external_pages.add(a_href[:80])
                    return page
                except Exception as e:
                    logger.warning(f"Layer 0 navigation failed: {e}")
        except Exception: continue

    # ── 原有的 button/<a> 遍历 ──
    for el in page.query_selector_all("button, a"):
        try:
            if not el.is_visible(): continue
            txt = (el.inner_text() or "").strip().lower()
            aria = (el.get_attribute("aria-label") or "").lower()
            combined = f"{txt} {aria}"
            # LinkedIn aria 标注 on company website 即为 External
            if 'on company website' in combined or 'on company site' in combined:
                is_ext = True
            else:
                if any(ea in combined for ea in EASY_APPLY_TEXTS): continue
                is_ext = any(kw in combined for kw in EXTERNAL_APPLY_TEXTS)
            tag = el.evaluate("el => el.tagName")
            href_lower = (el.get_attribute("href") or "").lower()
            if tag == "a" and "apply" in txt and "linkedin.com" not in href_lower and href_lower.startswith("http"):
                is_ext = True
            if is_ext:
                # ── 点击前提取目标 URL (Layer 4 fallback) ──
                target_href = None
                if tag == "A":
                    target_href = el.get_attribute("href") or ""
                else:
                    try:
                        inner_a = el.query_selector("a")
                        if inner_a:
                            target_href = inner_a.get_attribute("href") or ""
                    except Exception: pass
                    if not target_href:
                        target_href = el.get_attribute("data-url") or el.get_attribute("data-href") or ""

                # ── expect_popup: Playwright 内置 popup 捕获 ──
                new_page = None
                try:
                    with page.expect_popup(timeout=8000) as popup_info:
                        el.click()
                        logger.info(f"Clicked external: '{txt[:60]}'")
                    new_page = popup_info.value
                    logger.info(f"[POPUP] Captured popup page: {new_page.url[:120]}")
                    if not new_page.url.startswith(('http://', 'https://')):
                        new_page.wait_for_url(lambda url: str(url).startswith(('http://', 'https://')), timeout=10000)
                    new_page.wait_for_load_state("domcontentloaded", timeout=10000)
                    new_page.wait_for_timeout(2000)
                    # Popup capture does not guarantee that Chromium selected
                    # the new tab. Select it explicitly so a later human
                    # handoff cannot foreground the still-active LinkedIn JD.
                    new_page.bring_to_front()
                    logger.info("External popup tab selected: %s", new_page.url[:240])
                    _claimed_external_pages.add(new_page.url[:80])
                    return new_page
                except Exception as e:
                    logger.info(f"[POPUP] No popup captured: {e}")

                # LinkedIn may show a safety reminder after the first click:
                # "Review job post" / "Continue applying".  The actual ATS
                # navigation happens only after the user-facing continuation
                # link is clicked, so capture that second popup/navigation too.
                try:
                    continue_el = None
                    for candidate in page.query_selector_all("a, button"):
                        if not candidate.is_visible():
                            continue
                        candidate_text = " ".join((candidate.inner_text() or "").split()).lower()
                        if "continue applying" in candidate_text:
                            continue_el = candidate
                            break
                    if continue_el is not None:
                        logger.info("LinkedIn safety reminder detected; clicking 'Continue applying'")
                        before_continue = list(page.context.pages)
                        try:
                            with page.expect_popup(timeout=10000) as popup_info:
                                continue_el.click()
                            continued_page = popup_info.value
                            continued_page.wait_for_load_state("domcontentloaded", timeout=10000)
                            continued_page.wait_for_timeout(1500)
                            continued_page.bring_to_front()
                            logger.info("[SAFETY] Captured external page: %s", continued_page.url[:240])
                            _claimed_external_pages.add(continued_page.url[:80])
                            return continued_page
                        except Exception as continue_error:
                            logger.info("[SAFETY] No popup captured after continuation: %s", continue_error)
                            new_pages = [p for p in page.context.pages if p not in before_continue]
                            if new_pages:
                                continued_page = new_pages[0]
                                continued_page.bring_to_front()
                                continued_page.wait_for_load_state("domcontentloaded", timeout=10000)
                                continued_page.wait_for_timeout(1500)
                                _claimed_external_pages.add(continued_page.url[:80])
                                return continued_page
                            current_url = (page.url or "").lower()
                            if current_url.startswith(("http://", "https://")) and "linkedin.com" not in current_url:
                                return page
                except Exception as safety_error:
                    logger.debug("LinkedIn safety reminder handling failed: %s", safety_error)

                # Layer 1: 新标签页 (fallback)
                new_pages = [p for p in page.context.pages if p not in original_pages]
                if new_pages:
                    logger.info(f"[DEBUG] Layer 1 SUCCESS: {len(new_pages)} new page(s)")
                    new_page = new_pages[0]
                    new_page.bring_to_front()
                    if not new_page.url.startswith(('http://', 'https://')):
                        try:
                            new_page.wait_for_url(lambda url: str(url).startswith(('http://', 'https://')), timeout=10000)
                        except PlaywrightTimeoutError:
                            logger.warning("External popup did not reach a usable URL")
                            return None
                    new_page.wait_for_load_state("domcontentloaded")
                    new_page.wait_for_timeout(2000)
                    _claimed_external_pages.add(new_page.url[:80])
                    return new_page
                logger.info(f"[DEBUG] Layer 1 FAIL: no new pages ({len(original_pages)}→{len(page.context.pages)})")

                # Layer 2: 当前页跳转 (排除 LinkedIn 和 Glassdoor)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=3000)
                except Exception: pass
                current_url = page.url.lower()
                logger.info(f"[DEBUG] Layer 2: current URL = {current_url[:100]}")
                if ("linkedin.com/jobs" not in current_url and "linkedin.com" not in current_url
                        and "glassdoor" not in current_url):
                    _claimed_external_pages.add(page.url[:80])
                    return page
                logger.info(f"[DEBUG] Layer 2 FAIL: URL still glassdoor/linkedin")

                # Layer 3: 扫描所有页面 (排除 LinkedIn, Glassdoor, Indeed, 已声明的页面)
                # Indeed 页面由 handle_quick_apply_flow() 正常处理, 不应被 External Apply 劫持
                for p in page.context.pages:
                    try:
                        url = p.url.lower()
                        if ("linkedin.com" not in url and "glassdoor" not in url
                                and "indeed" not in url and "smartapply" not in url
                                and url.startswith("http") and "google" not in url):
                            if any(claimed in url for claimed in _claimed_external_pages):
                                continue
                            p.bring_to_front()
                            p.wait_for_load_state("domcontentloaded")
                            p.wait_for_timeout(2000)
                            _claimed_external_pages.add(p.url[:80])
                            return p
                    except Exception:
                        continue

                # Layer 4: JS 拦截 window.open 获取 URL
                if not target_href or not target_href.startswith("http"):
                    try:
                        page.evaluate("() => { window.__captured_popup_url = null; var o=window.open; window.open=function(u){window.__captured_popup_url=u;return o.apply(this,arguments);}; }")
                        el.click()
                        page.wait_for_timeout(2000)
                        captured = page.evaluate("() => window.__captured_popup_url")
                        if captured and str(captured).startswith("http"):
                            logger.info(f"Layer 4: captured popup URL → navigating: {str(captured)[:100]}")
                            page.goto(str(captured), wait_until="domcontentloaded", timeout=15000)
                            page.wait_for_timeout(3000)
                            return page
                    except Exception as e:
                        logger.debug(f"Layer 4 failed: {e}")
        except Exception: continue
    return None


def detect_ats_type(page: Page) -> str:
    """检测外部招聘系统的类型。"""
    url = page.url.lower()
    if "workday" in url or "myworkdayjobs" in url: return "workday"
    elif "greenhouse.io" in url: return "greenhouse"
    elif "lever.co" in url: return "lever"
    elif "icims" in url: return "icims"
    elif "smartrecruiters" in url: return "smartrecruiters"
    elif "personio" in url: return "personio"
    return "generic"


def _lightpanda_submit_runtime_block(session: ExternalSession) -> bool:
    """Detect a Lightpanda-only hCaptcha stall before spending more AI rounds."""
    if session.backend != "lightpanda":
        return False
    context = session.page.context
    errors = [str(item).lower() for item in getattr(context, "_external_page_errors", [])]
    if not getattr(context, "_external_submit_attempted", False):
        return False
    host = (urlsplit(session.page.url).hostname or "").lower()
    ignored = ("sentry", "analytics", "captcha", "hcaptcha", "recaptcha",
               "challenge", "indeed", "widget_params")
    for item in getattr(context, "_external_network_events", []):
        if item.get("kind") != "request" or item.get("method") not in {"POST", "PUT", "PATCH", "DELETE"}:
            continue
        url = str(item.get("url", "")).lower()
        if (urlsplit(url).hostname or "").lower() == host and not any(token in url for token in ignored):
            return False
    if any("no access" in item and "captcha" in item for item in errors):
        return True
    # Lightpanda may fail silently: the hCaptcha objects are initialized and
    # the React Send control stays busy, but no page error or employer request
    # is emitted. Require both DOM signals before replaying in Edge.
    try:
        state = session.page.evaluate("""() => ({
            busy: [...document.querySelectorAll('button[type=submit],input[type=submit],form button')]
                .some(e => e.getAttribute('aria-busy') === 'true'),
            hcaptcha: typeof window.hcaptcha === 'object' || typeof window.hcaptcha === 'function',
            grecaptcha: typeof window.grecaptcha === 'object' || typeof window.grecaptcha === 'function',
            invalid: [...document.querySelectorAll('input,select,textarea')]
                .some(e => e.willValidate && !e.validity.valid)
        })""")
        if state.get("busy") and (state.get("hcaptcha") or state.get("grecaptcha")) and not state.get("invalid"):
            return True
    except Exception:
        pass
    return False


def _prepare_edge_handoff(
    source_page: Page,
    lightpanda_page: Page,
    profile: Dict[str, Any],
    resume_path: str,
    cover_letter_path: str,
    max_steps: int,
    platform: str,
) -> tuple[ExternalSession, bool, str]:
    """Rebuild a Lightpanda application state in the visible Edge tab.

    Lightpanda and Edge use different CDP contexts.  Calling
    ``bring_to_front`` on a Lightpanda page cannot select an Edge tab, so a
    human handoff from Lightpanda previously foregrounded Edge while showing a
    blank/unrelated page.  Reopen the same external URL in the original Edge
    page and let the same AI form loop refill it before handing control to the
    person.  The helper returns the Edge session even when the refill itself
    reaches another handoff or an unconfirmed Send state.
    """
    edge_page = source_page
    target_url = str(getattr(lightpanda_page, "url", "") or edge_page.url or "")
    try:
        source_host = (urlsplit(edge_page.url).hostname or "").casefold()
        target_host = (urlsplit(target_url).hostname or "").casefold()
        if target_host and source_host and target_host != source_host:
            logger.warning(
                "Refusing Lightpanda→Edge handoff navigation across hosts: %s -> %s",
                source_host, target_host,
            )
            target_url = edge_page.url
        if target_url and edge_page.url != target_url:
            logger.info("Reopening external application in visible Edge for human handoff: %s", target_url[:240])
            edge_page.goto(target_url, wait_until="domcontentloaded")
        edge_page.wait_for_timeout(1500)
        _install_external_diagnostics(edge_page)
        edge_page.context._external_backend = "edge"
        edge_page.context._external_submit_attempted = False
        edge_page.context._external_receipts_before = set()
        edge_page.context._external_recent_actions = []
        edge_page.bring_to_front()
        logger.info("Edge handoff page ready; refilling form before human intervention")
        try:
            success = _ai_driven_apply(
                edge_page, profile, resume_path, cover_letter_path, max_steps, platform
            )
            if success:
                return ExternalSession(edge_page, "edge", fallback_reason="lightpanda_human_handoff"), True, ""
            return ExternalSession(edge_page, "edge", fallback_reason="lightpanda_human_handoff"), False, "edge_handoff_refill_failed"
        except ExternalApplyHandoff as error:
            return ExternalSession(edge_page, "edge", fallback_reason="lightpanda_human_handoff"), False, str(error)
        except ExternalApplyUnconfirmed as error:
            return ExternalSession(edge_page, "edge", fallback_reason="lightpanda_human_handoff"), False, str(error)
    except Exception as error:
        logger.exception("Could not prepare visible Edge handoff: %s", error)
        return ExternalSession(edge_page, "edge", fallback_reason="lightpanda_human_handoff_failed"), False, type(error).__name__


# ── 外部投递主入口 ──

def handle_external_apply_flow(
    page: Page, profile: Dict[str, Any],
    resume_path: str, cover_letter_path: str = "", max_steps: int = 15,
    platform: str = "linkedin",
    application_metadata: Optional[Dict[str, str]] = None,
) -> bool:
    """外部投递入口: 接收已打开的页面 → AI 驱动填表。"""
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(3000)
    ats_type = detect_ats_type(page)
    logger.info(f"External ATS: {ats_type} | {page.url[:100]}")

    source_page = page
    session = ExternalSession(page, "edge")
    try:
        logger.info("External preflight: checking authentication and CAPTCHA")
        reason = blocking_reason(page)
        logger.info("External preflight: blocking check complete")
        if reason:
            raise ExternalApplyHandoff(reason)
        with application_page(page) as session:
            session.page.context._external_submit_attempted = False
            session.page.context._external_receipts_before = set()
            session.page.context._external_recent_actions = []
            reason = blocking_reason(session.page)
            if reason:
                raise ExternalApplyHandoff(reason)
            try:
                success = _ai_driven_apply(session.page, profile, resume_path, cover_letter_path, max_steps, platform)
            except ExternalApplyUnconfirmed as error:
                if not _lightpanda_submit_runtime_block(session):
                    raise
                logger.warning(
                    "Lightpanda submit blocked by hCaptcha runtime (%s); retrying once in Edge",
                    str(error),
                )
                if blocking_reason(source_page):
                    raise ExternalApplyHandoff("captcha_required")
                source_page.context._external_submit_attempted = False
                source_page.context._external_recent_actions = []
                source_page.context._external_receipts_before = set()
                _install_external_diagnostics(source_page)
                session = ExternalSession(
                    source_page, "edge", fallback_reason="lightpanda_hcaptcha_runtime_error"
                )
                success = _ai_driven_apply(
                    source_page, profile, resume_path, cover_letter_path, max_steps, platform
                )
            if application_metadata is not None:
                application_metadata["cover_letter_status"] = _cover_letter_status(session.page)
            status = "submitted" if success else "failed_or_unconfirmed"
            record_outcome(session, status, platform)
            return success
    except ExternalApplyHandoff as error:
        handoff_reason = str(error)
        needs_handoff = is_human_handoff_reason(handoff_reason)
        if needs_handoff and not _external_form_ready_for_handoff(session.page):
            logger.warning(
                "External human handoff suppressed: visible challenge reported before the form was ready"
            )
            handoff_reason = f"{handoff_reason}; form_incomplete_before_handoff"
            needs_handoff = False
        # The normal external backend may be Lightpanda.  Its page is not a
        # visible Edge tab, so handing it directly to a person leaves Edge on
        # the LinkedIn detail page or on an empty application form.  Rebuild
        # the live form in the original Edge tab before sounding the alert.
        if needs_handoff and session.backend == "lightpanda":
            session, edge_success, edge_reason = _prepare_edge_handoff(
                source_page, session.page, profile, resume_path, cover_letter_path,
                max_steps, platform,
            )
            if edge_success:
                if application_metadata is not None:
                    application_metadata["handoff_required"] = False
                    application_metadata["handoff_reason"] = "edge_refill_submitted"
                    application_metadata["cover_letter_status"] = _cover_letter_status(session.page)
                record_outcome(session, "submitted", platform, "edge_refill_submitted")
                return True
            if edge_reason:
                handoff_reason = f"{handoff_reason}; edge_handoff={edge_reason}"
            # Keep this as a human handoff even if the Edge refill stopped on
            # an unconfirmed Send; the visible Edge form is now the actionable
            # page and the person may complete it.
            needs_handoff = True
        if application_metadata is not None:
            application_metadata["handoff_required"] = needs_handoff
            application_metadata["handoff_reason"] = handoff_reason
        # Only a visible interactive challenge is ready for human action.
        # Missing answers, incomplete forms, and an unconfirmed Send without a
        # visible challenge go to the regular manual queue without bringing
        # an unfinished page to the foreground or sounding an alert.
        if needs_handoff:
            if _wait_for_manual_captcha(session, timeout_seconds=120):
                if application_metadata is not None:
                    application_metadata["handoff_reason"] = "captcha_completed_by_user"
                    application_metadata["handoff_required"] = False
                record_outcome(session, "submitted", platform, "captcha_completed_by_user")
                return True
        if application_metadata is not None:
            application_metadata["handoff_reason"] = handoff_reason
        # Keep the existing outcome status for compatibility; the separate
        # metadata flag decides whether the job enters the intervention queue
        # or the regular manual queue.
        record_outcome(session, "needs_user", platform, handoff_reason)
        return False
    except ExternalApplyUnconfirmed as error:
        if application_metadata is not None:
            application_metadata["handoff_required"] = False
            application_metadata["handoff_reason"] = str(error)
        visible_reason = ""
        try:
            visible_reason = blocking_reason(session.page)
        except Exception:
            visible_reason = ""
        if (
            is_human_handoff_reason(visible_reason)
            and _external_form_ready_for_handoff(session.page)
            and _wait_for_manual_captcha(session, timeout_seconds=120)
        ):
            if application_metadata is not None:
                application_metadata["handoff_reason"] = "human_completed_submission"
                application_metadata["handoff_required"] = False
            record_outcome(session, "submitted", platform, "human_completed_submission")
            return True
        record_outcome(session, "submission_unconfirmed", platform, str(error))
        return False
    except Exception as error:
        if application_metadata is not None:
            application_metadata["handoff_required"] = False
            application_metadata["handoff_reason"] = type(error).__name__
        record_outcome(session, "failed_or_unconfirmed", platform, type(error).__name__)
        logger.exception("External application stopped; actions will not be replayed in another browser")
        return False


# ── AI 驱动填表核心 ──

def _detect_and_fill_cover_letter(page: Page, profile: Dict[str, Any], platform: str = "linkedin", cover_letter_path: str = "") -> bool:
    """检测外部表单中的 Cover Letter 字段 — 创建专用 flag, Agent 异步生成。

    不再使用代码模板生成。所有 cover letter 由 Agent 基于 CV 真实信息 +
    user_qa_preferences.json 生成, 确保真实性。

    Returns:
        True 如果检测到 cover letter 字段并创建了 flag
    """
    from src.cover_letter_agent import detect_cover_letter_field

    cl_field = detect_cover_letter_field(page, profile)
    if not cl_field:
        return False

    page.context._cover_letter_field_seen = True

    logger.info(f"📝 Cover Letter field detected: {cl_field['type']} — {cl_field['label'][:60]}")
    docx_path = cover_letter_path
    if not docx_path or not Path(docx_path).is_file():
        logger.info("Cover letter upload skipped: no existing tailored file was supplied")
        return False
    logger.info("Cover letter upload: existing file supplied; locating the field")
    text = ""
    if cl_field.get("type") == "textarea":
        try:
            from docx import Document
            text = "\n\n".join(p.text for p in Document(docx_path).paragraphs if p.text.strip())
        except Exception as error:
            logger.warning("Unable to read cover letter: %s", error)

    if cl_field.get("type") == "textarea" and text:
        for el in page.query_selector_all("textarea"):
            try:
                if not el.is_visible():
                    continue
                label = _get_label(el).lower()
                attrs = " ".join([
                    el.get_attribute("id") or "",
                    el.get_attribute("name") or "",
                    el.get_attribute("placeholder") or "",
                    label,
                ]).lower()
                if any(kw in attrs for kw in ("cover", "motivation letter", "motivatiebrief", "sollicitatiebrief")):
                    el.fill(text)
                    page.context._cover_letter_uploaded = True
                    logger.info("Cover letter text filled")
                    return True
            except Exception:
                continue

    if cl_field.get("type") == "file" and docx_path:
        for el in page.query_selector_all("input[type='file']"):
            try:
                name = (el.get_attribute("name") or "").lower()
                label = _get_label(el).lower()
                attrs = f"{name} {label}"
                if any(kw in attrs for kw in ("cover", "motivation", "motivatie", "sollicitatiebrief")):
                    logger.info("Cover letter upload: setting file input")
                    set_file_input(el, docx_path)
                    logger.info("Cover letter upload: file input accepted; waiting for page")
                    page.wait_for_timeout(1500)
                    page.context._cover_letter_uploaded = True
                    logger.info(f"Cover letter DOCX uploaded: {Path(docx_path).name}")
                    return True
            except Exception as error:
                logger.warning("Cover letter upload failed: %s", error)
                continue

    logger.warning("Cover letter upload: matching usable field not found")
    return False


def _cover_letter_status(page: Page) -> str:
    if getattr(page.context, "_cover_letter_uploaded", False):
        return "yes"
    if getattr(page.context, "_cover_letter_field_seen", False):
        return "no"
    return "not applicable"


def _recover_external_page(page: Page) -> Optional[Page]:
    """Return a live external page if the current page was closed."""
    try:
        page.evaluate("() => true")
        return page
    except Exception:
        pass

    try:
        pages = list(page.context.pages)
    except Exception:
        return None

    for candidate in pages:
        try:
            url = (candidate.url or "").lower()
            if not url.startswith("http"):
                continue
            if any(host in url for host in ("linkedin.com", "glassdoor", "google", "bing", "msn")):
                continue
            candidate.evaluate("() => true")
            candidate.bring_to_front()
            logger.info(f"Recovered external page: {candidate.url[:100]}")
            return candidate
        except Exception:
            continue
    return None


def _ai_driven_apply(
    page: Page, profile: Dict[str, Any],
    resume_path: str, cover_letter_path: str = "", max_rounds: int = 10,
    platform: str = "linkedin",
) -> bool:
    """AI 全权驱动: 每轮提取页面 → 等 AI → 执行指令 → 检查。"""
    logger.info("AI-driven external apply started")

    # P0修复: URL 黑名单入口检查 — 死胡同页面直接返回, 不启动 AI 循环
    current_url = (page.url or "").lower()
    for blocked in _BLOCKED_URL_PATTERNS:
        if blocked in current_url:
            logger.info(f"⏭ Auto-skipping blocked URL in _ai_driven_apply: {current_url[:120]}")
            return False

    # Auto-fill cover letter on first round
    cover_letter_filled = False

    previous_fingerprint = None
    last_progress = time.monotonic()
    rnd = 0

    while True:
        rnd += 1
        recovered_page = _recover_external_page(page)
        if recovered_page is None:
            logger.warning("External page was closed and could not be recovered")
            return False
        page = recovered_page
        try:
            page.wait_for_timeout(2000)
        except Exception as e:
            logger.warning(f"External page wait failed: {e}")
            return False

        # Auto-detect and fill cover letter on first round
        if rnd == 1 and not cover_letter_filled:
            try:
                cover_letter_filled = _detect_and_fill_cover_letter(page, profile, platform, cover_letter_path)
            except Exception as e:
                logger.debug(f"Cover letter auto-fill error: {e}")

        logger.info("External preflight: checking submission receipt")
        if _check_success(page):
            logger.info("External application submitted!")
            return True

        reason = blocking_reason(page)
        if reason:
            raise ExternalApplyHandoff(reason)
        if getattr(page.context, "_external_submit_attempted", False):
            errors = _check_validation_errors(page)
            if errors:
                page.context._external_submit_attempted = False
            else:
                raise ExternalApplyUnconfirmed("no_application_receipt_after_submit")

        logger.info("External preflight: capturing page progress")
        fingerprint = _progress_fingerprint(page)
        logger.info("External preflight: page progress captured")
        now = time.monotonic()
        if fingerprint is not None and fingerprint != previous_fingerprint:
            last_progress = now
        elif now - last_progress >= 300:
            logger.warning("External form has made no observable progress for five minutes; requesting review")
            raise ExternalApplyHandoff("form_no_progress")
        elif rnd > 1:
            # Pace retries on unchanged pages to limit repeated AI calls.
            history = getattr(page.context, "_external_recent_actions", [])
            failures = _failure_summary(history) if isinstance(history, list) else []
            repeated = bool(failures and failures[0]["occurrences"] >= 3)
            delay_ms = 45000 if repeated else 15000
            if repeated:
                logger.info("Repeated external action failures detected; reducing retry frequency")
            page.wait_for_timeout(min(delay_ms, max(0, int((300 - (now - last_progress)) * 1000))))
            now = time.monotonic()
            if now - last_progress >= 300:
                raise ExternalApplyHandoff("form_no_progress")

        logger.info(f"External AI round {rnd} (five-minute no-progress limit)")
        page.context._external_no_progress_deadline = last_progress + 300
        page.context._external_no_progress_seconds = max(0, int(now - last_progress))

        # Check for validation errors from previous round
        current_errors = _check_validation_errors(page)
        if current_errors:
            logger.warning(f"Round {rnd}: {len(current_errors)} validation error(s) persist")

        previous_fingerprint = fingerprint
        if not _request_ai_help(page, profile, resume_path, cover_letter_path, platform):
            logger.warning(f"AI did not respond in round {rnd}")
            continue

        page.wait_for_timeout(1000)
        if _check_success(page):
            logger.info("Submitted after AI round!")
            return True

def _progress_fingerprint(page):
    """Capture visible content and live form values; return None if incomplete."""
    try:
        frames = []
        for index, frame in enumerate(page.frames):
            if frame != page.main_frame and frame.url in ("", "about:blank"):
                logger.info("External progress: skipping blank frame %d", index)
                continue
            logger.info("External progress: reading frame %d", index)
            frames.append(frame.evaluate("""() => ({
                url: location.href,
                text: document.body ? document.body.innerText : '',
                fields: Array.from(document.querySelectorAll('input, select, textarea, button')).map(e => ({
                    name: e.name, type: e.type, value: e.value, checked: e.checked,
                    disabled: e.disabled, hidden: !e.getClientRects().length,
                    options: e.tagName === 'SELECT' ? Array.from(e.options).map(o => [o.value, o.selected]) : null,
                    files: e.type === 'file' ? Array.from(e.files).map(f => [f.name, f.size]) : null
                }))
            })"""))
        return json.dumps(frames, sort_keys=True, ensure_ascii=False)
    except Exception as error:
        logger.debug("Cannot compare external form progress: %s", error)
        return None


def _check_success(page: Page) -> bool:
    """Require application-specific receipt text, never a generic thank-you."""
    if getattr(page.context, "_external_submit_attempted", False) is not True:
        return False
    before = getattr(page.context, "_external_receipts_before", set())
    if visible_receipts(page) - before:
        logger.info("External application receipt confirmed after submission")
        return True
    return False


def _select_handoff_page(session: ExternalSession) -> Page:
    """Choose the best same-host page for a visible human handoff."""
    current = session.page
    try:
        host = (urlsplit(current.url).hostname or "").casefold()
        candidates = [
            candidate for candidate in current.context.pages
            if not candidate.is_closed()
            and (urlsplit(candidate.url).hostname or "").casefold() == host
        ]
        if not candidates:
            return current

        def score(candidate: Page) -> tuple[int, int, int]:
            try:
                forms = candidate.locator("form").count()
                controls = candidate.locator(
                    "input:not([type=hidden]), textarea, select, button"
                ).count()
                body_chars = len(candidate.locator("body").inner_text(timeout=1500))
                return (forms, controls, body_chars)
            except Exception:
                return (0, 0, 0)

        selected = max(candidates, key=score)
        if selected != current:
            logger.warning(
                "External handoff switched to the same-host page with the most form content: %s",
                selected.url[:240],
            )
            session.page = selected
        return selected
    except Exception as error:
        logger.info("External handoff page selection failed: %s", type(error).__name__)
        return current


def _external_form_ready_for_handoff(page: Page) -> bool:
    """Return true only when the visible application form is action-ready.

    A CAPTCHA iframe can exist before the applicant fields are filled.  The
    iframe text is therefore not enough evidence for a human handoff: the
    user must receive a page where the final challenge/Send action is the only
    remaining work.  This check is intentionally conservative and treats
    visible required contact fields, resume uploads, and location/country
    selectors as required even when an ATS omitted the HTML ``required``
    attribute.
    """
    try:
        state = page.evaluate(r"""() => {
          const visible = e => {
            const r = e.getBoundingClientRect(), s = getComputedStyle(e);
            return !!(r.width && r.height && s.display !== 'none' && s.visibility !== 'hidden');
          };
          const label = e => {
            const labels = [...(e.labels || [])].map(x => x.innerText || '').join(' ');
            const parent = e.closest('fieldset,li,[data-qa],.application-question,section,div');
            return `${labels} ${e.getAttribute('aria-label') || ''} ${e.name || ''} ${e.id || ''} ${parent?.innerText || ''}`
              .replace(/\s+/g, ' ').trim().toLowerCase();
          };
          const present = e => {
            if (e.type === 'file') {
              if (e.files && e.files.length) return true;
              const text = (e.parentElement?.innerText || '').toLowerCase();
              return !!text && !/(attach|upload|choose|select).*(resume|cv|curriculum)|resume\s*\/\s*cv\s*$/i.test(text);
            }
            if (e.type === 'radio') {
              return !!e.name && [...document.querySelectorAll(`input[type="radio"][name="${CSS.escape(e.name)}"]`)].some(x => x.checked);
            }
            if (e.type === 'checkbox') return !!e.checked;
            if (e.tagName === 'SELECT') return !!String(e.value || '').trim();
            if (e.getAttribute('role') === 'combobox' || e.getAttribute('role') === 'listbox') {
              const root = e.closest('[class*="control" i],fieldset,section,div') || e.parentElement;
              return !!String(e.value || e.innerText || root?.innerText || '').trim();
            }
            return !!String(e.value || '').trim();
          };
          const controls = [...document.querySelectorAll('input,select,textarea,[role="combobox"],[role="listbox"]')]
            .filter(e => visible(e) && !e.disabled && !['hidden','submit','button','reset'].includes((e.type || '').toLowerCase()));
          const required = controls.filter(e => {
            const text = label(e);
            return !!e.required || e.getAttribute('aria-required') === 'true' ||
              e.dataset.required === 'true' || e.getAttribute('data-required') === 'true' ||
              (e.type === 'file' && /(resume|cv|curriculum)/i.test(text)) ||
              (e.tagName === 'SELECT' && /(location|country|region|office)/i.test(text));
          });
          const missing = required.filter(e => !present(e) || (e.willValidate && !e.validity.valid));
          return {controls: controls.length, required: required.length, missing: missing.map(e => ({name:e.name || '', id:e.id || '', type:e.type || e.tagName.toLowerCase()}))};
        }""")
        ready = bool(state and state.get("controls") and not state.get("missing"))
        if not ready:
            logger.info(
                "External handoff readiness: controls=%s required=%s missing=%s",
                (state or {}).get("controls", 0), (state or {}).get("required", 0),
                len((state or {}).get("missing", [])),
            )
        return ready
    except Exception as error:
        logger.info("External handoff readiness check failed: %s", type(error).__name__)
        return False


def _wait_for_manual_captcha(session: ExternalSession, timeout_seconds: int = 120) -> bool:
    """Keep the Edge application page alive for any human handoff.

    The function focuses the actual application tab and observes it. It never
    clicks the challenge or Send, and it accepts success only after a new
    employer receipt is visible. The historical function name is retained for
    compatibility with existing callers and tests.
    """
    page = _select_handoff_page(session)
    # Recheck immediately before focusing Edge or sounding the alert.  A
    # challenge can disappear while the AI response is being written, and a
    # stale frame must never expose an empty initial form to the user.
    try:
        current_reason = blocking_reason(page)
    except Exception:
        current_reason = ""
    form_ready = _external_form_ready_for_handoff(page)
    if not is_human_handoff_reason(current_reason) or not form_ready:
        logger.warning(
            "External human handoff suppressed at final check: challenge=%s form_ready=%s",
            current_reason or "none", form_ready,
        )
        return False
    session_host = (urlsplit(page.url).hostname or '').casefold()
    try:
        # Window focus alone can leave the correct tab hidden behind another
        # Edge tab. Select the application tab first, then foreground Edge.
        page.bring_to_front()
    except Exception as error:
        logger.warning("External handoff tab activation failed: %s", type(error).__name__)
    try:
        focused = focus_external_browser(page=page)
    except Exception as error:
        focused = False
        logger.warning("External CAPTCHA handoff focus failed: %s", type(error).__name__)
    try:
        page.bring_to_front()
    except Exception:
        pass
    try:
        form_count = page.locator("form").count()
        visible_control_count = page.locator(
            "input:not([type=hidden]), textarea, select, button"
        ).count()
        logger.info(
            "External CAPTCHA handoff ready: url=%s title=%s forms=%s controls=%s",
            page.url[:240], page.title()[:160], form_count, visible_control_count,
        )
    except Exception as error:
        logger.info("External CAPTCHA handoff ready-state inspection failed: %s", type(error).__name__)
    notify_handoff_required(source="external_apply_ready")
    logger.info("External CAPTCHA handoff: Edge focused=%s; waiting up to %ss", focused, timeout_seconds)
    deadline = time.monotonic() + max(1, int(timeout_seconds))
    last_log = -1
    while time.monotonic() < deadline:
        try:
            # A challenge may recreate the application tab. Rebind to a
            # same-host page before deciding that the old page disappeared.
            if page.is_closed():
                candidates = [candidate for candidate in page.context.pages
                              if (urlsplit(candidate.url).hostname or '').casefold() ==
                              session_host]
                if candidates:
                    page = candidates[-1]
                    session.page = page
                    logger.info("External CAPTCHA handoff: rebound to recreated application page")
            if not page.is_closed():
                before = getattr(page.context, "_external_receipts_before", set())
                if visible_receipts(page) - before or _check_success(page):
                    logger.info("External CAPTCHA handoff: employer receipt confirmed")
                    return True
        except Exception as error:
            logger.info("External CAPTCHA handoff observation error: %s", type(error).__name__)
        elapsed = int(time.monotonic() - (deadline - max(1, int(timeout_seconds))))
        if elapsed // 10 != last_log:
            logger.info("External CAPTCHA handoff waiting (%ss/%ss)", min(elapsed, int(timeout_seconds)), int(timeout_seconds))
            last_log = elapsed // 10
        try:
            page.wait_for_timeout(2000)
        except Exception:
            time.sleep(2)
    logger.warning("External CAPTCHA handoff timed out after %ss; skipping this job", timeout_seconds)
    return False


# ── AI 帮助接口 ──

# P0修复: URL 黑名单 — 已知死胡同, 自动跳过避免死循环
_BLOCKED_URL_PATTERNS = [
    "requests.hrtechprivacy.com",   # Indeed 隐私同意页 (Glassdoor ext apply 死胡同)
    "linkedin.com/jobs/search/",    # LinkedIn 搜索页 (非外部 ATS, 代码迷路)
]


def _external_data_dir(platform):
    return Path(__file__).resolve().parent.parent / "data" / platform


def _matched_known_answers(form_state: str):
    """Select only fixed answers whose saved patterns occur on this form."""
    prefs_path = Path(__file__).resolve().parent.parent / 'data' / 'user_qa_preferences.json'
    try:
        preferences = json.loads(prefs_path.read_text(encoding='utf-8')).get('preferences', {})
    except Exception:
        return []
    state = form_state.lower()
    answers = []
    for key, item in preferences.items():
        if not isinstance(item, dict):
            continue
        patterns = [str(pattern).lower() for pattern in item.get('question_patterns', [])]
        answer = str(item.get('answer', '')).strip()
        if answer and answer not in {'待用户确认', '待确认', 'unknown', 'Unknown', 'N/A'} and any(pattern in state for pattern in patterns):
            answers.append({'id': key, 'answer': answer})
    return answers


def _matched_profile_fields(form_state: str, profile: Dict[str, Any], resume_path: str):
    """Expose only configured basic facts relevant to the visible form."""
    state = form_state.lower()
    result = {}
    for key, patterns in {
        'first_name': ('first name', 'given name', 'voornaam'),
        'last_name': ('last name', 'surname', 'family name', 'achternaam'),
        'email': ('email', 'e-mail'),
        'phone': ('phone', 'telephone', 'mobile', 'telefoon'),
        'city': ('city', 'woonplaats'),
        'country': ('country', 'land van verblijf'),
        'linkedin_url': ('linkedin profile', 'linkedin url', 'linkedin.com/in'),
    }.items():
        if any(pattern in state for pattern in patterns) and profile.get(key):
            result[key] = str(profile[key])
    if any(pattern in state for pattern in ('full name', 'your name', 'volledige naam')):
        name = ' '.join(str(profile.get(key) or '').strip() for key in ('first_name', 'last_name')).strip()
        if name:
            result['full_name'] = name
    if any(pattern in state for pattern in ('cv or resume', 'resume', 'résumé', 'curriculum vitae', 'upload your cv')):
        path = Path(resume_path) if resume_path else None
        if path and path.is_file():
            result['resume_path'] = str(path.resolve())
    return result


def _failure_summary(history):
    """Compact cross-round failure memory without retaining personal values."""
    counts = {}
    for item in history:
        if not item.get('error_type'):
            continue
        key = (item.get('type', ''), item.get('target', ''), item.get('error_type', ''), item.get('error', ''))
        counts[key] = counts.get(key, 0) + 1
    return [dict(type=k[0], target=k[1], error_type=k[2], error=k[3], occurrences=n)
            for k, n in sorted(counts.items(), key=lambda pair: pair[1], reverse=True)[:12]]


def _form_metadata(page):
    """Bounded live main-frame metadata; never report contact values."""
    try:
        return page.evaluate("""() => {
          const els = [...document.querySelectorAll('input:not([type=hidden]),select,textarea,button,[role=combobox],[role=radio],[role=checkbox]')];
          const visible = el => { const r=el.getBoundingClientRect(), s=getComputedStyle(el); return !!(r.width && r.height && s.display !== 'none' && s.visibility !== 'hidden'); };
          const controls = els.map(el => {
            const id=el.id || '', name=el.getAttribute('name') || '';
            const labels=[...(el.labels || [])].map(x => (x.innerText || '').trim()).filter(Boolean);
            const options=el.tagName==='SELECT' ? [...el.options].slice(0,30).map(o=>({label:o.text.trim().slice(0,80),value:o.value,selected:o.selected})) : [];
            return {tag:el.tagName.toLowerCase(), type:el.getAttribute('type') || '', role:el.getAttribute('role') || '', id, name,
              label:labels.join(' / ').slice(0,120), aria_label:el.getAttribute('aria-label') || '', placeholder:el.getAttribute('placeholder') || '',
              visible:visible(el), disabled:!!el.disabled, required:!!el.required || el.getAttribute('aria-required')==='true',
              filled:el.type==='file' ? !!el.files?.length : !!el.value, checked:!!el.checked, options,
              id_unique:!!id && els.filter(x=>x.id===id).length===1,
              name_matches:name ? els.filter(x=>x.getAttribute('name')===name).length : 0};
          });
          controls.sort((a,b)=>Number(b.visible)-Number(a.visible));
          return {frame:'main', total_controls:controls.length, controls:controls.slice(0,80)};
        }""")
    except Exception as error:
        logger.warning('Could not capture structured form metadata: %s', error)
        return {'frame': 'main', 'error': type(error).__name__}


def _request_ai_help(page, profile, resume_path, cover_letter_path, platform="linkedin") -> bool:
    """提取外部页面表单状态 → 发信号 → 等 AI 回复 → 执行指令。"""
    from datetime import datetime

    # P0修复: URL 黑名单检查 — 死胡同页面直接跳过, 不创建 HELP flag
    current_url = (page.url or "").lower()
    for blocked in _BLOCKED_URL_PATTERNS:
        if blocked in current_url:
            logger.info(f"⏭ Auto-skipping blocked URL: {current_url[:120]} (matched '{blocked}')")
            return False

    data_dir = _external_data_dir(platform)
    data_dir.mkdir(parents=True, exist_ok=True)
    help_file = data_dir / "ext_help.json"
    need_flag = data_dir / "ext_help_needed.flag"
    done_flag = data_dir / "ext_help_done.flag"
    instructions_file = data_dir / "ext_instructions.json"

    for f in [need_flag, done_flag]:
        if f.exists(): f.unlink()

    # 提取页面状态
    form_state = _extract_page_state(page)
    help_data = {
        "form_state": form_state,
        "known_answers": _matched_known_answers(form_state),
        "profile_fields": _matched_profile_fields(form_state, profile, resume_path),
        "context_map": "data/applicant_context/README.md",
        "recent_actions": getattr(page.context, "_external_recent_actions", [])[-12:],
        "failure_summary": _failure_summary(getattr(page.context, "_external_recent_actions", [])),
        "form_metadata": _form_metadata(page),
        "no_progress_seconds": getattr(page.context, "_external_no_progress_seconds", 0),
        "url": page.url,
        "timestamp": datetime.now().isoformat(),
    }
    with open(help_file, "w", encoding="utf-8") as f:
        json.dump(help_data, f, ensure_ascii=False, indent=2)

    need_flag.write_text(datetime.now().isoformat(), encoding="utf-8")
    logger.info("🆘 External AI help needed! (waiting up to 5min)")

    # 等待 AI 回复 (硬超时: 300s, 用 time.monotonic 防止 sleep 卡死)
    start = time.monotonic()
    last_logged = 0
    while not done_flag.exists():
        try: time.sleep(1)
        except: pass
        elapsed = time.monotonic() - start
        if int(elapsed) >= last_logged + 30:
            logger.info(f"   ...waiting for AI ({int(elapsed)}s)")
            last_logged = int(elapsed)
        if elapsed > 300 or time.monotonic() >= getattr(page.context, "_external_no_progress_deadline", float("inf")):
            logger.error("AI help timeout (5min)")
            # 超时后清理残留 flag, 防止污染下次请求 (P0-1)
            for f in [need_flag, help_file]:
                try:
                    if f.exists(): f.unlink()
                except FileNotFoundError: pass
            return False

    # 读取并执行 AI 指令
    if not instructions_file.exists():
        try: done_flag.unlink()
        except FileNotFoundError: pass
        return False
    try:
        with open(instructions_file, "r", encoding="utf-8") as f:
            instructions = json.load(f)
    except Exception as e:
        logger.error(f"Bad instructions: {e}"); return False

    if instructions.get("needs_user"):
        logger.warning(f"External AI needs user input: {instructions.get('questions', [])}")
        try: done_flag.unlink()
        except FileNotFoundError: pass
        for f in [need_flag, instructions_file]:
            try:
                if f.exists(): f.unlink()
            except FileNotFoundError: pass
        # The supervisor may ask the person to solve a visible CAPTCHA, but it
        # may also ask for missing profile answers.  The question text alone
        # is not sufficient evidence for a handoff: inspect the live page and
        # hand off only when an interactive challenge is actually visible.
        try:
            visible_reason = blocking_reason(page)
        except Exception:
            visible_reason = ""
        if visible_reason and _external_form_ready_for_handoff(page):
            raise ExternalApplyHandoff(visible_reason)
        if visible_reason:
            logger.info(
                "External AI reported a challenge before the form was ready; suppressing human handoff"
            )
        raise ExternalApplyHandoff("agent_needs_user")

    actions = instructions.get("actions", [])
    logger.info(f"External AI: executing {len(actions)} actions...")

    # P0修复: 每步指令执行前检查页面是否存活
    current_page = page
    for i, action in enumerate(actions):
        recovered_page = _recover_external_page(current_page)
        if recovered_page is None:
            logger.error(f"Cannot recover page — aborting remaining {len(actions)-i} actions")
            break
        current_page = recovered_page

        try:
            execute_action(current_page, action)
            history = getattr(current_page.context, "_external_recent_actions", [])
            history.append({"type": action.get("type", ""), "target": action.get("target", ""),
                            "button": action.get("value", "") if action.get("type") == "click_button" else ""})
            current_page.context._external_recent_actions = history[-80:]
            if action.get('type') == 'click_button' and str(action.get('value', '')).strip().lower() in ('apply', 'apply now', 'solliciteren'):
                # This often navigates asynchronously; re-read the new form
                # instead of applying selectors from the previous document.
                current_page.wait_for_timeout(1200)
                logger.info('Application entry clicked; re-reading page before filling')
                break
        except (ExternalApplyHandoff, ExternalApplyUnconfirmed):
            raise
        except Exception as e:
            if getattr(current_page.context, '_external_submit_attempted', False):
                raise ExternalApplyUnconfirmed('action_failed_after_submit_attempt') from e
            logger.warning("External action could not be executed: %s — %s", action.get('type'), e)
            history = getattr(current_page.context, "_external_recent_actions", [])
            history.append({"type": action.get("type", ""), "target": action.get("target", ""),
                            "button": action.get("value", "") if action.get("type") == "click_button" else "",
                            "option": str(action.get("value", ""))[:100] if action.get("type") in ('select_option', 'fill_combobox') else "",
                            "error_type": type(e).__name__,
                            "error": str(e)[:200]})
            current_page.context._external_recent_actions = history[-80:]
            break

    try: done_flag.unlink()
    except FileNotFoundError: pass
    for f in [need_flag, instructions_file]:
        try:
            if f.exists(): f.unlink()
        except FileNotFoundError: pass
    return len(actions) > 0


def _extract_page_state(page) -> str:
    """提取外部网站的完整表单状态文本，供 AI 分析。"""
    lines = [frame_state(page), f"URL: {page.url[:150]}", ""]

    try:
        body = page.inner_text("body")
        # 截取关键部分（开头和包含 form/apply 的部分）
        if len(body) > 8000:
            # 优先保留含关键词的部分
            keywords = ["apply", "first name", "last name", "email", "phone", "upload",
                       "submit", "resume", "cv", "name", "address"]
            important = []
            for i, line_text in enumerate(body.split("\n")):
                line_lower = line_text.lower()
                if any(kw in line_lower for kw in keywords):
                    start = max(0, i - 2)
                    end = min(len(body.split("\n")), i + 5)
                    important.append("\n".join(body.split("\n")[start:end]))
            if important:
                body = "\n---\n".join(important[:30])  # 最多30个关键段落
            else:
                body = body[:6000]
        lines.append(body)
    except Exception as e:
        lines.append(f"ERROR extracting body: {e}")

    lines.append("\n=== PAGE HTML EXCERPT ===")
    try:
        html = page.content()
        # Keep the prompt bounded while preserving the page body/source signal.
        lines.append(html[:6000])
    except Exception as e:
        lines.append(f"ERROR extracting html: {e}")

    # 额外：提取所有可见 input/select/button
    lines.append("\n=== FORM ELEMENTS ===")
    try:
        for el in page.query_selector_all("input:not([type='hidden']), select, textarea, button"):
            if not el.is_visible(): continue
            tag = el.evaluate("el => el.tagName")
            itype = el.get_attribute("type") or ""
            name = el.get_attribute("name") or ""
            placeholder = el.get_attribute("placeholder") or ""
            value = el.get_attribute("value") or ""
            role = el.get_attribute("role") or ""
            label = _get_label(el)
            txt = (el.inner_text() or "").strip()[:50]
            if itype in ("submit", "hidden", "button"): continue
            lines.append(f"[{tag}/{itype}] role='{role}' name='{name[:30]}' value='{value[:30]}' "
                        f"placeholder='{placeholder[:30]}' label='{label[:40]}' text='{txt}'")
    except Exception as e:
        lines.append(f"ERROR: {e}")

    lines.append("\n=== ASSOCIATED CHOICES ===")
    try:
        for el in page.query_selector_all("input[type='checkbox'], input[type='radio']"):
            info = el.evaluate("""el => ({id: el.id, name: el.name, value: el.value,
                checked: el.checked, labels: Array.from(el.labels || []).map(l => l.innerText.trim()).filter(Boolean)})""")
            if info['labels']:
                lines.append(f"[choice] type='{el.get_attribute('type')}' id='{info['id'][:80]}' "
                             f"name='{info['name'][:80]}' value='{info['value'][:80]}' "
                             f"checked={info['checked']} labels={info['labels']}")
    except Exception as e:
        lines.append(f"ERROR: {e}")

    lines.append("\n=== KEY OUTER HTML ===")
    try:
        selectors = "form, input:not([type='hidden']), select, textarea, button, a[href]"
        for el in page.query_selector_all(selectors)[:80]:
            try:
                html = el.evaluate("el => el.outerHTML")
                if html:
                    html = " ".join(html.split())[:500]
                    lines.append(f"[visible={str(el.is_visible()).lower()}] {html}")
            except Exception:
                continue
    except Exception as e:
        lines.append(f"ERROR: {e}")

    # --- COMBOBOX FIELDS (separate section — require click→type→ArrowDown→Enter) ---
    lines.append("\n=== COMBOBOX FIELDS ===")
    try:
        comboboxes = page.query_selector_all("input[role='combobox']")
        for el in comboboxes:
            if not el.is_visible(): continue
            name = el.get_attribute("name") or ""
            placeholder = el.get_attribute("placeholder") or ""
            value = el.get_attribute("value") or ""
            aria_label = el.get_attribute("aria-label") or ""
            lines.append(f"[combobox] name='{name[:30]}' value='{value[:30]}' "
                        f"placeholder='{placeholder[:30]}' aria-label='{aria_label[:40]}'")
        if not comboboxes:
            lines.append("(none detected)")
    except Exception as e:
        lines.append(f"ERROR: {e}")

    # --- FILE UPLOAD STATUS ---
    lines.append("\n=== FILE UPLOAD STATUS ===")
    try:
        file_inputs = page.query_selector_all("input[type='file']")
        for el in file_inputs:
            name = el.get_attribute("name") or ""
            try:
                files_count = el.evaluate("el => el.files.length")
            except Exception:
                files_count = "?"
            lines.append(f"[file-input] name='{name[:30]}' files_selected={files_count} "
                        f"visible={el.is_visible()}")
        # Some ATS replace file input with a display element after upload
        for el in page.query_selector_all("[class*='resume'], [class*='attachment'], "
                                           "[class*='file-name'], [class*='uploaded']"):
            try:
                if el.is_visible():
                    txt = el.inner_text().strip()[:60]
                    if txt and any(x in txt.lower() for x in
                                   ("resume", "cv", ".pdf", ".docx", "upload")):
                        lines.append(f"[file-display] '{txt}'")
            except Exception:
                pass
        if not file_inputs:
            lines.append("(no file input detected — may use custom upload widget)")
    except Exception as e:
        lines.append(f"ERROR: {e}")

    # --- VALIDATION ERRORS ---
    lines.append("\n=== VALIDATION ERRORS ===")
    try:
        error_selectors = [
            ".field-error", "[class*='error']", "[class*='Error']",
            "[aria-invalid='true']", "[role='alert']",
            ".parsley-error", ".invalid-feedback", ".validation-error",
            "[class*='field-validation-error']",
        ]
        found_errors = False
        for sel in error_selectors:
            for el in page.query_selector_all(sel):
                try:
                    if el.is_visible():
                        txt = el.inner_text().strip()[:120]
                        if txt:
                            lines.append(f"  [{sel}] {txt}")
                            found_errors = True
                except Exception:
                    pass
        if not found_errors:
            lines.append("(no validation errors detected)")
    except Exception as e:
        lines.append(f"ERROR: {e}")

    return "\n".join(lines)


def _get_label(el) -> str:
    """获取表单元素的 label。"""
    try:
        el_id = el.get_attribute("id") or ""
        if el_id:
            label_el = el.evaluate_handle(f'() => document.querySelector("label[for=\'{el_id}\']")')
            try: return label_el.inner_text().strip()[:60]
            except: pass
        aria = el.get_attribute("aria-label") or ""
        if aria: return aria[:60]
        name = el.get_attribute("name") or ""
        if name: return name[:60]
    except: pass
    return ""


# ── React-safe fill (bypass React synthetic events) ──

def _react_fill(page: Page, selector: str, value: str) -> bool:
    """React-safe fill: use native value setter + dispatch synthetic events.

    Playwright's fill() sets the DOM value but does not trigger React's
    synthetic event system, causing React-controlled inputs to reject the value.
    This function directly invokes the native setter and dispatches the full
    event sequence React expects (focus, input, change, blur).
    """
    try:
        result = page.evaluate("""([sel, val]) => {
            const el = document.querySelector(sel);
            if (!el) return false;

            // Focus
            el.dispatchEvent(new Event('focus', { bubbles: true }));

            // Use native value setter (bypasses React's setter override)
            const nativeSetter = Object.getOwnPropertyDescriptor(
                HTMLInputElement.prototype, 'value'
            ).set;
            nativeSetter.call(el, val);

            // Dispatch input event with inputType (React listens for this)
            el.dispatchEvent(new InputEvent('input', {
                bubbles: true,
                cancelable: true,
                inputType: 'insertText',
                data: val,
            }));

            // Dispatch change event
            el.dispatchEvent(new Event('change', { bubbles: true }));

            // Blur
            el.dispatchEvent(new Event('blur', { bubbles: true }));

            // Update React's internal tracking if __reactProps$ exists
            for (const key in el) {
                if (key.startsWith('__reactProps$')) {
                    el[key].value = val;
                    break;
                }
            }

            return true;
        }""", [selector, value])

        if result:
            logger.info(f"React-filled: {selector} = '{value[:40]}'")
        else:
            logger.warning(f"React-fill failed: element not found: {selector}")
        return result
    except Exception as e:
        logger.warning(f"React-fill error for '{selector}': {e}")
        return False


# ── Combobox fill ──

def _fill_combobox(page: Page, selector: str, value: str) -> bool:
    """Fill a combobox (role='combobox') by typing and selecting from dropdown.

    Combobox fields don't accept direct value setting. They require:
    1. Click to focus (opens dropdown)
    2. Type to filter options
    3. ArrowDown to navigate to the first match
    4. Enter to select
    """
    try:
        el = page.query_selector(selector)
        if not el or not el.is_visible():
            logger.warning(f"Combobox not found or not visible: {selector}")
            return False

        # Click to focus and open dropdown
        el.click()
        page.wait_for_timeout(300)

        # Clear existing value
        el.fill("")
        page.wait_for_timeout(200)

        # Type the value character by character with delay to trigger filtering
        el.type(value, delay=100)
        page.wait_for_timeout(800)  # wait for dropdown to populate

        # ArrowDown + Enter to select the first filtered option
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(200)
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)

        logger.info(f"Combobox filled: '{selector}' with '{value[:40]}'")
        return True
    except Exception as e:
        logger.warning(f"Combobox fill error for '{selector}': {e}")
        return False


# ── Validation error detection ──

def _check_validation_errors(page: Page) -> list:
    """Detect visible validation error messages on the page.

    Returns a list of error message strings. Used by _ai_driven_apply
    to detect and report validation failures between AI rounds.
    """
    errors = []
    error_selectors = [
        ".field-error",            # Greenhouse / common pattern
        "[class*='error']",        # Generic error class
        "[class*='Error']",        # Capitalized variant
        "[aria-invalid='true']",   # ARIA invalid state
        "[role='alert']",          # ARIA alert role
        ".parsley-error",          # Parsley validation
        ".invalid-feedback",       # Bootstrap invalid feedback
        ".validation-error",       # Generic validation error
        "[class*='field-validation-error']",  # ASP.NET MVC
        ".error-message",          # Common error message class
        ".errorlist",              # Django error list
    ]
    try:
        for sel in error_selectors:
            for el in page.query_selector_all(sel):
                try:
                    if el.is_visible():
                        txt = el.inner_text().strip()[:150]
                        if txt and txt not in errors:
                            errors.append(txt)
                except Exception:
                    pass

        # Also check for aria-describedby on invalid elements
        invalid_inputs = page.query_selector_all("[aria-invalid='true']")
        for el in invalid_inputs:
            try:
                describedby = el.get_attribute("aria-describedby") or ""
                if describedby:
                    desc_el = page.query_selector(f"#{describedby}")
                    if desc_el:
                        txt = desc_el.inner_text().strip()[:150]
                        if txt and txt not in errors:
                            errors.append(txt)
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"Validation check error: {e}")

    if errors:
        logger.info(f"Validation errors detected: {len(errors)}")
        for err in errors:
            logger.info(f"  - {err[:80]}")

    return errors


# ── Safe file upload ──

def _upload_file_safe(page: Page, selector: str, file_path: str) -> bool:
    """Safe file upload with post-upload verification and DOM change handling.

    After set_input_files(), some ATS (e.g. Greenhouse) replace the
    <input type='file'> with a filename display <div>, making the
    selector invalid for subsequent operations.

    Steps:
    1. Wait for the file input to be present
    2. Upload via set_input_files
    3. Wait briefly for the ATS to process
    4. Verify: check if input still exists OR if a filename display appeared
    """
    from pathlib import Path

    abs_path = str(Path(file_path).resolve())
    if not Path(abs_path).exists():
        logger.error(f"File not found: {abs_path}")
        return False

    try:
        el = page.query_selector(selector)
        if not el:
            logger.warning(f"File input not found: {selector}")
            return False

        # Upload (works even on hidden file inputs)
        set_file_input(el, abs_path)
        logger.info(f"File uploaded via '{selector}': {Path(abs_path).name}")

        # Wait for ATS processing (some ATS need time to show the filename)
        page.wait_for_timeout(2000)

        # Verify upload
        # Check 1: did the input get replaced with a display element?
        file_displays = page.query_selector_all(
            "[class*='file-name'], [class*='uploaded'], [class*='attachment'], "
            "[class*='resume'], .dz-filename, [class*='file-info']"
        )
        for display in file_displays:
            try:
                if display.is_visible():
                    text = display.inner_text().strip().lower()
                    if Path(abs_path).name.lower() in text or ".pdf" in text:
                        logger.info(f"Upload confirmed via display element: '{text[:60]}'")
                        return True
            except Exception:
                pass

        # Check 2: does the original input still exist with files selected?
        input_still_exists = page.query_selector(selector)
        if input_still_exists:
            try:
                files_count = input_still_exists.evaluate("el => el.files.length")
                if files_count > 0:
                    logger.info(f"Upload confirmed: {files_count} file(s) on input")
                    return True
            except Exception:
                pass

        # Check 3: optimistic — the set_input_files likely worked
        logger.info("Upload performed but could not verify — may still be processing")
        return True

    except Exception as e:
        logger.warning(f"File upload error for '{selector}': {e}")
        return False


# ── Extract form state (external) ──
extract_form_state = _extract_page_state
