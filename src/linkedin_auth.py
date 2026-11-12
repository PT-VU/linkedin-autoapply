"""
LinkedIn 登录模块 — 检测登录状态，引导或等待用户完成登录。
"""

import logging
import time
from typing import Any, Dict, Optional

from playwright.sync_api import Page

logger = logging.getLogger("linkedin_autoapply.auth")

LINKEDIN_URL = "https://www.linkedin.com"
LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
LINKEDIN_FEED_URL = "https://www.linkedin.com/feed/"


def is_logged_in(page: Page) -> bool:
    """检测当前页面是否已登录 LinkedIn。

    Args:
        page: Playwright Page 对象

    Returns:
        True 如果已登录
    """
    try:
        current_url = page.url

        # A URL alone is not proof of authentication. LinkedIn can leave a
        # feed URL in the address bar while showing an auth wall or a
        # checkpoint. This guard prevents the workflow from starting early.
        lowered_url = (current_url or "").casefold()
        if any(marker in lowered_url for marker in ("/login", "authwall", "checkpoint", "challenge")):
            return False

        # Look for authenticated navigation/profile markers. Keep this list
        # deliberately conservative; generic links on the public homepage are
        # not enough to start a run.
        selectors = [
            ".global-nav__me",
            ".share-box-feed-entry__avatar",
            '[data-control-name="identity_profile_photo"]',
            ".profile-rail-card",
            'button[aria-label*="Me"]',
            'a[href*="/in/"] img',
        ]

        for selector in selectors:
            element = page.query_selector(selector)
            if element and element.is_visible():
                logger.debug(f"Login detected via selector: {selector}")
                return True

        # 检查 cookie 中是否有 li_at（LinkedIn session cookie）
        cookies = page.context.cookies()
        for cookie in cookies:
            if cookie.get("name") == "li_at" and cookie.get("value"):
                logger.debug("Login detected via li_at cookie")
                return True

        return False

    except Exception as e:
        logger.warning(f"Error checking login state: {e}")
        return False


def ensure_logged_in(
    page: Page,
    timeout_seconds: int = 120,
    config: Optional[Dict[str, Any]] = None,
) -> bool:
    """确保已登录 LinkedIn。

    如果已登录则直接返回。
    如果未登录，导航到登录页面并等待用户手动完成登录。
    登录成功后自动保存 Cookie 供后续复用。

    Args:
        page: Playwright Page 对象
        timeout_seconds: 等待用户登录的超时时间

    Returns:
        True 如果已登录
    """
    from .browser import save_cookies

    auth_cfg = ((config or {}).get("linkedin", {}) or {}).get("auth", {}) or {}
    login_url = str(auth_cfg.get("login_url") or LINKEDIN_LOGIN_URL)
    success_url = str(auth_cfg.get("success_url") or LINKEDIN_FEED_URL)
    timeout_seconds = int(auth_cfg.get("login_timeout_seconds", timeout_seconds) or timeout_seconds)
    poll_seconds = max(1, int(auth_cfg.get("poll_interval_seconds", 2) or 2))

    # First check the configured success page. A saved Edge profile or a
    # restored cookie jar can satisfy this without any user interaction.
    page.goto(success_url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)

    if is_logged_in(page):
        logger.info("Already logged in to LinkedIn (via saved cookies)")
        return True

    # No active session: show the normal LinkedIn login page. We never read
    # or store passwords; the user completes the normal browser login/2FA.
    logger.info("No active LinkedIn session — opening the interactive login page")
    page.goto(login_url, wait_until="domcontentloaded", timeout=30000)

    # 给浏览器自动填充一些时间
    page.wait_for_timeout(3000)

    # 检查是否自动登录成功（浏览器可能自动填充并提交）
    if is_logged_in(page):
        logger.info("Auto-login succeeded via browser saved credentials")
        save_cookies(page.context)
        return True

    # Wait without clicking or guessing at credentials. This also handles
    # SSO, passkeys, CAPTCHA, and two-factor steps that require the user.
    logger.info("Waiting for you to finish login in the visible browser (%ss timeout)", timeout_seconds)
    logger.info("Complete email/password, SSO, and any 2FA in that window; the run continues automatically.")

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        page.wait_for_timeout(poll_seconds * 1000)
        if is_logged_in(page):
            logger.info("LinkedIn login detected — saving the browser session for future runs")
            save_cookies(page.context)
            return True
        remaining = max(0, int(deadline - time.monotonic()))
        if remaining and remaining % 10 < poll_seconds:
            logger.info("Still waiting for LinkedIn login (%ss remaining)", remaining)

    logger.error(f"Login timeout after {timeout_seconds}s")
    return False


def go_to_jobs_page(page: Page) -> bool:
    """导航到 LinkedIn Jobs 页面。

    Args:
        page: Playwright Page 对象

    Returns:
        True 如果导航成功
    """
    try:
        page.goto("https://www.linkedin.com/jobs/", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        logger.info("Navigated to LinkedIn Jobs page")
        return True
    except Exception as e:
        logger.error(f"Failed to navigate to Jobs page: {e}")
        return False
