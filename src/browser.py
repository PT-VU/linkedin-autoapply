"""
浏览器管理模块 — 启动 Playwright 控制的 Edge 浏览器。
使用非持久化 context + Cookie 持久化，避免 Edge Profile 锁定问题。
"""

import json
import logging
import os
from pathlib import Path
import shutil
from typing import Optional

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

logger = logging.getLogger("linkedin_autoapply.browser")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COOKIE_FILE = PROJECT_ROOT / "data" / "linkedin_cookies.json"


def _edge_available() -> bool:
    candidates = [shutil.which("msedge"), shutil.which("msedge.exe")]
    if os.name == "nt":
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(variable)
            if base:
                candidates.append(str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
    return any(candidate and Path(candidate).is_file() for candidate in candidates)


def launch_browser(
    browser_type: str = "chromium",
    channel: str = "msedge",
    headless: bool = False,
    slow_mo: int = 300,
    profile_path: str = "",
) -> tuple[Playwright, Browser, BrowserContext, Page]:
    """启动浏览器并返回 (playwright, browser, context, page)。

    使用非持久化 context，通过 Cookie 文件保持登录态。
    修复了 BUG-001: 避免 persistent_context 与 Edge 进程冲突。

    Args:
        browser_type: "chromium"
        channel: "msedge" 使用 Microsoft Edge
        headless: 是否无头模式
        slow_mo: 操作间隔 (ms)
        profile_path: 已废弃，保留参数兼容性

    Returns:
        (Playwright, Browser, BrowserContext, Page) 元组
    """
    logger.info(f"Launching browser: {browser_type}/{channel}, headless={headless}")

    pw = sync_playwright().start()

    # 使用 launch() 而非 launch_persistent_context()
    # 避免 BUG-001: 与已运行的 Edge 进程冲突
    requested_channel = str(channel or "").strip()
    if requested_channel.casefold() in {"msedge", "edge"} and not _edge_available():
        logger.warning("Microsoft Edge is unavailable; falling back to Playwright Chromium")
        requested_channel = ""
    launch_options = {
        "headless": headless,
        "slow_mo": slow_mo,
        "args": [
            "--disable-blink-features=AutomationControlled",  # 隐藏自动化标志
            "--no-sandbox",
        ],
    }
    if requested_channel:
        launch_options["channel"] = requested_channel
    browser = pw.chromium.launch(**launch_options)

    context = browser.new_context(
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
        ),
    )

    # 尝试加载已保存的 Cookie
    if COOKIE_FILE.exists():
        try:
            with open(COOKIE_FILE, "r", encoding="utf-8") as f:
                cookies = json.load(f)
            context.add_cookies(cookies)
            logger.info(f"Loaded {len(cookies)} cookies from {COOKIE_FILE}")
        except Exception as e:
            logger.warning(f"Failed to load cookies: {e}")

    page = context.new_page()
    logger.info("Browser launched successfully (non-persistent context)")
    return pw, browser, context, page


def save_cookies(context: BrowserContext) -> None:
    """保存当前 context 的 Cookie 到文件，供下次复用。

    Args:
        context: BrowserContext 对象
    """
    try:
        cookies = context.cookies()
        COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved {len(cookies)} cookies to {COOKIE_FILE}")
    except Exception as e:
        logger.warning(f"Failed to save cookies: {e}")


def close_browser(pw: Playwright, browser: Browser, context: BrowserContext = None) -> None:
    """安全关闭浏览器并保存 Cookie。

    Args:
        pw: Playwright 实例
        browser: Browser 实例
        context: BrowserContext 实例（可选，用于保存 Cookie）
    """
    try:
        if context:
            save_cookies(context)
        browser.close()
        pw.stop()
        logger.info("Browser closed (cookies saved)")
    except Exception as e:
        logger.warning(f"Error closing browser: {e}")
