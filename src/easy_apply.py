"""
Easy Apply 投递模块 — 处理 LinkedIn Easy Apply 多步表单流程。
基于实战探索结果重构:
  1. 多步表单 (1/N pages) — Next → Review → Submit
  2. 在弹窗容器内查找元素 (修复 BUG-004)
"""

import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from playwright.sync_api import Page, ElementHandle

logger = logging.getLogger("linkedin_autoapply.easy_apply")

from .i18n import EASY_APPLY_TEXTS, NEXT_TEXTS, REVIEW_TEXTS, SUBMIT_TEXTS, DONE_TEXTS, SUCCESS_TEXTS, FIELD_LABELS, EXTERNAL_APPLY_TEXTS
from .external_apply import _react_fill, _fill_combobox

EASY_APPLY_BUTTON_SELECTORS = [
    # LinkedIn 搜索卡片上的 Easy Apply 按钮
    ".jobs-s-apply",                     # 新 UI: <div class=\"jobs-s-apply\">
    "button.jobs-apply-button",
    ".jobs-apply-button",
    '[data-control-name="jobdetails_topcard_apply_button"]',
    # 全页面视图上的按钮
    ".job-details-jobs-unified-top-card__apply-button",
    ".jobs-unified-top-card__apply-button",
]

MODAL_SELECTORS = [
    "dialog",                        # LinkedIn 使用原生 <dialog> 元素!
    "div[role='dialog']",
    ".jobs-easy-apply-modal",
    "[class*='jobs-easy-apply']",
    ".artdeco-modal",
    "[class*='modal']",
    "[aria-labelledby]",             # 通用弹窗标识
]


def _get_modal(page: Page) -> Optional[ElementHandle]:
    """获取 Easy Apply 弹窗容器。"""
    for sel in MODAL_SELECTORS:
        # A dismiss action can open a second confirmation dialog above the
        # Easy Apply dialog.  The last visible match is the topmost one and
        # must be inspected/closed first.
        visible = []
        for modal in page.query_selector_all(sel):
            try:
                if modal.is_visible():
                    visible.append(modal)
            except Exception:
                continue
        if visible:
            return visible[-1]
    return None


def is_easy_apply_available(page: Page) -> bool:
    """检测当前岗位是否支持 Easy Apply (多语言)。

    先检查已知选择器，再遍历所有按钮文本进行回退匹配。
    排除 External Apply 关键词防止误判。
    """
    def _is_external_text(text: str) -> bool:
        """检查文本是否包含外部申请关键词。"""
        for kw in EXTERNAL_APPLY_TEXTS:
            if kw in text:
                return True
        return False

    # 方式 1: 已知 CSS 选择器
    for selector in EASY_APPLY_BUTTON_SELECTORS:
        btn = page.query_selector(selector)
        if btn and btn.is_visible():
            # inner button aria (.jobs-s-apply is a div, aria-label is on child button)
            inner_btn = btn.query_selector('button')
            inner_aria = (inner_btn.get_attribute('aria-label') or '').lower() if inner_btn else ''
            combined = f"{btn.inner_text().strip().lower()} {(btn.get_attribute('aria-label') or '').lower()} {inner_aria}"
            # 排除 External Apply: LinkedIn aria 标注了 on company website
            if 'on company website' in combined or 'on company site' in combined:
                continue
            if _is_external_text(combined):
                continue
            for kw in EASY_APPLY_TEXTS:
                if kw in combined:
                    return True

    # 方式 2: 回退 — 遍历所有按钮，匹配多语言 Easy Apply 文本
    buttons = page.query_selector_all("button")
    for btn in buttons:
        try:
            if not btn.is_visible():
                continue
            combined = f"{btn.inner_text().strip().lower()} {(btn.get_attribute('aria-label') or '').lower()}"
            # 跳过搜索筛选器按钮
            if 'filter' in combined: continue
            # 排除 External Apply: LinkedIn aria 标注了 on company website
            if 'on company website' in combined or 'on company site' in combined:
                continue
            # 排除 External Apply 关键词 (防止 "Apply" 误判)
            if _is_external_text(combined):
                continue
            for kw in EASY_APPLY_TEXTS:
                if kw in combined:
                    return True
        except Exception:
            continue

    return False


def click_easy_apply(page: Page) -> bool:
    """点击 Easy Apply 按钮启动申请流程 (多语言)。"""
    def _is_external_text(text: str) -> bool:
        for kw in EXTERNAL_APPLY_TEXTS:
            if kw in text: return True
        return False

    # 方式 1: 已知 CSS 选择器
    for selector in EASY_APPLY_BUTTON_SELECTORS:
        btn = page.query_selector(selector)
        if btn and btn.is_visible():
            try:
                combined = f"{btn.inner_text().strip().lower()} {(btn.get_attribute('aria-label') or '').lower()}"
                if _is_external_text(combined):
                    continue
                btn.click()
                page.wait_for_timeout(2000)
                logger.info("Clicked Easy Apply (selector)")
                return True
            except Exception as e:
                logger.warning(f"Click via selector failed: {e}")

    # 方式 2: 遍历所有按钮，按文本匹配
    buttons = page.query_selector_all("button")
    for btn in buttons:
        try:
            if not btn.is_visible() or btn.is_disabled():
                continue
            combined = f"{btn.inner_text().strip().lower()} {(btn.get_attribute('aria-label') or '').lower()}"
            # 排除 External Apply 关键词
            if _is_external_text(combined):
                continue
            for kw in EASY_APPLY_TEXTS:
                if kw in combined:
                    btn.click()
                    page.wait_for_timeout(2000)
                    logger.info(f"Clicked Easy Apply (text: '{combined[:60]}')")
                    return True
        except Exception as e:
            continue

    logger.warning("No clickable Easy Apply button found")
    return False


def get_current_step_info(page: Page) -> tuple[int, int]:
    """获取 Easy Apply 当前步骤信息。

    Returns:
        (current_page, total_pages) — 例如 (1, 4) 表示 1/4 pages
        如果无法检测则返回 (1, 1)
    """
    modal = _get_modal(page)
    if not modal:
        return (1, 1)

    try:
        text = modal.inner_text()
        import re
        match = re.search(r'(\d+)\s*/\s*(\d+)\s*pages?', text)
        if match:
            return (int(match.group(1)), int(match.group(2)))
    except Exception:
        pass

    return (1, 1)


def find_step_button(page: Page) -> Optional[ElementHandle]:
    """在弹窗中找到当前步骤的推进按钮。

    优先级: Submit application > Review > Next > Continue > Done
    仅在弹窗容器内搜索 (修复 BUG-004)
    """
    modal = _get_modal(page)
    search_root = modal if modal else page

    # The final Review screen must never fall back to a stale/hidden Next
    # control.  LinkedIn can leave an old page mounted while the review page
    # is being rendered; clicking that old Next button sends the form back to
    # an earlier step and creates a false cycle.  On the final step accept
    # only a visible submit control and otherwise let the caller request help.
    try:
        current_step, total_steps = get_current_step_info(page)
    except Exception:
        current_step, total_steps = (1, 1)
    # Some LinkedIn variants omit the ``x/y pages`` indicator on Review. In
    # that case modal text is the second signal; a stale Next must still never
    # be used as a fallback.
    try:
        modal_text = (search_root.inner_text() or "").casefold()
    except Exception:
        modal_text = ""
    review_screen = any(marker in modal_text for marker in (
        "review your application", "review application", "review and submit",
        "check your application", "review your answers",
    ))
    if (total_steps > 1 and current_step == total_steps) or review_screen:
        for btn in search_root.query_selector_all("button"):
            try:
                if not btn.is_visible() or btn.is_disabled():
                    continue
                text = " ".join((
                    btn.inner_text() or "",
                    btn.get_attribute("aria-label") or "",
                )).strip().casefold()
                # This branch is already restricted to the final/review
                # screen, so the short locale label is safe to accept.
                if any(token in text for token in (
                    "submit application", "sollicitatie indienen", "提交申请",
                    # Some locales render only the short label.
                    "submit",
                )):
                    return btn
            except Exception:
                continue
        logger.warning(
            "Easy Apply review/final step %s/%s has no visible submit control; refusing to click Next",
            current_step,
            total_steps,
        )
        return None

    # 多语言按钮文本：Submit > Review > Next > Done
    priority_texts = SUBMIT_TEXTS + REVIEW_TEXTS + NEXT_TEXTS + DONE_TEXTS

    for text in priority_texts:
        buttons = search_root.query_selector_all("button")
        for btn in buttons:
            try:
                btn_text = btn.inner_text().strip().lower()
                if text in btn_text:
                    if btn.is_visible() and not btn.is_disabled():
                        return btn
            except Exception:
                continue

    # 回退：找 footer 中的按钮
    footer_btns = search_root.query_selector_all("footer button")
    for btn in footer_btns:
        try:
            if btn.is_visible() and not btn.is_disabled():
                btn_text = btn.inner_text().strip()
                if btn_text and len(btn_text) < 30:
                    return btn
        except Exception:
            continue

    return None


def click_step_button(page: Page) -> bool:
    """点击当前步骤的推进按钮。

    Returns:
        True 如果按钮被点击
    """
    btn = find_step_button(page)
    if btn:
        try:
            btn_text = btn.inner_text().strip()[:50]
            logger.info(f"Clicking: '{btn_text}'")
            if "submit" in btn_text.casefold() or "提交申请" in btn_text:
                page.context._easy_apply_submit_clicked = True
            try:
                btn.scroll_into_view_if_needed()
                btn.click()
            except Exception as first_error:
                # LinkedIn's native <dialog> can briefly intercept the normal
                # pointer path even though the footer button is visible.  A
                # forced click is still tied to the inspected button and is
                # safer than asking the AI to guess a new selector.
                logger.debug("Normal Easy Apply button click failed: %s", first_error)
                try:
                    btn.click(force=True)
                except Exception:
                    # Last resort: dispatch the button's own DOM click handler.
                    btn.evaluate("el => el.click()")
            page.wait_for_timeout(2500)
            return True
        except Exception as e:
            logger.warning(f"Button click failed: {e}")
    else:
        # 调试：列出弹窗中所有可见按钮
        modal = _get_modal(page)
        if modal:
            buttons = modal.query_selector_all("button")
            logger.debug(f"No step button found. {len(buttons)} buttons in modal:")
            for b in buttons[:10]:
                try:
                    if b.is_visible():
                        txt = b.inner_text().strip()[:80]
                        dis = b.is_disabled()
                        logger.debug(f"  [{'X' if dis else ' '}] '{txt}'")
                except Exception:
                    pass
    return False


def _document_kind(text: str) -> str:
    """Classify an upload control from its visible/accessible label.

    LinkedIn occasionally asks for supporting documents after the CV step.  A
    generic ``Upload`` button is treated as a CV upload; a semantic label is
    required before selecting the diploma or transcript so that those files
    are never swapped accidentally.
    """
    value = (text or "").lower()
    if any(k in value for k in (
        "cover letter", "cover_letter", "coverletter", "motivation",
        "motivatie", "supporting document",
    )):
        return "cover_letter"
    if any(k in value for k in (
        "transcript", "grade list", "grades", "academic record", "marks",
        "cijferlijst", "puntenlijst", "成绩单", "成绩", "学分",
    )):
        return "transcript"
    if any(k in value for k in (
        "diploma", "degree certificate", "degree", "certificate",
        "graduation", "education proof", "diplom",
    )):
        return "diploma"
    if any(k in value for k in (
        "resume", "cv", "curriculum vitae", "resumé", "简历",
    )):
        return "resume"
    return "resume"


def _upload_control_semantic(control: ElementHandle) -> str:
    """Return only the label that belongs to an upload control.

    Do not use ``_get_element_context`` here.  LinkedIn often renders the
    resume and cover-letter controls in one shared wrapper, so the wrapper
    text can contain *both* words even though the button represents only one
    of them.  Feeding that broad text to ``_document_kind`` was able to turn
    a resume button into a cover-letter button (or vice versa).
    """
    parts: list[str] = []
    for attr in ("aria-label", "title", "id", "name", "data-test-id", "data-control-name"):
        try:
            value = control.get_attribute(attr) or ""
        except Exception:
            value = ""
        if value:
            parts.append(value)
    try:
        text = (control.inner_text() or "").strip()
    except Exception:
        text = ""
    if text:
        parts.insert(0, text)

    # A real <label> is still local to the control and is safe to use.  Do
    # not walk arbitrary parent divs; those are the source of cross-field
    # resume/cover-letter contamination.
    try:
        label = control.evaluate("el => el.closest('label')?.innerText || ''") or ""
        if label.strip():
            parts.append(label.strip())
    except Exception:
        pass
    return " ".join(parts).strip()


def _document_kind_for_control(control: ElementHandle) -> tuple[Optional[str], str]:
    """Classify an upload button without allowing an ambiguous wrapper.

    ``None`` means the control is ambiguous and must not be used.  A generic
    ``Upload`` button with no document-specific label remains a resume
    control for backwards compatibility, but a label containing both resume
    and cover-letter terms is rejected rather than guessing.
    """
    semantic = _upload_control_semantic(control)
    value = semantic.casefold()
    cover_tokens = (
        "cover letter", "cover_letter", "coverletter", "motivation",
        "motivatie", "supporting document",
    )
    resume_tokens = ("resume", "cv", "curriculum vitae", "resumé", "简历")
    has_cover = any(token in value for token in cover_tokens)
    has_resume = any(token in value for token in resume_tokens)
    if has_cover and has_resume:
        return None, semantic
    return ("cover_letter" if has_cover else "resume"), semantic


def _attached_file_names(control: ElementHandle) -> list[str]:
    """Read the browser's actual FileList, never the recent-file UI text."""
    try:
        values = control.evaluate(
            "el => Array.from(el.files || []).map(file => String(file.name || ''))"
        )
        return [str(value).strip() for value in (values or []) if str(value).strip()]
    except Exception:
        # Chromium can expose the path through input_value even when a custom
        # widget temporarily hides FileList.  This is only a fallback; the
        # exact basename is still required.
        try:
            value = control.input_value() or ""
            name = Path(value.replace("\\", "/")).name
            return [name] if name else []
        except Exception:
            return []


def _verify_document_upload(
    search_root,
    kind: str,
    expected_path: str,
    preferred: Optional[ElementHandle] = None,
    *,
    diagnostic: bool = True,
    log_failure: bool = True,
) -> bool:
    """Verify that the configured basename, not a recent file, is attached."""
    expected_name = Path(expected_path).name.casefold()
    if not expected_name:
        return False
    if preferred is not None:
        # When we know the exact input that was targeted, do not accept the
        # expected basename from a different input.  This prevents a stale
        # cover-letter FileList elsewhere in the modal from masking a bad CV
        # selection.  A detached/replaced node falls through to the live
        # input scan below.
        preferred_names = _attached_file_names(preferred)
        if preferred_names:
            exact = (
                len(preferred_names) == 1
                and Path(preferred_names[0].replace("\\", "/")).name.casefold() == expected_name
            )
            if exact:
                logger.info(
                    "Easy Apply %s upload verified: expected=%s attached=%s",
                    kind, Path(expected_path).name, preferred_names,
                )
            else:
                logger.error(
                    "Easy Apply %s control contains the wrong file: expected=%s attached=%s",
                    kind, Path(expected_path).name, preferred_names,
                )
            return exact
    candidates: list[ElementHandle] = []
    if preferred is not None:
        candidates.append(preferred)
    try:
        candidates.extend(search_root.query_selector_all("input[type='file']"))
    except Exception:
        pass
    seen: set[int] = set()
    for control in candidates:
        try:
            marker = id(control)
            if marker in seen:
                continue
            seen.add(marker)
            names = _attached_file_names(control)
            if any(Path(name.replace("\\", "/")).name.casefold() == expected_name for name in names):
                logger.info(
                    "Easy Apply %s upload verified: expected=%s attached=%s",
                    kind, Path(expected_path).name, names,
                )
                return True
        except Exception:
            continue
    # Keep a compact DOM snapshot in the log when the exact FileList cannot be
    # found.  This distinguishes a detached/replaced input from a form that
    # only exposes a recent-file widget, without accepting either as proof.
    if diagnostic:
        try:
            diagnostics = []
            for index, control in enumerate(search_root.query_selector_all("input[type='file']")[:8]):
                names = _attached_file_names(control)
                diagnostics.append({
                    "index": index,
                    "id": control.get_attribute("id") or "",
                    "name": control.get_attribute("name") or "",
                    "accept": control.get_attribute("accept") or "",
                    "visible": bool(control.is_visible()),
                    "disabled": bool(control.is_disabled()),
                    "files": names,
                })
            logger.warning(
                "Easy Apply %s upload controls at verification failure: expected=%s controls=%s",
                kind, Path(expected_path).name, diagnostics,
            )
            try:
                visible_text = " ".join(str(search_root.inner_text() or "").split())
                tokens = ("resume", "cv", "curriculum", "upload", "attach", "pdf", "document")
                snippets = [
                    visible_text[max(0, match.start() - 90):match.start() + 220]
                    for token in tokens
                    for match in re.finditer(re.escape(token), visible_text, flags=re.IGNORECASE)
                ]
                logger.warning(
                    "Easy Apply %s upload visible attachment evidence: %s",
                    kind, snippets[:8],
                )
            except Exception as exc:
                logger.debug("Easy Apply upload visible-text diagnostic failed: %s", exc)
        except Exception as exc:
            logger.debug("Easy Apply upload diagnostic snapshot failed: %s", exc)
    if log_failure:
        logger.error(
            "Easy Apply %s upload safety check failed: expected=%s; refusing to submit",
            kind, Path(expected_path).name,
        )
    return False


def _visible_document_upload_evidence(search_root, kind: str, expected_path: str) -> bool:
    """Accept LinkedIn's post-upload widget when it replaces the file input.

    On some Easy Apply steps LinkedIn removes the native file input after the
    upload succeeds and renders the selected basename plus a status label in
    the attachment widget.  The exact basename must still appear in the same
    local text window as the corresponding resume/cover-letter label; a hit in
    the unrelated "recent files" list is not sufficient.
    """
    expected = Path(expected_path).name.casefold()
    if not expected:
        return False
    try:
        text = " ".join(str(search_root.inner_text() or "").split())
    except Exception:
        return False
    if expected not in text.casefold():
        return False
    labels = (
        ("resume", "cv", "curriculum vitae", "resumé")
        if kind == "resume"
        else ("cover letter", "coverletter", "motivation", "motivatie")
    )
    lowered = text.casefold()
    start = 0
    while True:
        index = lowered.find(expected, start)
        if index < 0:
            break
        window = lowered[max(0, index - 260): index + len(expected) + 260]
        if any(label in window for label in labels):
            # Prefer a direct attachment label/status over the historical
            # recent-file list.  The label is deliberately broad because
            # LinkedIn localizes this widget across account settings.
            if (
                "select or upload" in window
                or "uploaded successfully" in window
                or "selected" in window
                or "remove" in window
                or "delete" in window
                or "replace" in window
            ):
                return True
        start = index + len(expected)
    return False


def _verify_document_upload_with_retry(
    search_root,
    kind: str,
    expected_path: str,
    preferred: Optional[ElementHandle] = None,
    *,
    page: Optional[Page] = None,
    attempts: int = 5,
) -> bool:
    """Verify an upload after LinkedIn's asynchronous input replacement.

    LinkedIn often replaces the native ``input[type=file]`` immediately after
    ``set_input_files``.  A 250 ms probe can therefore inspect the detached
    old node and report an empty FileList even though the new node is about to
    receive the file.  Re-scan the live controls with bounded backoff before
    declaring a failure; this does not weaken the exact-basename check.
    """
    delays_ms = (0, 300, 800, 1500, 2500)
    for index in range(max(1, attempts)):
        delay = delays_ms[min(index, len(delays_ms) - 1)]
        if delay:
            try:
                waiter = page or getattr(search_root, "page", None)
                if waiter is not None:
                    waiter.wait_for_timeout(delay)
                else:
                    time.sleep(delay / 1000)
            except Exception:
                time.sleep(delay / 1000)
        if _verify_document_upload(
            search_root,
            kind,
            expected_path,
            preferred if index == 0 else None,
            diagnostic=(index == 0),
            log_failure=(index == max(1, attempts) - 1),
        ):
            if index:
                logger.info(
                    "Easy Apply %s upload verified after async settle: attempt=%s expected=%s",
                    kind, index + 1, Path(expected_path).name,
                )
            return True
        if _visible_document_upload_evidence(search_root, kind, expected_path):
            logger.info(
                "Easy Apply %s upload verified from LinkedIn attachment widget after native input replacement: %s",
                kind, Path(expected_path).name,
            )
            return True
    return False


def _verify_easy_apply_resume(page: Page, resume_path: str) -> bool:
    """Require the configured CV on any visible/current Easy Apply file step."""
    if getattr(page.context, "_easy_apply_resume_upload_failed", False):
        logger.error("Easy Apply resume verification blocked by a prior upload failure")
        return False
    if not resume_path or not Path(resume_path).is_file():
        logger.error("Configured Easy Apply resume is unavailable: %s", resume_path)
        return False
    modal = _get_modal(page)
    search_root = modal if modal else page
    try:
        controls = search_root.query_selector_all("input[type='file']")
    except Exception:
        controls = []
    resume_controls: list[ElementHandle] = []
    for control in controls:
        try:
            el_id = (control.get_attribute("id") or "").lower()
            el_name = (control.get_attribute("name") or "").lower()
            label = _get_input_label(control).lower()
            if _is_cover_letter_file_input(el_id, el_name, label):
                continue
            kind = _document_kind(" ".join((el_id, el_name, label, control.get_attribute("aria-label") or "")))
            if kind == "resume":
                resume_controls.append(control)
        except Exception:
            continue
    if not resume_controls:
        verified = str(getattr(page.context, "_easy_apply_resume_verified_path", "") or "")
        try:
            if verified and Path(verified).resolve() == Path(resume_path).resolve():
                return True
        except Exception:
            pass
        # This step has no CV control.  A cover letter/diploma control must
        # never count as proof that the configured CV is attached.
        return not controls
    return _verify_document_upload(search_root, "resume", resume_path, resume_controls[0])


def _document_paths(profile: Dict[str, Any], resume_path: str) -> Dict[str, str]:
    """Return configured document paths, preserving relative resume behavior."""
    paths = {
        "resume": str(resume_path or ""),
        "diploma": str(profile.get("diploma_path", "") or ""),
        "transcript": str(profile.get("transcript_path", "") or ""),
    }
    resolved: Dict[str, str] = {}
    for kind, raw in paths.items():
        if not raw:
            resolved[kind] = ""
            continue
        try:
            resolved[kind] = str(Path(raw).expanduser().resolve())
        except Exception:
            resolved[kind] = raw
    return resolved


def close_easy_apply_modal(page: Page, reason: str = "") -> bool:
    """Close a failed/abandoned Easy Apply modal before returning to search.

    A failed modal otherwise intercepts the pagination button and makes the
    collector appear to have reached the last page.  We use the visible close
    or cancel control first, then Escape as a safe fallback, and log the final
    state for monitoring.
    """
    modal = _get_modal(page)
    if not modal:
        return True
    labels = (
        "close", "cancel", "discard", "dismiss", "not now", "close dialog",
        "sluiten", "annuleren", "afbreken", "关闭", "取消",
    )
    clicked = False
    clicked_text = ""
    try:
        controls = modal.query_selector_all("button, [role='button'], a")
        logger.debug(
            "Easy Apply modal close candidates: %s",
            [
                " ".join((c.inner_text() or "", c.get_attribute("aria-label") or "")).strip()[:80]
                for c in controls[:20]
            ],
        )
        for control in controls:
            try:
                if control.is_disabled():
                    continue
                text = " ".join((
                    control.inner_text() or "",
                    control.get_attribute("aria-label") or "",
                    control.get_attribute("title") or "",
                )).strip().lower()
                if not any(label in text for label in labels):
                    continue
                try:
                    control.scroll_into_view_if_needed()
                except Exception:
                    pass
                clicked_text = text[:120]
                control.click(force=True)
                clicked = True
                page.wait_for_timeout(500)
                break
            except Exception as exc:
                logger.debug("Easy Apply modal close control failed: %s", exc)
    except Exception as exc:
        logger.debug("Easy Apply modal close scan failed: %s", exc)

    if _get_modal(page):
        try:
            for _ in range(2):
                if not _get_modal(page):
                    break
                page.keyboard.press("Escape")
                page.wait_for_timeout(500)
        except Exception as exc:
            logger.debug("Easy Apply modal Escape failed: %s", exc)

    # Some LinkedIn builds render the dismiss control outside the dialog's
    # subtree or mark it non-visible while the backdrop is animating.  Use a
    # page-level semantic fallback before giving up.
    if _get_modal(page):
        try:
            for control in page.query_selector_all("button, [role='button']"):
                text = " ".join((
                    control.inner_text() or "",
                    control.get_attribute("aria-label") or "",
                    control.get_attribute("title") or "",
                )).strip().lower()
                if "dismiss" in text or "close" in text or "sluiten" in text:
                    if not control.is_disabled():
                        control.click(force=True)
                        page.wait_for_timeout(500)
                        break
        except Exception as exc:
            logger.debug("Page-level Easy Apply dismiss fallback failed: %s", exc)

    if _get_modal(page):
        for selector in (
            ".artdeco-modal__dismiss",
            "button[data-test-modal-close-btn]",
            "button[aria-label='Dismiss']",
            "button[aria-label='Close']",
        ):
            try:
                control = page.query_selector(selector)
                if control:
                    control.click(force=True)
                    page.wait_for_timeout(500)
                    if not _get_modal(page):
                        break
            except Exception as exc:
                logger.debug("Easy Apply selector cleanup failed (%s): %s", selector, exc)

    # Dismiss may open a confirmation dialog ("Discard application?").  Walk
    # the currently visible dialog a few times and prefer the destructive
    # confirmation before trying dismiss again.
    for _ in range(3):
        modal_now = _get_modal(page)
        if not modal_now:
            break
        try:
            candidates = []
            for control in modal_now.query_selector_all("button, [role='button'], a"):
                if control.is_disabled():
                    continue
                text = " ".join((
                    control.inner_text() or "",
                    control.get_attribute("aria-label") or "",
                    control.get_attribute("title") or "",
                )).strip().lower()
                if not text:
                    continue
                rank = next((i for i, token in enumerate((
                    "discard", "abandon", "cancel", "close", "dismiss", "not now",
                    "annuleren", "sluiten", "取消", "关闭",
                )) if token in text), 99)
                if rank < 99:
                    candidates.append((rank, control, text))
            if not candidates:
                page.keyboard.press("Escape")
                page.wait_for_timeout(400)
                continue
            _, control, text = sorted(candidates, key=lambda item: item[0])[0]
            logger.debug("Easy Apply modal confirmation control: %s", text[:120])
            control.click(force=True)
            page.wait_for_timeout(700)
        except Exception as exc:
            logger.debug("Easy Apply modal confirmation cleanup failed: %s", exc)
            break

    closed = _get_modal(page) is None
    logger.info(
        "Easy Apply modal cleanup: %s (reason=%s, control_clicked=%s, clicked=%s)",
        "closed" if closed else "still open", reason or "unspecified", clicked, clicked_text,
    )
    return closed


def handle_easy_apply_step(
    page: Page,
    profile: Dict[str, Any],
    resume_path: str,
    cover_letter_path: str = "",
    job_context: Optional[Dict[str, Any]] = None,
) -> bool:
    """处理 Easy Apply 当前步骤：填表 → 点击 Next/Submit。

    Args:
        page: Page 对象
        profile: 用户个人信息
        resume_path: 简历文件路径
        cover_letter_path: Cover Letter 路径

    Returns:
        True 如果当前步骤成功完成
    """
    curr, total = get_current_step_info(page)
    logger.info(f"Easy Apply step {curr}/{total}")

    # 填充表单
    try:
        _fill_modal_form(page, profile, resume_path, cover_letter_path)
    except Exception as e:
        logger.warning(f"Form fill error: {e}")

    if getattr(page.context, "_easy_apply_resume_upload_failed", False):
        question = (
            "The configured CV upload failed or was not retained in the live Easy Apply form. "
            f"Attach {Path(resume_path).name} manually; do not use a recent file."
        )
        page.context._easy_apply_human_intervention_required = True
        page.context._easy_apply_human_questions = [question]
        logger.error("Easy Apply resume upload failure guard blocked navigation")
        return False

    if getattr(page.context, '_cover_letter_required', False):
        # A required cover-letter upload is resolvable when the current
        # workflow explicitly permits AI generation.  Generate from the
        # current JD + configured CV/profile, then refill this same step.
        generated_path = _generate_required_cover_letter(
            page, profile, resume_path, job_context or getattr(page.context, "_easy_apply_job_context", {})
        )
        if generated_path:
            cover_letter_path = generated_path
            page.context._easy_apply_cover_letter_path = generated_path
            page.context._cover_letter_required = False
            # Cover-letter generation is a successful recovery milestone;
            # do not let the pre-generation navigation attempts consume the
            # remaining AI-help budget for the rest of this form.
            page.context._easy_apply_ai_help_attempts = 0
            try:
                _fill_modal_form(page, profile, resume_path, generated_path)
            except Exception as exc:
                logger.warning("Cover letter refill error: %s", exc)
        if getattr(page.context, "_easy_apply_resume_upload_failed", False):
            question = (
                "The configured CV upload failed or was not retained in the live Easy Apply form. "
                f"Attach {Path(resume_path).name} manually; do not use a recent file."
            )
            page.context._easy_apply_human_intervention_required = True
            page.context._easy_apply_human_questions = [question]
            logger.error("Easy Apply resume upload failure guard blocked navigation after cover-letter refill")
            return False
        if getattr(page.context, '_cover_letter_required', False):
            logger.info("Easy Apply still requires a cover letter after AI generation; stopping for human-intervention queue")
            return False

    # Never rely on LinkedIn's "recently used" document choice.  Before any
    # Next/Review/Submit click, the live FileList must contain the exact
    # configured CV basename.  If the check cannot prove that, keep the form
    # open for human intervention instead of risking submission of another
    # company's cover letter as the resume.
    if not _verify_easy_apply_resume(page, resume_path):
        question = (
            "The Easy Apply resume safety check could not verify the configured CV "
            f"({Path(resume_path).name}). Do not submit a recent file; inspect the "
            "resume control and attach the configured CV manually."
        )
        page.context._easy_apply_human_intervention_required = True
        page.context._easy_apply_human_questions = [question]
        logger.error("Easy Apply resume safety guard blocked navigation: configured CV is not verified")
        return False

    page.wait_for_timeout(500)

    # 点击推进按钮
    if not click_step_button(page):
        logger.error(f"Could not proceed from step {curr}/{total}")
        logger.info("Requesting AI form help after Easy Apply navigation failure")
        if _request_ai_form_help(page, profile, resume_path, cover_letter_path):
            return True
        return False

    return True


def _fill_modal_form(
    page: Page,
    profile: Dict[str, Any],
    resume_path: str,
    cover_letter_path: str = "",
) -> None:
    """在弹窗容器内填写表单 (修复 BUG-004 & BUG-005)。

    通过 label 文本准确匹配字段语义，避免子串误匹配。
    """
    modal = _get_modal(page)
    search_root = modal if modal else page

    filled = 0

    # --- 处理所有 SELECT 下拉 ---
    selects = search_root.query_selector_all("select")
    for el in selects:
        try:
            label = _get_select_label(el)
            options = [o.inner_text().strip() for o in el.query_selector_all("option") if o.inner_text().strip()]

            if not options:
                continue

            logger.debug(f"SELECT label='{label}' options={options[:5]}...")

            # BUG-007 fix: 同时匹配英文和荷兰语 label
            label_lower = label.lower()

            if any(kw in label_lower for kw in FIELD_LABELS["email"]):
                configured_email = str(profile.get("email", "") or "").strip()
                skip_texts = ["select an option", "selecteer een optie", "选择一个选项", "请选择"]

                selected = False
                # Only select a configured email. Never guess from a user's
                # name or from the first account option in a public build.
                if configured_email:
                    for opt in options:
                        if configured_email.casefold() in opt.casefold():
                            el.select_option(label=opt)
                            logger.info("Selected configured email option")
                            filled += 1
                            selected = True
                            break

                # 第三轮回退
                if not selected:
                    for opt in options:
                        if "@" in opt and opt.lower() not in skip_texts:
                            el.select_option(label=opt)
                            logger.warning(f"Selected email (LAST RESORT): {opt}")
                            filled += 1
                            break

            elif any(kw in label_lower for kw in FIELD_LABELS["phone_country"]) \
                    and not any(kw in label_lower for kw in FIELD_LABELS["phone"]):
                for opt in options:
                    opt_lower = opt.lower()
                    # 排除 Caribbean Netherlands (+599) — 需要的是 Netherlands (+31)
                    if "carib" in opt_lower:
                        continue
                    desired_country = str(profile.get("country", "") or "").casefold()
                    if desired_country and desired_country in opt_lower:
                        el.select_option(label=opt)
                        logger.info(f"Selected phone country: {opt}")
                        filled += 1
                        break

            else:
                # 检查是否是 Yes/No 选择题 → 用筛选 QA 逻辑判断
                yes_no_options = set(o.lower().strip() for o in options if o.lower().strip() in ("yes", "no"))
                if yes_no_options == {"yes", "no"} and "select" in options[0].lower():
                    answer = _analyze_yes_no_question(label)
                    if answer:
                        el.select_option(label=answer)
                        logger.info(f"Selected {answer} for: {label[:50]}")
                        filled += 1
                    else:
                        logger.info(
                            "No deterministic answer for Yes/No select; deferring to AI/user help: %s",
                            label[:100],
                        )
                else:
                    # 检查 profile 匹配
                    matched = False
                    for field, value in profile.items():
                        if field.replace("_", " ") in label.lower():
                            if isinstance(value, str):
                                for opt in options:
                                    if value.lower() in opt.lower():
                                        el.select_option(label=opt)
                                        logger.info(f"Selected {field}={opt}")
                                        filled += 1
                                        matched = True
                                        break
                            break
                    # Prefer a configured country; never assume a country in
                    # the public build.
                    if not matched and str(profile.get("country", "") or "").strip():
                        desired_country = str(profile.get("country", "")).casefold()
                        for opt in options:
                            opt_lower = opt.lower()
                            if "carib" in opt_lower:
                                continue
                            if desired_country in opt_lower:
                                el.select_option(label=opt)
                                logger.info("Selected configured country option: %s", opt[:40])
                                filled += 1
                                break

        except Exception as e:
            logger.debug(f"SELECT fill error: {e}")

    # --- 处理文本输入 ---
    text_inputs = search_root.query_selector_all("input[type='text'], input[type='tel'], input:not([type])")
    for el in text_inputs:
        try:
            itype = el.get_attribute("type") or "text"
            if itype in ("file", "radio", "checkbox", "submit", "hidden", "range"):
                continue

            label = _get_input_label(el)
            logger.debug(f"INPUT label='{label}'")

            label_lower = label.lower()

            if any(kw in label_lower for kw in ("linkedin profile", "linkedin url", "linkedin")):
                linkedin_url = str(profile.get("linkedin_url", "") or "").strip()
                if linkedin_url:
                    el.click()
                    el.fill("")
                    el.type(linkedin_url, delay=35)
                    logger.info("Filled LinkedIn profile: %s", linkedin_url)
                    filled += 1

            elif any(kw in label_lower for kw in FIELD_LABELS["phone"]):
                phone = profile.get("phone", "")
                if phone:
                    el.click()
                    el.fill("")
                    el.type(phone, delay=50)
                    logger.info(f"Filled phone: {phone}")
                    filled += 1

            elif any(kw in label_lower for kw in FIELD_LABELS["city"]):
                city = profile.get("city", "")
                if city:
                    el.click()
                    el.fill("")
                    el.type(city, delay=80)  # 逐字输入触发 autocomplete
                    page.wait_for_timeout(1000)  # 等下拉出现
                    page.keyboard.press("ArrowDown")
                    page.wait_for_timeout(300)
                    page.keyboard.press("Enter")
                    page.wait_for_timeout(300)
                    logger.info(f"Filled city (autocomplete): {city}")
                    filled += 1

            elif any(kw in label_lower for kw in FIELD_LABELS["first_name"]):
                el.fill(profile.get("first_name", ""))
                filled += 1

            elif any(kw in label_lower for kw in FIELD_LABELS["last_name"]):
                el.fill(profile.get("last_name", ""))
                filled += 1

            else:
                # BUG-008 fix: 处理岗位筛选问题 (screening questions)
                filled += _handle_screening_question(el, label, profile)

        except Exception as e:
            logger.debug(f"INPUT fill error: {e}")

    # --- 处理文本区域 ---
    textareas = search_root.query_selector_all("textarea")
    for el in textareas:
        try:
            label = _get_input_label(el)
            logger.debug(f"TEXTAREA label='{label}'")
            # 检测 Cover Letter 字段 → 创建专用 flag (非阻塞, Agent 生成)
            if _is_cover_letter_field(el, label):
                page.context._cover_letter_field_seen = True
                page.context._cover_letter_field_label = label or "cover letter"
                logger.info(f"📝 Cover Letter textarea detected: '{label[:60]}'")
                if _is_required_control(el, label):
                    page.context._cover_letter_required = True
                    logger.warning("Easy Apply cover letter is explicitly required; queueing for human intervention")
                else:
                    logger.info("Easy Apply cover letter is optional; skipping this field")
            else:
                filled += _handle_screening_question(el, label, profile)
        except Exception as e:
            logger.debug(f"TEXTAREA fill error: {e}")

    # --- 上传 CV / supporting documents ---
    # A page can expose hidden file inputs or a button that opens a chooser.
    # Classify each control before selecting a document so diploma and grade
    # list requests use the files configured in profile rather than the CV.
    document_paths = _document_paths(profile, resume_path)
    uploaded_keys = getattr(page.context, "_easy_apply_uploaded_documents", set())
    if not isinstance(uploaded_keys, set):
        uploaded_keys = set()
    setattr(page.context, "_easy_apply_uploaded_documents", uploaded_keys)

    def upload_document(
        kind: str,
        setter,
        preferred: Optional[ElementHandle] = None,
        path_override: str = "",
    ) -> bool:
        path = path_override or document_paths.get(kind, "")
        if not path:
            logger.error("Required Easy Apply document is not configured: %s", kind)
            if kind == "resume":
                page.context._easy_apply_resume_upload_failed = True
            return False
        if not Path(path).exists():
            logger.error("Required Easy Apply document is missing: kind=%s path=%s", kind, path)
            if kind == "resume":
                page.context._easy_apply_resume_upload_failed = True
            return False
        control_key = (kind, path)
        if control_key in uploaded_keys:
            # A previous step may have replaced the input node.  Re-check the
            # live FileList instead of trusting an in-memory key.
            if _verify_document_upload(search_root, kind, path, preferred):
                logger.debug("Easy Apply document already uploaded: kind=%s", kind)
                return True
            uploaded_keys.discard(control_key)
        try:
            setter(path)
            page.wait_for_timeout(250)
            if not _verify_document_upload_with_retry(
                search_root, kind, path, preferred, page=page,
            ):
                logger.error(
                    "Easy Apply document was set but did not retain the configured file: kind=%s path=%s",
                    kind, path,
                )
                if kind == "resume":
                    page.context._easy_apply_resume_upload_failed = True
                return False
            if kind == "resume":
                page.context._easy_apply_resume_verified_path = str(Path(path).resolve())
                page.context._easy_apply_resume_upload_failed = False
            uploaded_keys.add(control_key)
            logger.info("✅ Easy Apply document uploaded: kind=%s path=%s", kind, path)
            page.wait_for_timeout(1200)
            return True
        except Exception as exc:
            logger.warning("Easy Apply document upload failed: kind=%s path=%s error=%s", kind, path, exc)
            if kind == "resume":
                page.context._easy_apply_resume_upload_failed = True
            return False

    file_inputs = search_root.query_selector_all("input[type='file']")
    input_uploaded = False
    for el in file_inputs:
        try:
            el_id = (el.get_attribute("id") or "").lower()
            el_name = (el.get_attribute("name") or "").lower()
            el_label = _get_input_label(el).lower()
            semantic = " ".join((el_id, el_name, el_label, el.get_attribute("aria-label") or ""))
            kind = _document_kind(semantic)
            if kind == "cover_letter" or _is_cover_letter_file_input(el_id, el_name, el_label):
                page.context._cover_letter_field_seen = True
                page.context._cover_letter_field_label = el_label or el_name or el_id or "cover letter"
                if cover_letter_path and Path(cover_letter_path).is_file():
                    cover_key = ("cover_letter", cover_letter_path)
                    if cover_key not in uploaded_keys:
                        if upload_document(
                            "cover_letter",
                            lambda path, element=el: element.set_input_files(path),
                            preferred=el,
                            path_override=cover_letter_path,
                        ):
                            uploaded_keys.add(cover_key)
                            page.context._cover_letter_uploaded = True
                            page.context._cover_letter_required = False
                            filled += 1
                            logger.info("✅ Easy Apply cover letter uploaded: %s", cover_letter_path)
                        else:
                            logger.error("Easy Apply cover letter was not retained; refusing to proceed")
                        page.wait_for_timeout(1200)
                elif _is_required_control(el, el_label):
                    page.context._cover_letter_required = True
                    logger.warning("Easy Apply cover letter upload is explicitly required; queueing for human intervention")
                else:
                    logger.info("Easy Apply cover letter upload is optional; skipping this control")
                continue
            if upload_document(
                kind,
                lambda path, element=el: element.set_input_files(path),
                preferred=el,
            ):
                filled += 1
                input_uploaded = True
        except Exception as exc:
            logger.debug("Easy Apply file input inspection failed: %s", exc)

    # Button-based upload controls.  The surrounding text is included because
    # many LinkedIn controls have only a generic ``Upload`` button label.
    for button in search_root.query_selector_all("button, [role='button']"):
        try:
            if not button.is_visible() or button.is_disabled():
                continue
            text = _upload_control_semantic(button)
            if not any(k in text.lower() for k in ("upload", "attach", "上传", "uploaden", "bijlage")):
                continue
            kind, semantic = _document_kind_for_control(button)
            if kind is None:
                logger.warning(
                    "Easy Apply upload control is ambiguous (resume and cover letter in one wrapper); refusing it: %s",
                    semantic[:180],
                )
                continue
            if kind == "cover_letter":
                page.context._cover_letter_field_seen = True
                page.context._cover_letter_field_label = text or "cover letter"
                if cover_letter_path and Path(cover_letter_path).is_file():
                    cover_key = ("cover_letter", cover_letter_path)
                    if cover_key not in uploaded_keys:
                        try:
                            with page.expect_file_chooser(timeout=5000) as fc_info:
                                button.scroll_into_view_if_needed()
                                button.click(force=True)
                            fc_info.value.set_files(cover_letter_path)
                            page.wait_for_timeout(250)
                            if not _verify_document_upload_with_retry(
                                search_root, "cover_letter", cover_letter_path, page=page,
                            ):
                                raise RuntimeError("cover-letter FileList did not retain the configured basename")
                            uploaded_keys.add(cover_key)
                            page.context._cover_letter_uploaded = True
                            page.context._cover_letter_required = False
                            filled += 1
                            logger.info("✅ Easy Apply cover letter uploaded via chooser: %s", cover_letter_path)
                            page.wait_for_timeout(1200)
                        except Exception as exc:
                            logger.warning("Easy Apply cover letter chooser upload failed: %s", exc)
                elif _is_required_control(button, text):
                    page.context._cover_letter_required = True
                    logger.warning("Easy Apply cover letter chooser is explicitly required; queueing for human intervention")
                else:
                    logger.info("Easy Apply cover letter chooser is optional; skipping this control")
                continue
            path = document_paths.get(kind, "")
            if not path or not Path(path).exists() or (kind, path) in uploaded_keys:
                continue
            try:
                with page.expect_file_chooser(timeout=5000) as fc_info:
                    button.scroll_into_view_if_needed()
                    button.click(force=True)
                chooser = fc_info.value
                if upload_document(kind, chooser.set_files):
                    filled += 1
                    input_uploaded = True
            except Exception as exc:
                logger.debug("Easy Apply file chooser failed for %s: %s", kind, exc)
        except Exception as exc:
            logger.debug("Easy Apply upload button inspection failed: %s", exc)

    if not file_inputs and not input_uploaded:
        logger.debug("No document upload control present in this Easy Apply step")

    # --- 处理 Radio / Checkbox 筛选问题 ---
    # 这些是 "Are you authorized... Yes/No" 类型的问题
    filled += _handle_radio_groups(search_root, page)

    logger.info(f"Fields filled in step: {filled}")


def _handle_radio_groups(search_root, page) -> int:
    """处理 Radio / Checkbox 筛选问题。

    识别 "Are you legally authorized to work in X? Yes / No" 类型的问题，
    根据预设的标准回答自动选择。

    Returns:
        成功处理的字段数
    """
    # 标准回答映射 (问题关键词 → 回答)
    ANSWER_MAP = [
        # NewRocket-style screening questions.  These are explicit profile
        # facts/preferences supplied by the user, so handle them before the
        # generic AI-help path and keep each question bound to its own group.
        (["5 years or more of python", "python experience"],
         "Yes", "Python 5+ years → Yes"),

        (["anthropic experience and certifications", "anthropic experience"],
         "No", "Anthropic experience/certifications → No"),

        (["enterprise consulting/services", "enterprise consulting", "services capacity"],
         "No", "Enterprise consulting/services capacity → No"),

        (["native/bilingual dutch", "dutch speaker", "native dutch", "nederlandse moedertaal"],
         "No", "Dutch speaker → No"),

        # (question_keywords, answer_text, description)
        (["legally authorized", "authorized to work", "right to work",
          "eligible to work", "work authorization", "legal right",
          "wettelijk bevoegd", "werkvergunning", "合法工作", "工作授权"],
         "Yes", "Work authorization → Yes (current orientation-year permit)"),

        (["require sponsorship", "sponsorship required", "need sponsorship",
          "visa sponsorship", "employment visa", "visa status",
          "sponsoring nodig", "visum sponsoring", "签证赞助", "需要赞助"],
         "Yes", "Sponsorship required → Yes"),

        # User policy: distance/proximity/commute questions are always Yes.
        (["within", "distance", "minutes drive", "minutes away", "close to",
          "near", "nearby", "commuting distance", "commute to",
          "maximaal", "afstand", "dichtbij", "op maximaal"],
         "Yes", "Distance/proximity → Yes"),

        (["driver license", "driver's license", "driving license", "rijbewijs",
          "valid driver's licence", "valid driving licence"],
         "No", "Driver's license → No"),

        (["willing to relocate", "relocate", "relocation",
          "bereid te verhuizen", "愿意搬迁"],
         "Yes", "Willing to relocate → Yes"),

        # The user accepts hybrid/onsite-remote work.  Keep this known
        # preference deterministic so a custom LinkedIn radio group does not
        # fall through to the user-answer workflow.
        (["hybrid setting", "hybrid work", "hybrid working", "hybride werken",
          "remote and office", "onsite and remote", "work partly from home",
          "partly remote", "hybrid"],
         "Yes", "Hybrid working → Yes"),

        (["currently employed", "currently working",
          "momenteel in dienst", "目前在职"],
         "No", "Currently employed → No (student)"),
    ]

    filled = 0
    processed_groups = set()  # 避免重复处理同一组

    # LinkedIn 使用 <div role=\"radio\"> 而非 <input type=\"radio\">
    # 获取所有可能的 radio/checkbox 元素
    native_radios = search_root.query_selector_all("input[type='radio']")
    native_checkboxes = search_root.query_selector_all("input[type='checkbox']")
    custom_checkboxes = search_root.query_selector_all("[role='checkbox']")
    custom_radios = search_root.query_selector_all("[role='radio']")
    choice_controls = _get_choice_controls(search_root)

    # 组合所有可选项：优先可见的 custom radio（LinkedIn），回退到原生 input
    all_radios = choice_controls or (list(custom_radios) if custom_radios else list(native_radios))
    all_checkboxes = list(native_checkboxes) + list(custom_checkboxes)

    # Some Workable/LinkedIn forms expose the consent checkbox with an empty
    # label and put the question only in a surrounding text node.  When the
    # modal explicitly contains the user's consent wording, use the sole
    # visible checkbox as the deterministic consent control.  Do not apply
    # this fallback to forms with multiple unrelated checkboxes.
    try:
        modal_text = (search_root.inner_text() or "").casefold()
        if any(token in modal_text for token in ("personal data consent", "consent to the processing", "privacy notice")):
            visible_checkboxes = [cb for cb in all_checkboxes if cb.is_visible()]
            if len(visible_checkboxes) == 1:
                cb = visible_checkboxes[0]
                checked = False
                try:
                    checked = cb.is_checked()
                except Exception:
                    checked = (cb.get_attribute("aria-checked") or "").casefold() == "true"
                if not checked:
                    label = _find_label_for_radio(cb)
                    if label and label.is_visible():
                        label.scroll_into_view_if_needed()
                        label.click(force=True)
                    else:
                        cb.scroll_into_view_if_needed()
                        cb.click(force=True)
                    page.wait_for_timeout(300)
                    filled += 1
                    logger.info("Selected privacy/data-processing consent → Yes (single-checkbox fallback)")
    except Exception as e:
        logger.debug(f"Consent checkbox fallback failed: {e}")

    for el in all_radios + all_checkboxes:
        try:
            if not el.is_visible():
                continue

            el_type = el.get_attribute("type") or ""
            el_name = el.get_attribute("name") or el.get_attribute("id") or ""
            el_value = (el.get_attribute("value") or "").strip().lower()

            # 获取这个问题所在区域的完整文本（fieldset 或父容器）
            context_text = _get_radio_context(el)
            context_lower = context_text.lower()

            # LinkedIn's custom role=radio controls often have no name/id.
            # Use their question context as the group key; otherwise the
            # first unrelated radio would suppress every later group.
            group_key = f"{el_type}_{el_name or context_lower[:240]}"
            if group_key in processed_groups:
                continue

            # 匹配已知问题
            matched_answer = None
            for keywords, answer, desc in ANSWER_MAP:
                if any(kw in context_lower for kw in keywords):
                    matched_answer = answer
                    logger.info(f"Screening radio: {desc}")
                    break

            # JOIN/LinkedIn also renders non-Yes/No questions as custom
            # role=radio controls.  These questions have no useful value or
            # label[for] on the wrapper, so match the question text and use
            # the visible option text below.  The defaults here are grounded
            # in the applicant profile: business-fluent English and consent
            # to the employer's terms/privacy policy are required to submit.
            question_text = (
                el.get_attribute("aria-label")
                or context_text
                or ""
            ).lower()
            if matched_answer is None and any(kw in question_text for kw in [
                "english proficiency", "level of english", "niveau in het engels",
                "niveau engels", "english level",
            ]):
                matched_answer = "Volledige professionele vaardigheid"
                logger.info("Screening radio: English proficiency → full professional proficiency")
            elif matched_answer is None and any(kw in question_text for kw in [
                "terms", "privacy policy", "privacybeleid", "algemene voorwaarden",
                "gegevensprivacybeleid", "voorwaarden",
            ]):
                matched_answer = "Yes"
                logger.info("Screening radio: terms/privacy consent → Yes")

            if not matched_answer:
                continue

            processed_groups.add(group_key)

            # Privacy/data-processing consent is a checkbox, not a radio
            # option.  The user's standing preference explicitly authorizes
            # these job-application notices; click the checkbox itself (or
            # its associated label) instead of trying to match a Yes/No label.
            if el in all_checkboxes:
                try:
                    if not el.is_checked():
                        label = _find_label_for_radio(el)
                        if label and label.is_visible():
                            label.scroll_into_view_if_needed()
                            label.click(force=True)
                        else:
                            el.scroll_into_view_if_needed()
                            el.click(force=True)
                        page.wait_for_timeout(300)
                        filled += 1
                        logger.info("Selected privacy/data-processing consent → Yes")
                except Exception as e:
                    logger.debug(f"Consent checkbox select error: {e}")
                continue

            # 找到同组的所有 radio
            if el_type == "radio" and el_name:
                group_radios = search_root.query_selector_all(f'input[name="{el_name}"]')
            elif not el_type and custom_radios:
                # Group custom radios by the question's aria-label.  The
                # terms/consent control has one option and naturally remains
                # a one-item group.
                aria_question = el.get_attribute("aria-label") or ""
                aria_ref = el.get_attribute("aria-labelledby") or ""
                group_radios = []
                for candidate in custom_radios:
                    if (aria_question and candidate.get_attribute("aria-label") == aria_question):
                        group_radios.append(candidate)
                    elif (aria_ref and candidate.get_attribute("aria-labelledby") == aria_ref):
                        group_radios.append(candidate)
                if not group_radios:
                    group_radios = [el]
            elif not el_type and choice_controls:
                # Button/option based Yes/No renderers have no shared name.
                # Bind by their bounded ancestor context and keep the current
                # option as the safe fallback when no explicit group exists.
                current_context = _get_radio_context(el).casefold()
                group_radios = [candidate for candidate in choice_controls
                                if _question_matches_context(current_context, _get_radio_context(candidate))]
                if not group_radios:
                    group_radios = [el]
            else:
                group_radios = [el]

            # 点击匹配的选项
            for r in group_radios:
                try:
                    r_value = (r.get_attribute("value") or "").strip().lower()
                    r_label = _get_radio_label(r).lower()

                    # 匹配回答文本
                    answer_lower = matched_answer.lower()
                    answer_aliases = {answer_lower}
                    if answer_lower in {"yes", "ja"}:
                        answer_aliases.update({"yes", "ja", "y", "true"})
                    elif answer_lower in {"no", "nee"}:
                        answer_aliases.update({"no", "nee", "n", "false"})
                    if (
                        any(alias in r_value or alias in r_label for alias in answer_aliases)
                        or r_value in answer_aliases
                        or r_label in answer_aliases
                    ):
                        checked = False
                        try:
                            checked = bool(r.is_checked())
                        except Exception:
                            checked = (r.get_attribute("aria-checked") or "").lower() == "true"
                        if not checked:
                            # 直接点击 radio 元素
                            r.click(force=True)
                            page.wait_for_timeout(300)
                            filled += 1
                            logger.info(f"  Selected: '{matched_answer}'")
                            break
                except Exception as e:
                    logger.debug(f"Radio select error: {e}")

        except Exception as e:
            logger.debug(f"Radio group error: {e}")

    return filled


def _get_element_context(el) -> str:
    """获取元素所在区域的完整文本（问题上下文）。"""
    try:
        # 向上查找 fieldset / div / section
        parent = el.evaluate_handle("""
            el => {
                let p = el.closest('fieldset');
                if (p) {
                    // LinkedIn's custom radio groups often put the actual
                    // question in the wrapper immediately outside the
                    // fieldset.  Returning only fieldset.textContent gives
                    // us just "Yes No", so AI/user answers can never match
                    // the question and the form appears stuck.  Prefer that
                    // immediate field wrapper when it adds meaningful text,
                    // while retaining the fieldset-only fallback for normal
                    // forms.
                    const fieldText = (p.textContent || '').trim();
                    const owner = p.parentElement;
                    const ownerText = (owner?.textContent || '').trim();
                    if (ownerText.length > fieldText.length + 10) return ownerText;
                    return fieldText;
                }
                p = el.closest('[role="group"]');
                if (p) return p.textContent;
                // 向上3层
                p = el.parentElement?.parentElement?.parentElement;
                if (p) return p.textContent;
                return el.parentElement?.textContent || '';
            }
        """)
        try:
            return parent.json_value() or ""
        except Exception:
            return ""
    except Exception:
        return ""


def _get_radio_context(el) -> str:
    """Return bounded question context for native/custom radio controls.

    LinkedIn has used layouts where the fieldset contains only the option
    text (``Yes``/``No``), while the actual question is in a wrapper several
    ancestors above.  Include a few bounded ancestor snippets so known
    answers can bind to the correct group without using the whole modal.
    """
    try:
        extra = el.evaluate("""
            (el) => {
                const out = [];
                let n = el;
                for (let i = 0; n && i < 6; i++, n = n.parentElement) {
                    const t = (n.innerText || n.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (t && !out.includes(t)) out.push(t.slice(0, 900));
                }
                return out.join('\\n');
            }
        """) or ""
        snippets = [line.strip() for line in str(extra).splitlines() if line.strip()]
        # Prefer the nearest compact ancestor that contains the actual
        # question marker.  The old fieldset/owner helper can return the whole
        # modal, which makes every Yes/No group look like every other group.
        for snippet in snippets:
            if 12 <= len(snippet) <= 650 and ("?" in snippet or "*" in snippet):
                return snippet
        for snippet in snippets:
            if 12 <= len(snippet) <= 650:
                return snippet
    except Exception:
        pass
    return _get_element_context(el)


def _get_choice_controls(search_root):
    """Return visible native/custom radio-like controls.

    LinkedIn's current Easy Apply renderer may expose the visible Yes/No
    choices as ``button``/``role=button`` elements while the native radio
    input is hidden.  Treat only exact short choice labels as radio-like; do
    not include navigation buttons such as Review or Back.
    """
    controls = []
    seen = set()
    selectors = (
        "[role='radio']", "input[type='radio']", "button", "[role='button']",
        "[role='option']", "label",
    )
    choice_labels = {"yes", "no", "ja", "nee", "y", "n", "true", "false"}
    for selector in selectors:
        for el in search_root.query_selector_all(selector):
            try:
                if not el.is_visible():
                    continue
                # Playwright handles are not hash-stable across wrappers;
                # use a compact DOM identity instead.
                identity = el.get_attribute("id") or el.get_attribute("data-testid") or f"{selector}:{len(controls)}"
                if identity in seen:
                    continue
                text = " ".join(filter(None, [
                    (el.inner_text() or "").strip(),
                    (el.get_attribute("aria-label") or "").strip(),
                    (el.get_attribute("value") or "").strip(),
                ])).casefold()
                if selector in ("button", "[role='button']", "[role='option']", "label", "div", "span", "p") and text not in choice_labels:
                    continue
                seen.add(identity)
                controls.append(el)
            except Exception:
                continue

    # Only inspect plain divs when this step actually contains Yes/No text.
    # This keeps document-upload and contact steps fast.  In LinkedIn's
    # current SDUI the clickable option is the outer ``b6bikq`` div; prefer
    # that wrapper over nested text/presentation nodes.
    try:
        modal_text = (search_root.inner_text() or "").casefold()
    except Exception:
        modal_text = ""
    if "yes" in modal_text and "no" in modal_text:
        divs = []
        for el in search_root.query_selector_all("div"):
            try:
                if not el.is_visible():
                    continue
                text = (el.evaluate("el => (el.innerText || el.textContent || '')") or "").strip()
                if text.casefold() not in choice_labels:
                    continue
                classes = (el.get_attribute("class") or "").casefold()
                if "b6bikq" not in classes and not any(token in classes for token in ("radio", "choice", "option")):
                    continue
                identity = el.get_attribute("id") or el.get_attribute("data-testid") or f"div:{len(controls) + len(divs)}"
                if identity in seen:
                    continue
                seen.add(identity)
                divs.append(el)
            except Exception:
                continue
        controls.extend(divs)
    return controls


def _get_radio_label(el) -> str:
    """获取 radio 的 label 文本。"""
    try:
        el_id = el.get_attribute("id") or ""
        if el_id:
            label = el.evaluate_handle(f'() => document.querySelector("label[for=\'{el_id}\']")')
            try:
                text = label.inner_text().strip()
                if text:
                    return text
            except Exception:
                pass
        # 同级或父级的 <label>
        parent_label = el.evaluate_handle("el => el.closest('label')")
        try:
            text = parent_label.inner_text().strip()
            if text:
                return text
        except Exception:
            pass
        # LinkedIn's custom role=radio wrapper has no value/for label on the
        # wrapper itself; its visible option text is the wrapper's own text.
        try:
            text = (el.inner_text() or "").strip()
            if text:
                return text
        except Exception:
            pass
        value = el.get_attribute("value") or ""
        if value:
            return value

        # LinkedIn's SDUI Easy Apply renderer keeps the native radio input
        # visually empty and puts the visible option text in a sibling node:
        #   <input type="radio" ...><label></label><p>Yes</p>
        # There is no useful value, label[for], or input innerText in that
        # shape.  Walk only the small option container and prefer an exact
        # choice line so the question text from the surrounding fieldset is
        # never mistaken for the option label.
        option_text = el.evaluate("""
            (node) => {
                const normalize = (value) => String(value || '')
                    .replace(/\\s+/g, ' ').trim();
                const exactChoices = new Set([
                    'yes', 'no', 'ja', 'nee', 'y', 'n', 'true', 'false'
                ]);
                let current = node;
                for (let depth = 0; current && depth < 5; depth++, current = current.parentElement) {
                    const raw = String(current.innerText || current.textContent || '');
                    const lines = raw.split(/\\n+/).map(normalize).filter(Boolean);
                    const exact = lines.find((line) => exactChoices.has(line.toLowerCase()));
                    if (exact) return exact;
                    if (lines.length <= 2 && raw.indexOf('?') < 0 && raw.indexOf('*') < 0) {
                        const compact = normalize(raw);
                        if (compact && compact.length <= 100) return compact;
                    }
                }
                return '';
            }
        """) or ""
        return str(option_text).strip()
    except Exception:
        return ""


def _find_label_for_radio(el):
    """找到 radio 对应的可点击 label 元素。"""
    try:
        el_id = el.get_attribute("id") or ""
        if el_id:
            label = el.evaluate_handle(f'() => document.querySelector("label[for=\'{el_id}\']")')
            if label:
                return label.as_element()
    except Exception:
        pass
    return None


def _get_select_label(el: ElementHandle) -> str:
    """获取 SELECT 元素的语义 label。"""
    try:
        el_id = el.get_attribute("id") or ""
        if el_id:
            label_el = el.evaluate_handle(
                f'() => document.querySelector(\'label[for="{el_id}"]\')'
            )
            try:
                text = label_el.inner_text()
                if text:
                    return text.strip()
            except Exception:
                pass

        # 检查 aria-label
        aria = el.get_attribute("aria-label") or ""
        if aria:
            return aria

        # 检查 name
        name = el.get_attribute("name") or ""
        if name:
            return name

        # 向上查找 label
        parent_label = el.evaluate_handle("el => el.closest('label')")
        try:
            text = parent_label.inner_text()
            if text:
                return text.strip()
        except Exception:
            pass

    except Exception:
        pass
    return ""


def _click_radio_control(page, el, *, description: str = "") -> bool:
    """Click a radio even when LinkedIn keeps its input outside the viewport.

    Easy Apply modals often scroll an inner container rather than the document.
    Playwright's normal click can therefore raise ``outside of the viewport``
    even though the option is visible to the user.  Try the normal path first,
    then use the element's native DOM click (which still dispatches the page's
    change/input handlers), and finally ``check`` for native radio inputs.
    """
    errors: list[str] = []
    try:
        el.scroll_into_view_if_needed(timeout=2500)
        el.click(force=True)
        return True
    except Exception as exc:
        errors.append(str(exc).splitlines()[0][:160])

    try:
        el.evaluate(
            """el => {
                const control = el.closest('label,[role="radio"],button') || el;
                control.scrollIntoView({block:'center', inline:'nearest'});
                control.click();
                return true;
            }"""
        )
        return True
    except Exception as exc:
        errors.append(str(exc).splitlines()[0][:160])

    if (el.get_attribute("type") or "").casefold() == "radio":
        try:
            el.check(force=True)
            return True
        except Exception as exc:
            errors.append(str(exc).splitlines()[0][:160])

    logger.warning(
        "AI radio click failed%s: %s",
        f" ({description[:100]})" if description else "",
        " | ".join(errors),
    )
    return False


def _get_input_label(el: ElementHandle) -> str:
    """获取 INPUT 元素的语义 label。"""
    try:
        el_id = el.get_attribute("id") or ""
        if el_id:
            label_el = el.evaluate_handle(
                f'() => document.querySelector(\'label[for="{el_id}"]\')'
            )
            try:
                text = label_el.inner_text()
                if text:
                    return text.strip()
            except Exception:
                pass

        aria = el.get_attribute("aria-label") or ""
        if aria:
            return aria

        placeholder = el.get_attribute("placeholder") or ""
        if placeholder:
            return placeholder

        name = el.get_attribute("name") or ""
        if name:
            return name

        parent_label = el.evaluate_handle("el => el.closest('label')")
        try:
            text = parent_label.inner_text()
            if text:
                return text.strip()
        except Exception:
            pass

    except Exception:
        pass
    return ""


# 技能关键词 → 年限映射（基于用户简历）
# 从简历中推断的技能经验年限
SKILL_EXPERIENCE_YEARS = {
    # Core AI/ML (用户的核心技能 — MSc + 实际项目)
    "artificial intelligence": 4,
    "machine learning": 4,
    "deep learning": 3,
    "large language models": 2,
    "llm": 2,
    "natural language processing": 2,
    "nlp": 2,
    "generative ai": 2,
    "rag": 1.5,
    "knowledge graph": 2,
    "computer vision": 2,
    "prompt engineering": 2,
    "agent": 2,
    "multi-agent": 1.5,
    "reinforcement learning": 1,
    "evolutionary algorithm": 1,

    # Frameworks & Tools
    "python": 5,
    "pytorch": 3,
    "langchain": 2,
    "langgraph": 1.5,
    "git": 5,
    "linux": 4,
    "api": 3,
    "saas": 1,
    "yolo": 1,
    "u-net": 1,
    "matlab": 2,

    # 用户不太熟悉的技能
    "c++": 0,
    "c#": 0,
    "java": 0,
    "javascript": 0,
    "typescript": 0,
    "react": 0,
    "angular": 0,
    "docker": 0,
    "kubernetes": 0,
    "aws": 0,
    "azure": 0,
    "sql": 1,
    "r": 0,
    "scala": 0,
    "go": 0,
    "rust": 0,
}


def _analyze_yes_no_question(label: str) -> Optional[str]:
    """分析 Yes/No 题目的内容，返回 'Yes' 或 'No'。

    基于 docs/screening_qa.md 的标准回答和用户实际情况:
    - 工作授权/合法工作 → Yes (当前 orientation-year/search-year 许可)
    - 签证赞助/需要赞助 → Yes
    - 住在荷兰 → Yes
    - 荷兰语 → No
    - 未知问题 → None（交给 AI/用户，不猜答案）
    """
    label_lower = label.lower()

    # 需要回答 No 的问题
    no_patterns = [
        "dutch", "speak dutch", "nederlands", "currently employed",
        "ever worked", "worked at", "worked for", "previously worked",
    ]
    for pat in no_patterns:
        if pat in label_lower:
            return "No"

    # 需要回答 Yes 的问题
    yes_patterns = [
        "legally authorized", "authorized to work", "right to work",
        "eligible to work", "work authorization", "legal right",
        "sponsor", "visa", "work visa", "hsm",
        "live in", "based in", "relocate", "willing to",
    ]
    for pat in yes_patterns:
        if pat in label_lower:
            return "Yes"

    # Unknown questions must not be guessed. Let the normal AI/user-answer
    # path handle them with truthful context.
    return None


def _handle_screening_question(el, label: str, profile: dict) -> int:
    """处理 LinkedIn 筛选问题 (BUG-008 fix)。

    识别常见问题类型并自动作答：
    1. "How many years of work experience do you have with X?" → 查技能表
    2. "Are you authorized to work in X?" → 查 profile
    3. 其他自行判断

    Returns:
        1 如果填了一个字段，0 如果未处理
    """
    import re
    label_lower = label.lower()

    # ── Pattern 1: "How many years...with X?" ──
    years_match = re.search(
        r'how many years.*?(?:with|in|of|using)\s+(.+?)(?:\?|$|\))',
        label_lower
    )
    if years_match:
        tech = years_match.group(1).strip().rstrip('?').rstrip(')')
        # 在技能表中查找
        years = 0
        for skill, y in SKILL_EXPERIENCE_YEARS.items():
            if skill in tech or tech in skill:
                years = y
                break

        if years > 0:
            el.fill(str(int(years)) if years == int(years) else str(years))
            logger.info(f"Screening Q answered: '{label[:60]}' → {years} years")
            return 1
        elif years == 0:
            # 技能不在我们的表中 → 填 0（诚实）
            # 但如果标签暗示可选，就不填
            el.fill("0")
            logger.info(f"Screening Q answered (no experience): '{label[:60]}' → 0")
            return 1

    # ── Pattern 2: "How many years of ... experience?" (generic) ──
    years_match2 = re.search(
        r'how many years.*?experience',
        label_lower
    )
    if years_match2:
        # 通用经验年限问题 → 根据标签中的关键词推断
        for skill, y in SKILL_EXPERIENCE_YEARS.items():
            if skill in label_lower:
                el.fill(str(int(y)) if y == int(y) else str(y))
                logger.info(f"Screening Q (generic): '{label[:60]}' → {y}")
                return 1
        # 无法匹配 → 填 1 作为默认（表示有一些经验）
        el.fill("1")
        logger.info(f"Screening Q (default): '{label[:60]}' → 1 (default)")
        return 1

    # ── Pattern 3: Work authorization ──
    if any(kw in label_lower for kw in ["authorized", "eligible", "right to work", "work authorization",
                                          "geautoriseerd", "werkvergunning"]):
        el.fill("Yes")
        logger.info(f"Screening Q: work authorization → Yes")
        return 1

    # ── Pattern 4: Sponsorship ──
    if any(kw in label_lower for kw in ["sponsor", "visa", "werkvisum"]):
        el.fill("Yes")
        logger.info(f"Screening Q: sponsorship → Yes")
        return 1

    # Distance/proximity questions use the user's standing Yes answer.
    if any(kw in label_lower for kw in ["distance", "within", "minutes drive", "minutes away",
                                         "close to", "near", "nearby", "commute", "afstand",
                                         "dichtbij", "maximaal"]):
        el.fill("Yes")
        logger.info("Screening Q: distance/proximity → Yes")
        return 1

    # ── Pattern 5: Start date / availability ──
    if any(kw in label_lower for kw in [
        "start date", "earliest start", "available to start", "startdatum",
        "wanneer zou je", "wanneer kun je", "beginnen", "beginnen",
    ]):
        start_date = str(profile.get("earliest_start_date", "") or "").strip()
        if not start_date:
            logger.info("Screening Q: earliest start date is not configured; deferring")
            return 0
        # LinkedIn's date-picker input is locale-formatted even though its
        # DOM value initially resembles ISO.  Filling ISO leaves the control
        # visibly populated but aria-invalid=true; emit the browser's
        # month/day/year form and blur it so client-side validation commits.
        iso = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", start_date.strip())
        if iso:
            start_date = f"{int(iso.group(2)):02d}/{int(iso.group(3)):02d}/{iso.group(1)}"
        el.fill(start_date)
        try:
            el.press("Tab")
        except Exception:
            pass
        logger.info("Screening Q: earliest start date → %s", start_date)
        return 1

    # ── Pattern 6: Salary expectation ──
    if any(kw in label_lower for kw in ["salary", "compensation", "salaris", "vergoeding"]):
        import re
        configured = str(profile.get("salary_expectation", "") or "")
        match = re.search(r"\d[\d,\. ]*", configured)
        salary = re.sub(r"[^0-9]", "", match.group(0)) if match else "50000"
        el.fill(salary or "50000")
        logger.info("Screening Q: salary → %s", salary or "50000")
        return 1

    # ── Pattern 7: Education level ──
    if any(kw in label_lower for kw in ["education", "degree", "opleiding", "diploma"]):
        education = str(profile.get("highest_education", "") or "").strip()
        if not education:
            logger.info("Screening Q: education is not configured; deferring")
            return 0
        el.fill(education)
        logger.info("Screening Q: education configured")
        return 1

    return 0


def _handle_easy_apply_flow_impl(
    page: Page,
    profile: Dict[str, Any],
    resume_path: str,
    cover_letter_path: str = "",
    max_steps: int = 10,
    job_context: Optional[Dict[str, Any]] = None,
    already_open: bool = False,
) -> bool:
    """处理完整 Easy Apply 多步表单流程。

    循环处理每一步直至提交成功。

    Args:
        page: Page 对象
        profile: 用户个人信息
        resume_path: 简历文件路径
        cover_letter_path: Cover Letter 路径
        max_steps: 最大步数

    Returns:
        True 如果投递成功
    """
    page.context._cover_letter_required = False
    page.context._cover_letter_uploaded = False
    page.context._cover_letter_field_label = ""
    page.context._easy_apply_uploaded_documents = set()
    page.context._easy_apply_resume_verified_path = ""
    page.context._easy_apply_resume_upload_failed = False
    page.context._easy_apply_ai_help_attempts = 0
    page.context._easy_apply_human_intervention_required = False
    page.context._easy_apply_human_questions = []
    page.context._easy_apply_submit_clicked = False
    page.context._easy_apply_submission_confirmed = False
    # Keep the live profile/path context available if the user answers an
    # unknown question while this same modal is waiting.
    page.context._easy_apply_profile = profile
    page.context._easy_apply_resume_path = resume_path
    page.context._easy_apply_cover_letter_path = cover_letter_path
    page.context._easy_apply_job_context = job_context or {}
    # AI help can return the same click/fill plan more than once.  Keep a
    # small amount of per-form state so an unchanged page cannot consume the
    # whole step budget in a hidden loop.
    page.context._easy_apply_last_ai_action_signature = ""
    page.context._easy_apply_last_ai_before_fingerprint = ""
    page.context._easy_apply_no_progress_ai_attempts = 0
    if not already_open and not click_easy_apply(page):
        logger.error("Could not click Easy Apply button")
        return False

    # 等待弹窗加载（LinkedIn JS 渲染弹窗可能需要更长时间）
    if not already_open:
        page.wait_for_timeout(4000)

    # 记录上一步的弹窗文本，用于检测是否在同一步循环
    last_modal_text = ""
    same_step_count = 0
    step_history: List[tuple[int, int]] = []
    job_start_time = __import__("time").time()

    for step in range(1, max_steps + 1):
        page.wait_for_timeout(1500)

        # 检测提交成功
        if _check_submission_success(page):
            logger.info("Easy Apply submitted successfully!")
            return True

        # 检测弹窗是否存在
        modal = _get_modal(page)
        if not modal:
            page.wait_for_timeout(5000)
            modal = _get_modal(page)
            if not modal:
                if _check_submission_success(page):
                    return True
                logger.warning("Modal not found after 9s — may not have opened")
                return False

        # ── 循环检测：同一步重复 ≥3 次 → AI 帮助 ──
        try:
            current_text = modal.inner_text()[:500]
        except Exception:
            current_text = ""
        current_step = get_current_step_info(page)
        if current_step != (1, 1):
            step_history.append(current_step)
            # LinkedIn can silently cycle through 2/4 → 3/4 → 4/4, or a
            # shorter 2/3 → 3/3 loop, without advancing the form. Detect one
            # repeated period of 1–3 states before spending the full ten-step
            # budget on a dead application.
            cycle_period = next(
                (
                    period for period in (1, 2, 3)
                    if len(step_history) >= period * 2
                    and step_history[-period:] == step_history[-period * 2:-period]
                ),
                None,
            )
            if cycle_period is not None:
                cycle = step_history[-cycle_period:]
                logger.warning(
                    "Easy Apply step cycle detected (period=%s states=%s); requesting AI help",
                    cycle_period,
                    cycle,
                )
                if _request_ai_form_help(page, profile, resume_path, cover_letter_path):
                    page.wait_for_timeout(500)
                    if _check_submission_success(page) or not _get_modal(page):
                        return True
                    step_history = []
                    same_step_count = 0
                    last_modal_text = ""
                    continue
                return False
        if current_text and current_text == last_modal_text:
            same_step_count += 1
            if same_step_count >= 2:  # 降低到2次即触发
                logger.warning(f"Same step detected {same_step_count}x — requesting AI help")
                if _request_ai_form_help(page, profile, resume_path, cover_letter_path):
                    page.wait_for_timeout(1000)
                    if _check_submission_success(page) or not _get_modal(page):
                        return True
                    same_step_count = 0
                    last_modal_text = ""
                    continue
                else:
                    return False  # AI 未响应，跳过
        else:
            same_step_count = 0
        last_modal_text = current_text

        # 超时保护：单岗位超过5分钟 → AI 帮助/结束。多步 Easy Apply
        # 可能需要上传学历和等待 LinkedIn 控件稳定，2 分钟会在正常流程
        # 尚未到最后一步时误触发重复 help。
        elapsed = __import__("time").time() - job_start_time
        if elapsed > 300:
            logger.warning(f"Job taking too long ({int(elapsed)}s) — requesting AI help")
            if _request_ai_form_help(page, profile, resume_path, cover_letter_path):
                if _check_submission_success(page) or not _get_modal(page):
                    return True
            return False

        # 处理当前步骤
        logger.info(f"--- Easy Apply step {step} ---")
        if not handle_easy_apply_step(page, profile, resume_path, cover_letter_path, job_context=job_context):
            if getattr(page.context, "_cover_letter_required", False):
                # Policy is to skip Easy Apply roles that require a cover
                # letter; do not enter the generic AI retry/max-step path.
                logger.info("Cover letter requirement detected; abandoning Easy Apply immediately")
                return False
            if getattr(page.context, "_easy_apply_human_intervention_required", False):
                # A document-safety block is intentionally terminal for the
                # automated path.  Do not let the generic AI-help retry click
                # through a form whose CV has not been verified.
                logger.warning("Easy Apply document safety handoff active; preserving the modal")
                return False
            page.wait_for_timeout(1000)
            if _check_submission_success(page) or not _get_modal(page):
                return True
            break

    # 循环达到上限 → AI 帮助
    logger.warning("Max steps — requesting AI help")
    if _request_ai_form_help(page, profile, resume_path, cover_letter_path):
        page.wait_for_timeout(1000)
        return _check_submission_success(page) or not _get_modal(page)
    return False


def handle_easy_apply_flow(
    page: Page,
    profile: Dict[str, Any],
    resume_path: str,
    cover_letter_path: str = "",
    max_steps: int = 10,
    job_context: Optional[Dict[str, Any]] = None,
    already_open: bool = False,
) -> bool:
    """Run Easy Apply and clean up only when no human handoff is pending.

    A required cover letter, an AI no-progress guard, or an explicit user
    question must keep the current modal open so the human queue points at a
    usable form. Other failures are abandoned and cleaned up.
    """
    success = False
    flow_started = time.monotonic()
    try:
        success = _handle_easy_apply_flow_impl(
            page, profile, resume_path, cover_letter_path, max_steps=max_steps,
            job_context=job_context,
            already_open=already_open,
        )
        return success
    finally:
        logger.info("Easy Apply flow duration: %.1fs success=%s", time.monotonic() - flow_started, success)
        handoff_pending = bool(
            getattr(page.context, "_cover_letter_required", False)
            or getattr(page.context, "_easy_apply_human_intervention_required", False)
        )
        if not success and not handoff_pending:
            close_easy_apply_modal(page, reason="easy_apply_abandoned")


def get_cover_letter_status(page: Page) -> str:
    if getattr(page.context, "_cover_letter_uploaded", False):
        return "yes"
    if getattr(page.context, "_cover_letter_field_seen", False):
        return "no"
    return "not applicable"


# ── Radio question extraction & known-question matching ──

KNOWN_RADIO_QUESTIONS = {
    # (partial_keyword, answer)
    "legally authorized": "Yes",
    "work permit": "Yes",
    "authorized to work": "Yes",
    "right to work": "Yes",
    "require sponsorship": "Yes",
    "visa sponsorship": "Yes",
    "need sponsorship": "Yes",
    "distance": "Yes",
    "within": "Yes",
    "minutes drive": "Yes",
    "minutes away": "Yes",
    "close to": "Yes",
    "nearby": "Yes",
    "commuting distance": "Yes",
    "commute to": "Yes",
    "afstand": "Yes",
    "dichtbij": "Yes",
    "reside in": "Yes",
    "live in": "Yes",
    "currently located": "Yes",
    "freelancer": "Yes",
    "zzp": "Yes",
    "willing to relocate": "No",
    "ready to relocate": "No",
    "willing to travel": "Yes",
    "driver license": "No",
    "driver's license": "No",
    "driving license": "No",
    "rijbewijs": "No",
    "ever worked": "No",
    "worked at": "No",
    "worked for": "No",
    "previously worked": "No",
    "disability": "No",
    "veteran": "No",
    "gender": None,  # Skip — never auto-answer demographic questions
    "race": None,
    "ethnicity": None,
    "sexual orientation": None,
    "age": None,
    "salary expectation": None,  # Skip — variable per job
    "notice period": None,       # Skip — variable
}


def _match_known_radio_question(question_text: str) -> Optional[str]:
    """Try to match a radio question to known answers.

    Returns 'Yes', 'No', or None (unknown question → needs AI).
    Only returns a value for HIGH-CONFIDENCE keyword matches.
    """
    q = question_text.lower().strip()
    for keyword, answer in KNOWN_RADIO_QUESTIONS.items():
        if keyword in q:
            if answer is None:
                return None  # Explicit skip — needs human/AI
            return answer
    return None  # Unknown question → AI decides


def _extract_radio_groups(search_root) -> List[Dict]:
    """Extract radio button groups with their associated question text.

    LinkedIn Easy Apply uses [role='radio'] elements wrapped in various
    container structures. We group radios by their closest common parent
    and extract the question text from the preceding label/legend/heading.
    """
    groups = []
    seen_radios = set()

    for el in _get_choice_controls(search_root):
        try:
            if not el.is_visible():
                continue
            radio_id = el.evaluate("el => el.outerHTML")
            if radio_id in seen_radios:
                continue
            seen_radios.add(radio_id)

            txt = (_get_radio_label(el) or el.inner_text() or "").strip()[:40]
            # Check state: aria-checked for custom roles, .checked for native inputs
            aria_checked = el.get_attribute("aria-checked") or ""
            if aria_checked:
                checked = aria_checked.lower() == "true"
            else:
                try:
                    checked = el.evaluate("el => !!el.checked")
                except Exception:
                    checked = False

            # Find the question text: look for the closest preceding
            # legend, label, or heading in the parent hierarchy
            question = el.evaluate("""
                (el) => {
                    // S1: fieldset legend
                    let fs = el.closest('fieldset');
                    if (fs) { let lg = fs.querySelector('legend'); if (lg && lg.innerText.trim()) return lg.innerText.trim(); }

                    // S2: label with matching 'for' attribute
                    let rid = el.id;
                    if (rid) { let lbl = document.querySelector('label[for=\"' + rid + '\"]'); if (lbl && lbl.innerText.trim()) return lbl.innerText.trim(); }

                    // S3: find container and look for label/heading
                    let c = el.closest('[class*=\"form\"]') || el.closest('fieldset') ||
                            el.closest('div[class*=\"group\"]') || el.closest('div[class*=\"choice\"]') ||
                            el.closest('div[class*=\"selection\"]') || el.parentElement?.parentElement?.parentElement;
                    if (!c) c = el.parentElement?.parentElement;
                    if (c) {
                        let lbls = c.querySelectorAll('label, span[class*=\"label\"], span[class*=\"title\"], span[class*=\"question\"], h3, h4, h5, legend');
                        for (let lb of lbls) { let t = (lb.innerText||'').trim(); if (t.length > 2 && t.length < 250) return t; }
                        // First child with meaningful text
                        for (let ch of c.children) {
                            let t = (ch.innerText||'').trim();
                            if (t.length > 5 && t.length < 250 && !ch.querySelector('[role=\"radio\"], input[type=\"radio\"], button, [role=\"button\"], [role=\"option\"]')) return t;
                        }
                        // Container text minus radio labels
                        let dt = (c.innerText||'').trim();
                        let rs = c.querySelectorAll('[role=\"radio\"], input[type=\"radio\"], button, [role=\"button\"], [role=\"option\"]');
                        for (let r of rs) { dt = dt.replace(r.innerText?.trim()||'', ''); }
                        dt = dt.trim();
                        if (dt.length > 5 && dt.length < 250) return dt;
                    }

                    // S4: previous sibling (walk up if empty)
                    let p = el.parentElement?.previousElementSibling;
                    while (p && !(p.innerText||'').trim()) p = p.previousElementSibling;
                    if (p) { let t = p.innerText.trim(); if (t.length > 2 && t.length < 250) return t; }

                    // S5: grandparent's previous sibling
                    let gp = el.parentElement?.parentElement?.previousElementSibling;
                    if (gp) { let t = gp.innerText.trim(); if (t.length > 2 && t.length < 250) return t; }

                    return '';
                }
            """)

            # Group radios: find the group container for this radio
            group_key = el.evaluate("""
                (el) => {
                    let container = el.closest('[class*="form"]') ||
                                    el.closest('fieldset') ||
                                    el.parentElement?.parentElement;
                    return container ? container.className || container.tagName : 'unknown';
                }
            """)

            # Find or create the group
            existing = next((g for g in groups if g.get("key") == group_key), None)
            if existing:
                existing["radios"].append({"text": txt, "checked": checked})
                if question and not existing["question"]:
                    existing["question"] = question
            else:
                groups.append({
                    "key": group_key,
                    "question": question,
                    "radios": [{"text": txt, "checked": checked}],
                })

        except Exception:
            continue

    return groups


def _extract_full_form_state(page) -> str:
    """提取当前弹窗的完整表单状态，供 AI 分析。"""
    modal = _get_modal(page)
    search_root = modal if modal else page

    lines = ["=== EASY APPLY FORM STATE ==="]
    lines.append(f"URL: {page.url[:100]}")

    lines.append("\n--- PAGE/MODAL TEXT CONTEXT ---")
    try:
        text_context = search_root.inner_text() if modal else page.inner_text("body")
        lines.append(text_context[:3000])
    except Exception as e:
        lines.append(f"ERROR extracting text context: {e}")

    lines.append("\n--- PAGE/MODAL HTML EXCERPT ---")
    try:
        if modal:
            html_context = modal.evaluate("el => el.outerHTML")
        else:
            html_context = page.content()
        lines.append(" ".join(html_context.split())[:5000])
    except Exception as e:
        lines.append(f"ERROR extracting html context: {e}")

    # 页面步骤指示器
    for el in search_root.query_selector_all("*"):
        try:
            txt = el.inner_text().strip()
            if txt and "page" in txt.lower() and "/" in txt and len(txt) < 20:
                lines.append(f"STEP_INDICATOR: {txt}")
                break
        except: pass

    # 所有可见 input
    lines.append("\n--- INPUTS ---")
    for el in search_root.query_selector_all("input:not([type='hidden'])"):
        try:
            if not el.is_visible(): continue
            tag = el.evaluate("el => el.tagName")
            itype = el.get_attribute("type") or "text"
            name = el.get_attribute("name") or ""
            value = el.get_attribute("value") or ""
            placeholder = el.get_attribute("placeholder") or ""
            aria = el.get_attribute("aria-label") or ""
            label = _get_input_label(el)
            lines.append(f"[{tag}/{itype}] name='{name[:40]}' value='{value[:60]}' label='{label[:80]}' placeholder='{placeholder[:40]}' aria='{aria[:60]}'")
        except: pass

    # 所有可见 select
    lines.append("\n--- SELECTS ---")
    for el in search_root.query_selector_all("select"):
        try:
            if not el.is_visible(): continue
            name = el.get_attribute("name") or ""
            options = [o.inner_text().strip()[:40] for o in el.query_selector_all("option") if o.inner_text().strip()]
            selected = el.evaluate("el => el.options[el.selectedIndex]?.text || ''")
            label = _get_select_label(el)
            lines.append(f"[select] name='{name[:40]}' selected='{selected[:60]}' label='{label[:80]}' options={options[:10]}")
        except: pass

    # 所有可见 radio (LinkedIn custom) — 含 question 提取
    lines.append("\n--- RADIOS ---")
    radio_groups = _extract_radio_groups(search_root)
    for group in radio_groups:
        question = group.get("question", "")
        for r in group.get("radios", []):
            lines.append(f"[radio] text='{r['text'][:40]}' checked={r['checked']} question='{question[:120]}'")
        if question:
            # 尝试匹配标准答案
            known_answer = _match_known_radio_question(question)
            if known_answer is not None:
                lines.append(f"  ↳ AUTO-MATCHED: '{known_answer}'")

    # LinkedIn's native radio inputs are frequently visually hidden behind a
    # custom SDUI option wrapper.  They are still the authoritative checked
    # state, but the visible-radio extractor above intentionally ignores
    # hidden inputs.  Include a compact snapshot so the AI no-progress guard
    # can see that a click actually changed the selected answer.
    for radio in search_root.query_selector_all("input[type='radio']"):
        try:
            option = _get_radio_label(radio)[:60]
            question = _get_radio_context(radio)[:160]
            checked = bool(radio.is_checked())
            name = radio.get_attribute("name") or radio.get_attribute("id") or ""
            lines.append(
                f"[native-radio] name='{name[:60]}' option='{option}' "
                f"checked={checked} question='{question}'"
            )
        except Exception:
            continue

    # Some LinkedIn SDUI releases render the visible Yes/No options as plain
    # clickable nodes with no role, input, or label semantics.  Preserve a
    # compact diagnostic for AI help and future selector matching instead of
    # presenting an apparently empty RADIOS section.
    lines.append("\n--- CHOICE-LIKE CONTROLS ---")
    try:
        seen_choice = set()
        for el in search_root.query_selector_all("*"):
            if not el.is_visible():
                continue
            tag = el.evaluate("el => el.tagName.toLowerCase()")
            if tag in {"svg", "path", "rect", "circle", "script", "style"}:
                continue
            text = re.sub(r"\\s+", " ", (el.evaluate("el => el.innerText || el.textContent || ''") or "")).strip()
            if text.casefold() not in {"yes", "no", "ja", "nee"}:
                continue
            role = el.get_attribute("role") or ""
            aria = el.get_attribute("aria-label") or ""
            cls = (el.get_attribute("class") or "")[:120]
            parent_text = re.sub(r"\\s+", " ", (el.evaluate("el => el.parentElement?.innerText || ''") or "")).strip()[:240]
            key = (tag, role, aria, cls, parent_text)
            if key in seen_choice:
                continue
            seen_choice.add(key)
            lines.append(f"[choice] tag={tag} role='{role}' aria='{aria[:80]}' class='{cls}' parent='{parent_text}'")
            if len(seen_choice) >= 40:
                break
    except Exception as exc:
        lines.append(f"CHOICE_DIAGNOSTIC_ERROR: {exc}")

    # 所有 textarea
    lines.append("\n--- TEXTAREAS ---")
    for el in search_root.query_selector_all("textarea"):
        try:
            if not el.is_visible(): continue
            name = el.get_attribute("name") or ""
            value = el.evaluate("el => el.value") or ""
            label = _get_input_label(el)
            lines.append(f"[textarea] name='{name[:40]}' value='{value[:100]}' label='{label[:80]}'")
        except: pass

    # 所有按钮
    lines.append("\n--- BUTTONS ---")
    for el in search_root.query_selector_all("button"):
        try:
            if not el.is_visible(): continue
            txt = el.inner_text().strip()[:80]
            disabled = el.is_disabled()
            if txt:
                lines.append(f"[button] text='{txt}' disabled={disabled}")
        except: pass

    # 错误提示
    lines.append("\n--- ERRORS ---")
    for el in search_root.query_selector_all("[class*='error'], [class*='Error'], [role='alert'], .artdeco-inline-feedback--error"):
        try:
            if el.is_visible():
                txt = el.inner_text().strip()[:100]
                if txt: lines.append(f"[ERROR] {txt}")
        except: pass

    # 必填未填
    lines.append("\n--- REQUIRED UNFILLED ---")
    for el in search_root.query_selector_all("[required], [aria-required='true']"):
        try:
            name = el.get_attribute("name") or ""
            val = (el.get_attribute("value") or "").strip()
            if not val:
                lines.append(f"[REQUIRED_EMPTY] name='{name[:40]}'")
        except: pass

    return "\n".join(lines)


def _question_matches_context(question: str, context: str) -> bool:
    """Match an AI/user question to one visible field group without guessing."""
    import re

    q = re.sub(r"\s+", " ", str(question or "").casefold()).strip()
    c = re.sub(r"\s+", " ", str(context or "").casefold()).strip()
    if not q or not c:
        return False
    if q in c or c in q:
        return True
    stop = {"what", "which", "are", "you", "the", "this", "that", "with", "will", "your"}
    tokens = {t for t in re.findall(r"[a-z0-9À-ÿ]+", q) if len(t) >= 4 and t not in stop}
    if not tokens:
        return False
    overlap = sum(1 for token in tokens if token in c)
    return overlap >= max(2, int(len(tokens) * 0.55))


def _apply_user_answers_to_form(page, questions: list[str], answers: dict[str, str]) -> bool:
    """Apply answers supplied by the user to the still-open Easy Apply modal."""
    modal = _get_modal(page)
    search_root = modal if modal else page
    if not answers:
        return False
    applied: set[str] = set()

    for question in questions:
        answer = str(answers.get(question, "")).strip()
        if not answer:
            # Accept normalized/case-insensitive question keys from the CLI.
            answer = next(
                (str(value).strip() for key, value in answers.items()
                 if _question_matches_context(question, str(key))),
                "",
            )
        if not answer:
            continue
        question_key = question.casefold()

        # Native/custom radio groups.
        for radio in _get_choice_controls(search_root):
            try:
                if not radio.is_visible():
                    continue
                context = _get_radio_context(radio)
                if not _question_matches_context(question, context):
                    continue
                label = " ".join((
                    radio.inner_text() if radio.get_attribute("role") == "radio" else "",
                    _get_radio_label(radio),
                    radio.get_attribute("aria-label") or "",
                    radio.get_attribute("value") or "",
                )).strip().casefold()
                if answer.casefold() not in label:
                    continue
                checked = False
                try:
                    checked = radio.get_attribute("aria-checked") == "true" or radio.is_checked()
                except Exception:
                    pass
                if not checked:
                    radio.scroll_into_view_if_needed()
                    radio.click(force=True)
                    page.wait_for_timeout(350)
                applied.add(question_key)
                logger.info("User answer applied to radio question: %s → %s", question[:100], answer)
                break
            except Exception:
                continue
        # LinkedIn's current custom-radio renderer sometimes exposes only the
        # option text ("Yes"/"No") through the element context.  In that
        # case the normal label matcher cannot associate the option with its
        # question, even though the question is visibly in the same group.
        # Use a conservative ancestor-text fallback: score each visible radio
        # by the overlap between the requested question and the text of its
        # nearest form/fieldset ancestors, then click only a unique best
        # candidate.  This keeps unrelated Yes/No groups untouched.
        if question_key not in applied:
            try:
                candidates = []
                radios = _get_choice_controls(search_root)
                q_tokens = {
                    t for t in re.findall(r"[a-z0-9À-ÿ]+", question.casefold())
                    if len(t) >= 4 and t not in {"what", "which", "are", "you", "the", "this", "that", "with", "will", "your", "please", "answer"}
                }
                for idx, radio in enumerate(radios):
                    if not radio.is_visible():
                        continue
                    label = " ".join((
                        radio.inner_text() if radio.get_attribute("role") == "radio" else "",
                        _get_radio_label(radio),
                        radio.get_attribute("aria-label") or "",
                        radio.get_attribute("value") or "",
                    )).strip()
                    if answer.casefold() not in label.casefold():
                        continue
                    context = radio.evaluate("""
                        (el) => {
                            let out = [];
                            let n = el;
                            for (let i = 0; n && i < 8; i++, n = n.parentElement) {
                                let t = (n.innerText || '').trim();
                                if (t) out.push(t.slice(0, 1200));
                            }
                            return out.join('\\n');
                        }
                    """)
                    c_tokens = set(re.findall(r"[a-z0-9À-ÿ]+", str(context).casefold()))
                    score = sum(1 for token in q_tokens if token in c_tokens)
                    candidates.append((score, idx, radio))
                candidates.sort(key=lambda item: item[0], reverse=True)
                if candidates:
                    best_score = candidates[0][0]
                    tied = [item for item in candidates if item[0] == best_score]
                    # Require meaningful question evidence and a unique best
                    # group; never guess when all candidates look alike.
                    if best_score >= max(2, int(len(q_tokens) * 0.35)) and len(tied) == 1:
                        radio = candidates[0][2]
                        try:
                            if not (radio.get_attribute("aria-checked") == "true" or radio.is_checked()):
                                radio.scroll_into_view_if_needed()
                                radio.click(force=True)
                                page.wait_for_timeout(350)
                            applied.add(question_key)
                            logger.info("User answer applied via ancestor-text radio fallback: %s → %s", question[:100], answer)
                        except Exception:
                            pass
            except Exception:
                pass
        if question_key in applied:
            continue

        # Native select controls.
        for select in search_root.query_selector_all("select"):
            try:
                if not select.is_visible() or not _question_matches_context(question, _get_select_label(select)):
                    continue
                select.select_option(label=answer)
                page.wait_for_timeout(350)
                applied.add(question_key)
                logger.info("User answer applied to select question: %s → %s", question[:100], answer)
                break
            except Exception:
                continue
        if question_key in applied:
            continue

        # Text inputs and textareas.
        controls = list(search_root.query_selector_all("input:not([type='hidden']):not([type='file'])"))
        controls += list(search_root.query_selector_all("textarea"))
        for control in controls:
            try:
                if not control.is_visible() or not _question_matches_context(question, _get_input_label(control)):
                    continue
                control.click()
                control.fill(answer)
                page.wait_for_timeout(300)
                applied.add(question_key)
                logger.info("User answer applied to text question: %s → %s", question[:100], answer[:120])
                break
            except Exception:
                continue

    if applied:
        # Re-run deterministic filling so a user answer can be combined with
        # the normal profile/radio/document path on the same live modal.
        try:
            _fill_modal_form(page, getattr(page.context, "_easy_apply_profile", {}),
                             getattr(page.context, "_easy_apply_resume_path", ""),
                             getattr(page.context, "_easy_apply_cover_letter_path", ""))
        except Exception:
            pass
    return len(applied) == len([q for q in questions if q])


def _persist_easy_apply_answers(questions: list[str], answers: dict[str, str]) -> None:
    """Persist explicit user answers for future Easy Apply forms."""
    import json
    import hashlib
    from datetime import datetime

    qa_file = Path(__file__).resolve().parent.parent / "data" / "user_qa_preferences.json"
    try:
        data = json.loads(qa_file.read_text(encoding="utf-8-sig")) if qa_file.exists() else {"preferences": {}}
        prefs = data.setdefault("preferences", {})
        for question in questions:
            answer = next((str(v).strip() for k, v in answers.items() if _question_matches_context(question, str(k))), "")
            if not answer:
                continue
            key = "easy_apply_" + hashlib.sha1(question.encode("utf-8")).hexdigest()[:12]
            prefs[key] = {
                "question_patterns": [question],
                "answer": answer,
                "confirmed_at": datetime.now().isoformat(),
                "source": "easy_apply_user_input",
            }
        data["last_updated"] = datetime.now().isoformat()
        qa_file.parent.mkdir(parents=True, exist_ok=True)
        qa_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not persist Easy Apply user answers: %s", exc)


def _wait_for_easy_apply_user_answers(page, profile: dict, questions: list[str], form_state: str) -> bool:
    """Wait for a user answer file while keeping the current form open."""
    import json
    import os
    import time
    from datetime import datetime

    data_dir = Path(__file__).resolve().parent.parent / "data" / "linkedin"
    data_dir.mkdir(parents=True, exist_ok=True)
    question_file = data_dir / "form_user_question.json"
    answer_file = data_dir / "form_user_answer.json"
    if answer_file.exists():
        answer_file.unlink()
    event_id = f"easy-apply-user-{int(time.time() * 1000)}"
    question_file.write_text(json.dumps({
        "event_id": event_id,
        "created_at": datetime.now().isoformat(),
        "questions": questions,
        "url": page.url,
        "form_state": form_state,
        "answer_command": "python scripts\\respond_easy_apply.py --answer \"...\"",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    timeout = max(30, int(os.environ.get("EASY_APPLY_USER_WAIT_SECONDS", "300")))
    logger.warning(
        "Easy Apply requires a user answer; current form is preserved for up to %ss. Questions: %s",
        timeout,
        questions,
    )
    waited = 0
    while waited < timeout:
        time.sleep(2)
        waited += 2
        # A human may answer directly in the visible LinkedIn modal instead
        # of using respond_easy_apply.py.  Detect the employer receipt while
        # we are waiting so a real manual submission is not misclassified as
        # an unanswered question when the timeout expires.
        try:
            if _check_submission_success(page):
                logger.info("Easy Apply submission confirmed during user-answer wait")
                answer_file.unlink(missing_ok=True)
                question_file.unlink(missing_ok=True)
                return True
        except Exception:
            # The modal can briefly detach during LinkedIn navigation; keep
            # waiting for either a receipt or an explicit answer file.
            pass
        if not answer_file.exists():
            continue
        try:
            payload = json.loads(answer_file.read_text(encoding="utf-8"))
            if payload.get("event_id") not in (None, "", event_id):
                continue
            raw = payload.get("answers")
            if isinstance(raw, dict):
                answers = {str(k): str(v) for k, v in raw.items()}
            elif isinstance(payload.get("answer"), str) and len(questions) == 1:
                answers = {questions[0]: payload["answer"]}
            else:
                answers = {}
            if not answers:
                continue
            if _apply_user_answers_to_form(page, questions, answers):
                _persist_easy_apply_answers(questions, answers)
                answer_file.unlink(missing_ok=True)
                question_file.unlink(missing_ok=True)
                logger.info("Easy Apply user answers applied; resuming current form without restart")
                return True
            logger.warning("User answer file did not match every visible Easy Apply question; keeping form open")
        except Exception as exc:
            logger.warning("Could not consume Easy Apply user answer file: %s", exc)
    logger.warning("Easy Apply user-answer wait timed out after %ss; routing to human queue", timeout)
    return False


def _request_ai_form_help(page, profile, resume_path, cover_letter_path) -> bool:
    """同步 AI 帮助：提取表单状态 → 等待 AI 响应 → 执行指令。

    信号文件: ai_form_help_needed.flag + ai_form_help_done.flag
    超时 5 分钟 → 跳过当前岗位，不卡死整个流程。
    """
    import json, os, time
    from pathlib import Path

    help_attempts = int(getattr(page.context, "_easy_apply_ai_help_attempts", 0) or 0)
    if help_attempts >= 3:
        logger.error("Easy Apply AI help limit reached (3 attempts); abandoning this form")
        return False
    page.context._easy_apply_ai_help_attempts = help_attempts + 1

    data_dir = Path(__file__).resolve().parent.parent / "data" / "linkedin"
    data_dir.mkdir(parents=True, exist_ok=True)

    help_file = data_dir / "form_help.json"
    need_flag = data_dir / "form_help_needed.flag"
    done_flag = data_dir / "form_help_done.flag"
    instructions_file = data_dir / "form_instructions.json"

    for f in [need_flag, done_flag]:
        if f.exists(): f.unlink()

    try:
        form_state = _extract_full_form_state(page)
    except Exception as e:
        form_state = f"ERROR extracting form: {e}"
    help_data = {
        "form_state": form_state,
        "profile": profile,
        "timestamp": __import__("datetime").datetime.now().isoformat(),
    }
    with open(help_file, "w", encoding="utf-8") as f:
        json.dump(help_data, f, ensure_ascii=False, indent=2)

    need_flag.write_text(__import__("datetime").datetime.now().isoformat(), encoding="utf-8")
    logger.info(
        "🆘 AI form help needed! attempt %s/3 (waiting up to 5min)",
        page.context._easy_apply_ai_help_attempts,
    )

    waited = 0
    while not done_flag.exists():
        time.sleep(2)
        waited += 2
        if waited % 60 == 0:
            logger.info(f"   ...waiting for AI ({waited}s)")
        if waited > 120:
            logger.error("AI help timeout (2min) — skipping job")
            # 超时后清理残留 flag, 防止污染下次请求 (P0-1)
            for f in [need_flag, help_file]:
                try:
                    if f.exists(): f.unlink()
                except FileNotFoundError: pass
            return False

    # AI responded
    if not instructions_file.exists():
        logger.warning("No instructions file")
        done_flag.unlink()
        return False

    try:
        with open(instructions_file, "r", encoding="utf-8") as f:
            instructions = json.load(f)
    except Exception as e:
        logger.error(f"Bad instructions: {e}")
        return False

    if instructions.get("needs_user"):
        questions = [str(q).strip() for q in instructions.get("questions", []) if str(q).strip()]
        logger.warning(f"AI needs user input: {questions}")
        # A broken/unavailable Codex executable is not a user question.  Do
        # not wait five minutes for an answer to a runtime error; preserve the
        # existing handoff behavior and expose the real diagnostic.
        runtime_error = any(
            any(token in q.casefold() for token in (
                "codex exec returncode", "requires a newer version of codex",
                "usage limit reached", "quota", "model is unavailable",
            ))
            for q in questions
        )
        if not runtime_error and questions:
            unattended = os.environ.get("LINKEDIN_AUTOAPPLY_SKIP_HUMAN", "").strip().casefold() in {
                "1", "true", "yes", "on",
            }
            if unattended:
                logger.warning(
                    "Easy Apply needs user answers in unattended mode; "
                    "queueing immediately instead of waiting %ss: %s",
                    os.environ.get("EASY_APPLY_USER_WAIT_SECONDS", "300"),
                    questions,
                )
            elif _wait_for_easy_apply_user_answers(page, profile, questions, form_state):
                done_flag.unlink()
                for f in [need_flag, instructions_file]:
                    if f.exists(): f.unlink()
                return True
        page.context._easy_apply_human_intervention_required = True
        page.context._easy_apply_human_questions = questions
        done_flag.unlink()
        for f in [need_flag, instructions_file]:
            if f.exists(): f.unlink()
        return False

    actions = instructions.get("actions", [])
    logger.info(f"AI help received! Executing {len(actions)} actions...")

    # Capture a deterministic form snapshot before execution.  This is kept
    # separate from the human-readable log because it lets us distinguish a
    # real state transition from an AI action that returned successfully but
    # did not change the page (for example, clicking a disabled/covered Next
    # button).
    try:
        before_fingerprint = form_state
    except Exception:
        before_fingerprint = ""
    try:
        action_signature = json.dumps(actions, ensure_ascii=False, sort_keys=True)
    except Exception:
        action_signature = repr(actions)

    for action in actions:
        try:
            _execute_ai_instruction(page, action)
        except Exception as e:
            logger.warning(f"Action failed: {action.get('type')} — {e}")

    # Give React/LinkedIn a short settling window, then compare the complete
    # visible form state.  A short wait is intentional: this path is only used
    # after a detected stall and is much cheaper than another full AI round.
    try:
        page.wait_for_timeout(700)
        after_fingerprint = _extract_full_form_state(page)
    except Exception:
        after_fingerprint = ""

    previous_signature = getattr(page.context, "_easy_apply_last_ai_action_signature", "")
    previous_before = getattr(page.context, "_easy_apply_last_ai_before_fingerprint", "")
    no_progress = bool(before_fingerprint and after_fingerprint and before_fingerprint == after_fingerprint)
    repeated_no_progress = no_progress and action_signature == previous_signature and before_fingerprint == previous_before

    if no_progress:
        if repeated_no_progress:
            page.context._easy_apply_no_progress_ai_attempts = int(
                getattr(page.context, "_easy_apply_no_progress_ai_attempts", 0) or 0
            ) + 1
        else:
            page.context._easy_apply_no_progress_ai_attempts = 1
        logger.warning(
            "Easy Apply AI actions made no visible progress (attempt %s; action=%s)",
            page.context._easy_apply_no_progress_ai_attempts,
            action_signature[:180],
        )
    else:
        page.context._easy_apply_no_progress_ai_attempts = 0

    page.context._easy_apply_last_ai_action_signature = action_signature
    page.context._easy_apply_last_ai_before_fingerprint = before_fingerprint

    if repeated_no_progress and page.context._easy_apply_no_progress_ai_attempts >= 1:
        # Do not ask the same AI plan to be replayed again.  Route this case to
        # the existing human-intervention queue so the user can inspect a
        # browser-specific widget (custom dropdown, overlay, disabled button,
        # etc.) without losing the job or leaving the modal open.
        question = (
            "Easy Apply did not change after the same AI action was replayed. "
            "Please inspect the visible LinkedIn form and complete or advance it manually."
        )
        page.context._easy_apply_human_intervention_required = True
        page.context._easy_apply_human_questions = [question]
        logger.error("Easy Apply AI no-progress guard tripped; routing to human intervention")
        done_flag.unlink()
        for f in [need_flag, instructions_file]:
            if f.exists():
                f.unlink()
        return False

    done_flag.unlink()
    for f in [need_flag, instructions_file]:
        if f.exists(): f.unlink()
    return len(actions) > 0


def _ai_form_help_loop(page, profile, resume_path, cover_letter_path, max_retries=3) -> bool:
    """AI 表单帮助循环：请求 AI → 执行 → 检查错误 → 仍有错误则重试。

    Returns:
        True 如果 AI 帮助后表单成功提交或不再卡住
    """
    for retry in range(1, max_retries + 1):
        logger.info(f"AI help attempt {retry}/{max_retries}")

        if not _request_ai_form_help(page, profile, resume_path, cover_letter_path):
            logger.warning(f"AI help attempt {retry} failed (no response)")
            continue

        # 执行后检查是否还有错误
        page.wait_for_timeout(1000)
        form_state = _extract_full_form_state(page)

        # 检查是否提交成功
        if _check_submission_success(page):
            logger.info("Submitted after AI help!")
            return True

        # 检查是否还在同一步（modal 文本是否变化）
        still_errors = "[ERROR]" in form_state or "[REQUIRED_EMPTY]" in form_state
        if not still_errors:
            logger.info("No errors after AI help — trying to proceed")
            return True

        logger.info(f"Still has errors after AI help attempt {retry} — will retry")

    logger.warning(f"AI help exhausted after {max_retries} retries")
    return False


def _execute_ai_instruction(page, action: dict):
    """执行单条 AI 填表指令。"""
    atype = action.get("type", "")
    target = action.get("target", "")
    value = action.get("value", "")
    method = action.get("method", "fill")

    logger.info(f"  AI: {atype} | {target[:50]} = {value[:50]}")

    modal = _get_modal(page)
    search_root = modal if modal else page

    if atype == "fill_input":
        # 按 name/label/placeholder 查找 input 并填写
        el = None
        for attr in ["name", "aria-label", "placeholder"]:
            if target:
                el = search_root.query_selector(f'input[{attr}*="{target}"]')
                if el: break
        if not el:
            # 按 label 文本查找
            for inp in search_root.query_selector_all("input:not([type='hidden'])"):
                label = _get_input_label(inp)
                if target.lower() in label.lower():
                    el = inp; break
        if not el:
            # name 属性回退（处理 candidate[first_name] 等含方括号的 name）
            for inp in search_root.query_selector_all("input:not([type='hidden'])"):
                name_attr = (inp.get_attribute('name') or '').lower()
                if target.lower() in name_attr:
                    el = inp; break
        if el and el.is_visible():
            el.click(); el.fill(""); el.type(str(value), delay=30)
            page.wait_for_timeout(300)

    elif atype == "select_option":
        el = None
        for attr in ["name", "aria-label"]:
            if target:
                el = search_root.query_selector(f'select[{attr}*="{target}"]')
                if el: break
        if not el:
            for s in search_root.query_selector_all("select"):
                label = _get_select_label(s)
                if target.lower() in label.lower():
                    el = s; break
        if el and el.is_visible():
            el.select_option(label=value)
            page.wait_for_timeout(300)

    elif atype == "click_radio":
        # 优先搜索 [role='radio'] (LinkedIn 自定义)，再回退原生 input[type='radio']
        query = str(value or target).strip().lower()
        found = False
        for r in search_root.query_selector_all("[role='radio']"):
            if not (r.is_visible() and query and query in r.inner_text().strip().lower()):
                continue
            # Do not select the first generic "Yes" on the page.  A form can
            # contain several radio groups; bind the option to the question
            # text supplied by the agent when the target includes it.
            if target and target.casefold() != query:
                context = _get_radio_context(r).casefold()
                target_tokens = [t for t in re.findall(r"[a-z0-9À-ÿ]+", target.casefold()) if len(t) >= 4]
                if target_tokens and sum(1 for t in target_tokens if t in context) < max(2, int(len(target_tokens) * 0.45)):
                    continue
                if _click_radio_control(page, r, description=target or value):
                    page.wait_for_timeout(300)
                    found = True
                    break
        if not found:
            # 回退: 原生 radio — 按 value 或 label 匹配
            for r in search_root.query_selector_all("input[type='radio']"):
                if not query: continue
                r_val = (r.get_attribute("value") or "").lower()
                # LinkedIn's current renderer may expose the visible option
                # only in a sibling node (for example <p>Yes</p>) while the
                # native input has no value and an empty label[for].  Use the
                # radio-specific extractor so AI click_radio actions can
                # select that option instead of replaying Review.
                r_label = " ".join((_get_input_label(r), _get_radio_label(r))).lower()
                if query in r_val or query in r_label:
                    if target and target.casefold() != query:
                        context = _get_radio_context(r).casefold()
                        target_tokens = [t for t in re.findall(r"[a-z0-9À-ÿ]+", target.casefold()) if len(t) >= 4]
                        if target_tokens and sum(1 for t in target_tokens if t in context) < max(2, int(len(target_tokens) * 0.45)):
                            continue
                    if _click_radio_control(page, r, description=target or value):
                        page.wait_for_timeout(300)
                        try:
                            logger.info(
                                "AI radio selected: question=%s option=%s checked=%s",
                                target[:100], value[:40], r.is_checked(),
                            )
                        except Exception:
                            logger.info("AI radio click executed: question=%s option=%s", target[:100], value[:40])
                        found = True
                        break
        if not found:
            # LinkedIn's current renderer may expose the visible Yes/No
            # options as buttons while the native radio is hidden.  Reuse the
            # same bounded question-context guard and click the visible
            # choice control instead of replaying Review indefinitely.
            for r in _get_choice_controls(search_root):
                try:
                    label = " ".join(filter(None, [
                        (r.inner_text() or "").strip(),
                        (r.get_attribute("aria-label") or "").strip(),
                        (r.get_attribute("value") or "").strip(),
                    ])).casefold()
                    if not query or query not in label:
                        continue
                    if target and target.casefold() != query:
                        context = _get_radio_context(r).casefold()
                        target_tokens = [t for t in re.findall(r"[a-z0-9À-ÿ]+", target.casefold()) if len(t) >= 4]
                        if target_tokens and sum(1 for t in target_tokens if t in context) < max(2, int(len(target_tokens) * 0.45)):
                            continue
                    if _click_radio_control(page, r, description=target or value):
                        page.wait_for_timeout(300)
                        try:
                            logger.info(
                                "AI radio selected via visible choice: question=%s option=%s",
                                target[:100], value[:40],
                            )
                        except Exception:
                            pass
                        found = True
                        break
                except Exception:
                    continue

        if not found:
            logger.warning("AI radio target not found: question=%s option=%s", target[:120], value[:60])

    elif atype == "click_radio_by_index":
        # 按索引点击 radio（0-based）— 涵盖原生 + 自定义 radio
        # Agents have used both wire formats in practice:
        #   {target: "3", value: "Yes"}  (index in target, label in value)
        #   {target: "question text", value: "3"}  (index in value)
        # Accept either form so a descriptive label cannot make an otherwise
        # valid index action fail with ``invalid literal for int()``.
        raw_index = value if str(value).strip().lstrip("-").isdigit() else target
        try:
            idx = int(raw_index) if str(raw_index).strip() else 0
        except (TypeError, ValueError):
            logger.warning("AI radio-by-index action has no numeric index: target=%r value=%r", target, value)
            idx = -1
        # 优先 [role='radio']
        radios = [r for r in search_root.query_selector_all("[role='radio']") if r.is_visible()]
        if not radios:
            # 回退: 原生 input[type='radio']
            radios = [r for r in search_root.query_selector_all("input[type='radio']") if r.is_visible()]
        if 0 <= idx < len(radios):
            if _click_radio_control(page, radios[idx], description=f"index={idx}"):
                page.wait_for_timeout(300)

    elif atype == "click_checkbox":
        # 勾选 checkbox — 按 name/label/value 匹配
        # Boolean values (true/false) describe the desired state, not the
        # DOM label.  Prefer the natural-language target in that case.
        if isinstance(value, bool) or str(value).strip().lower() in {"true", "false"}:
            query = str(target or "").strip().lower()
        else:
            query = str(value or target).strip().lower()
        if not query:
            logger.warning("AI checkbox action has no target/value")
        for cb in search_root.query_selector_all("input[type='checkbox']"):
            if not query:
                break
            cb_name = (cb.get_attribute('name') or '').lower()
            cb_label = _get_input_label(cb).lower()
            cb_value = (cb.get_attribute('value') or '').lower()
            cb_aria = (cb.get_attribute('aria-label') or '').lower()
            if query in cb_name or query in cb_label or query in cb_value or query in cb_aria:
                try:
                    checked = cb.is_checked()
                except Exception:
                    checked = (cb.get_attribute("aria-checked") or "").casefold() == "true"
                if not checked:
                    cb.scroll_into_view_if_needed()
                    # LinkedIn often renders the native input off-screen and
                    # exposes the label as the actual click target.
                    label = _find_label_for_radio(cb)
                    if label and label.is_visible():
                        label.scroll_into_view_if_needed()
                        label.click(force=True)
                    else:
                        cb.click(force=True)
                    page.wait_for_timeout(300)
                break
            # Fallback for custom checkbox renderers whose visible label is
            # only present in an ancestor container.
            try:
                context = _get_element_context(cb).casefold()
                if query and query in context:
                    if not cb.is_checked():
                        label = _find_label_for_radio(cb)
                        if label and label.is_visible():
                            label.scroll_into_view_if_needed()
                            label.click(force=True)
                        else:
                            cb.scroll_into_view_if_needed()
                            cb.click(force=True)
                        page.wait_for_timeout(300)
                    break
            except Exception:
                continue

    elif atype == "click_button":
        # 支持 <button>, <a>, [role="button"] — 现代网站常用 <a> 或 <div> 做按钮
        clicked = False
        for selector in ["button", "a", "[role='button']"]:
            for el in search_root.query_selector_all(selector):
                if not el.is_visible():
                    continue
                el_text = (el.inner_text() or "").strip().lower()
                el_aria = (el.get_attribute("aria-label") or "").lower()
                if value.lower() in el_text or value.lower() in el_aria:
                    try:
                        if not el.is_disabled():
                            if "submit" in value.casefold() or "提交申请" in value:
                                page.context._easy_apply_submit_clicked = True
                            el.click(force=True)
                            page.wait_for_timeout(1000)
                            clicked = True
                            break
                    except Exception:
                        el.click(force=True)
                        page.wait_for_timeout(1000)
                        clicked = True
                        break
            if clicked:
                break

    elif atype == "upload_file":
        from pathlib import Path
        fp = str(Path(value).resolve())
        file_inputs = search_root.query_selector_all("input[type='file']")
        target_lower = (target or "").lower()
        matches = []
        for el in file_inputs:
            label = _get_input_label(el).lower()
            attrs = " ".join([
                el.get_attribute("id") or "",
                el.get_attribute("name") or "",
                el.get_attribute("aria-label") or "",
                label,
            ]).lower()
            if target_lower and target_lower in attrs:
                matches.append(el)
        # A missing/ambiguous target is a safety failure.  Selecting the first
        # file input can silently attach a cover letter to a CV field (or the
        # reverse), especially when LinkedIn keeps recent files in the UI.
        if not target_lower:
            logger.warning("AI upload_file rejected: target is missing; refusing first-file fallback")
            return
        if len(matches) != 1:
            logger.warning(
                "AI upload_file rejected: target=%s matched %s file inputs; refusing ambiguous upload",
                target, len(matches),
            )
            return
        chosen = matches[0]
        if any(token in target_lower for token in ("resume", "cv", "curriculum")):
            configured = str(getattr(page.context, "_easy_apply_resume_path", "") or "")
            if configured and Path(fp).resolve() != Path(configured).resolve():
                logger.error(
                    "AI upload_file blocked cross-document selection: target=%s configured_resume=%s requested=%s",
                    target, configured, fp,
                )
                return
        chosen.set_input_files(fp)
        page.wait_for_timeout(250)
        if not _verify_document_upload(search_root, "ai_upload", fp, chosen):
            logger.error("AI upload_file blocked: browser did not retain requested basename=%s", Path(fp).name)
            return
        if any(token in target_lower for token in ("resume", "cv", "curriculum")):
            page.context._easy_apply_resume_verified_path = str(Path(fp).resolve())

    elif atype == "press_key":
        page.keyboard.press(value)
        page.wait_for_timeout(500)

    elif atype == "wait":
        # P0修复: float 容错 (AI 可能写 "0.5" 而非 "1")
        try:
            seconds = float(str(value)) if value else 1.0
        except (ValueError, TypeError):
            seconds = 1.0
        page.wait_for_timeout(int(seconds * 1000))

    elif atype == "react_fill":
        # React-safe fill using native setter + event dispatch
        # target is a CSS selector, value is the text to fill
        if target:
            el = search_root.query_selector(f"[class*='{target}']")
            if el and el.is_visible():
                # Quill/rich text editor: use JS innerHTML
                if el.evaluate("el => el.classList.contains('ql-editor') || el.contentEditable === 'true'"):
                    el.evaluate("(el, val) => { el.innerHTML = '<p>' + val + '</p>'; }", str(value))
                    logger.info(f"  AI: react_fill (Quill) | {target[:40]} = filled")
                else:
                    _react_fill(page, target, str(value))
            else:
                _react_fill(page, target, str(value))
        else:
            # Fallback: find element by label text
            for inp in search_root.query_selector_all("input:not([type='hidden']):not([type='file'])"):
                try:
                    if inp.is_visible():
                        label = _get_input_label(inp)
                        if (value.lower() in label.lower() or
                                (target and target.lower() in label.lower())):
                            # Derive a CSS selector from element attributes
                            derived = page.evaluate("""el => {
                                if (el.id) return '#' + el.id;
                                if (el.name) return '[name="' + el.name + '"]';
                                return '';
                            }""", inp)
                            if derived:
                                _react_fill(page, derived, str(value))
                            break
                except Exception:
                    pass

    elif atype == "fill_combobox":
        # Handle role='combobox' fields: click, type, ArrowDown, Enter
        if target:
            _fill_combobox(page, target, str(value))
        else:
            # Find combobox by label/placeholder
            for cb in page.query_selector_all("input[role='combobox']"):
                try:
                    if cb.is_visible():
                        placeholder = cb.get_attribute("placeholder") or ""
                        aria_label = cb.get_attribute("aria-label") or ""
                        label_text = f"{placeholder} {aria_label}".lower()
                        if (value.lower() in label_text or
                                (target and target.lower() in label_text)):
                            derived = page.evaluate("""el => {
                                if (el.id) return '#' + el.id;
                                if (el.name) return '[name="' + el.name + '"]';
                                const all = document.querySelectorAll('input[role="combobox"]');
                                for (let i = 0; i < all.length; i++) {
                                    if (all[i] === el) return 'input[role="combobox"]:nth-of-type(' + (i+1) + ')';
                                }
                                return '';
                            }""", cb)
                            if derived:
                                _fill_combobox(page, derived, str(value))
                            break
                except Exception:
                    pass

    else:
        logger.warning(f"Unknown AI instruction type: {atype}")


def _check_submission_success(page: Page) -> bool:
    """检查是否提交成功 (多语言)。

    验证策略:
    1. 优先检查成功文本 (最可靠)
    2. 检查弹窗是否关闭 (弹窗关闭+无错误文本=成功)
    3. 检查 Easy Apply 按钮是否消失/变灰 (按钮消失说明已投过)
    4. NOTE: 不依赖 'applied' badge — 可能是投递前的旧 badge
    """
    try:
        # A post-submit LinkedIn dialog is often mounted after a long body
        # and can fall outside the old 3000-character slice.  Inspect every
        # visible modal first, then use the body as a fallback.
        visible_modal_text = []
        for selector in MODAL_SELECTORS:
            for candidate in page.query_selector_all(selector):
                try:
                    if candidate.is_visible():
                        visible_modal_text.append(candidate.inner_text())
                except Exception:
                    continue
        try:
            body_text = page.inner_text("body")
        except Exception:
            body_text = ""
        page_text = "\n".join(visible_modal_text + [body_text]).casefold()

        # 1. 成功文本 (最可靠)
        for text in SUCCESS_TEXTS:
            if text.casefold() in page_text:
                logger.info(f"✅ Submission confirmed: '{text}'")
                page.context._easy_apply_submission_confirmed = True
                return True
        # Employer-specific variants, including "Your application was sent to
        # <company>!", should be accepted even if the catalog has not yet
        # learned the exact company wording.
        if "your application was sent to" in page_text or "je sollicitatie is verzonden naar" in page_text:
            logger.info("✅ Submission confirmed by employer-specific sent-to receipt")
            page.context._easy_apply_submission_confirmed = True
            return True

        # 2. 弹窗是否已关闭 (关闭通常=提交完成)
        modal = _get_modal(page)
        if not modal and getattr(page.context, "_easy_apply_submit_clicked", False):
            # 确认不是因为错误而关闭
            error_texts = ["error", "错误", "fout", "required", "必填", "verplicht"]
            has_error = any(et in page_text for et in error_texts)
            if not has_error:
                logger.info("Modal closed without errors — submission likely complete")
                return True

        # 3. Easy Apply 按钮是否消失 (已投过的岗位按钮会变)
        ea_button = page.query_selector(".jobs-apply-button, button:has-text('Easy Apply'), button:has-text('快速申请')")
        if not ea_button and getattr(page.context, "_easy_apply_submit_clicked", False):
            # 按钮消失可能意味着已投递
            easily_applied = page.query_selector("button:has-text('Applied'), span:has-text('Applied'), button:has-text('已申请')")
            if easily_applied and easily_applied.is_visible():
                logger.info("'Applied' badge confirmed after modal close")
                return True

        return False
    except Exception as e:
        logger.debug(f"Success check error: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# Cover Letter 检测 + 专用 flag 创建 (非阻塞 — Agent 异步生成)
# ═══════════════════════════════════════════════════════════════

COVER_LETTER_TEXTAREA_KW = [
    "cover letter", "cover_letter", "coverletter",
    "motivation", "motivatie", "motivatiebrief",
    "why do you want", "tell us about yourself",
    "additional information", "personal statement",
    "why are you interested", "what makes you",
    "briefly describe", "introduce yourself",
]


def _is_cover_letter_field(el, label: str = "") -> bool:
    """检测 textarea 是否为 cover letter 字段。"""
    label_lower = (label or "").lower()
    if any(kw in label_lower for kw in COVER_LETTER_TEXTAREA_KW):
        return True
    try:
        el_id = (el.get_attribute("id") or "").lower()
        el_name = (el.get_attribute("name") or "").lower()
        placeholder = (el.get_attribute("placeholder") or "").lower()
        combined = f"{el_id} {el_name} {placeholder}"
        if any(kw in combined for kw in COVER_LETTER_TEXTAREA_KW):
            return True
    except Exception:
        pass
    return False


def _is_required_control(el, label: str = "") -> bool:
    """Return True only when a cover-letter control is explicitly mandatory."""
    try:
        if el.get_attribute("required") is not None:
            return True
        if (el.get_attribute("aria-required") or "").lower() == "true":
            return True
        context = _get_element_context(el)
        combined = f"{label} {context}".lower()
        if "optional" in combined or "not required" in combined or "if applicable" in combined:
            return False
        return any(token in combined for token in (
            "required", "mandatory", "must upload", "must provide",
            "verplicht", "obligatoire", "必填", "必须上传",
        ))
    except Exception:
        return False


COVER_LETTER_FILE_KW = [
    "cover letter", "cover_letter", "coverletter",
    "motivation letter", "motivatiebrief",
    "motivation", "motivatie", "sollicitatiebrief",
    "supporting document", "additional document",
]


def _is_cover_letter_file_input(el_id: str, el_name: str, label: str = "") -> bool:
    """检测 file input 是否为 cover letter 上传字段。"""
    combined = f"{el_id} {el_name} {label}".lower()
    return any(kw in combined for kw in COVER_LETTER_FILE_KW)


def _generate_required_cover_letter(
    page: Page,
    profile: Dict[str, Any],
    resume_path: str,
    job_context: Optional[Dict[str, Any]] = None,
) -> str:
    """Generate a DOCX for a required Easy Apply cover-letter control."""
    from src.cover_letter_agent import request_and_wait_cover_letter
    from src.jd_parser import extract_jd_text

    context = job_context or {}
    jd_text = str(context.get("jd_text", "") or "").strip()
    if len(jd_text) < 100:
        try:
            jd_text = extract_jd_text(page) or ""
        except Exception:
            jd_text = ""
    if len(jd_text) < 100:
        try:
            jd_text = page.locator("body").inner_text()[:12000]
        except Exception:
            jd_text = ""

    title = str(context.get("title", "") or "").strip()
    company = str(context.get("company", "") or "").strip()
    job_id = str(context.get("job_id", "") or "").strip()
    if not title:
        try:
            title = page.locator(
                ".job-details-jobs-unified-top-card__job-title, h1"
            ).first.inner_text().strip()
        except Exception:
            pass
    if not company:
        try:
            company = page.locator(
                ".job-details-jobs-unified-top-card__company-name"
            ).first.inner_text().strip()
        except Exception:
            pass

    if len(jd_text) < 100 or not title or not company:
        logger.warning(
            "Cannot generate required cover letter: incomplete context title=%r company=%r jd_chars=%s",
            title, company, len(jd_text),
        )
        return ""

    field_label = str(getattr(page.context, "_cover_letter_field_label", "cover letter upload") or "cover letter upload")
    logger.info(
        "📝 Required Easy Apply cover letter: requesting AI generation for %s @ %s (JD chars=%s)",
        title[:80], company[:80], len(jd_text),
    )
    try:
        result = request_and_wait_cover_letter(
            platform="linkedin",
            profile=profile,
            jd_text=jd_text,
            job_title=title,
            company=company,
            cl_field_info={"type": "file", "label": field_label},
            job_id=job_id,
            resume_path=resume_path,
        )
    except Exception as exc:
        logger.warning("Required Easy Apply cover-letter generation failed: %s", exc)
        return ""
    if not result or result.get("needs_user"):
        logger.warning("Required Easy Apply cover-letter generation needs user input")
        return ""
    path = str(result.get("cover_letter_docx_path") or result.get("cover_letter_path") or "").strip()
    if not path or not Path(path).is_file():
        logger.warning("Cover-letter agent returned no usable DOCX: %s", path)
        return ""
    logger.info("✅ Required Easy Apply cover letter ready: %s", path)
    return path


def _trigger_cover_letter_request(page, profile: dict, field_label: str = "", field_type: str = "textarea_or_file") -> Optional[dict]:
    """创建 cover_letter_needed.flag；Codex 模式下等待并返回生成结果。"""
    from src.cover_letter_agent import request_and_wait_cover_letter
    try:
        jd_text = ""
        job_title = ""
        company = ""
        try:
            jd_text = page.evaluate(
                "() => document.querySelector('.jobs-description__content') ? "
                "document.querySelector('.jobs-description__content').innerText : ''"
            )[:5000]
        except Exception:
            pass
        try:
            job_title = page.evaluate(
                "() => document.querySelector('.job-details-jobs-unified-top-card__job-title') ? "
                "document.querySelector('.job-details-jobs-unified-top-card__job-title').innerText : ''"
            ).strip()
        except Exception:
            pass
        try:
            company = page.evaluate(
                "() => document.querySelector('.job-details-jobs-unified-top-card__company-name') ? "
                "document.querySelector('.job-details-jobs-unified-top-card__company-name').innerText : ''"
            ).strip()
        except Exception:
            pass

        return request_and_wait_cover_letter(
            platform="linkedin",
            profile=profile,
            jd_text=jd_text,
            job_title=job_title,
            company=company,
            cl_field_info={"type": field_type, "label": field_label},
        )
    except Exception as e:
        logger.warning(f"Cover letter flag creation error: {e}")
        return None
