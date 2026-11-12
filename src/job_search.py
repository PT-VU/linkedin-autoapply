"""
岗位搜索模块 — 在 LinkedIn Jobs 页面搜索岗位、翻页、采集岗位卡片。
"""

import hashlib
import logging
import re
import time
from typing import Any, Dict, List, Optional

from playwright.sync_api import Page

logger = logging.getLogger("linkedin_autoapply.search")

LINKEDIN_JOBS_URL = "https://www.linkedin.com/jobs/"


def build_search_url(
    keywords: str = "Artificial Intelligence",
    location: str = "",
    date_posted: str = "",
) -> str:
    """构建 LinkedIn 岗位搜索 URL。

    Args:
        keywords: 搜索关键词
        location: 地点

    Returns:
        完整的搜索 URL
    """
    import urllib.parse

    params = {
        "keywords": keywords,
        "location": location,
        "position": "1",
        "pageNum": "0",
    }
    # LinkedIn's authenticated SPA may drop a free-text country after it
    # redirects to ``jobs/search-results``.  Supplying the country geoId keeps
    # the search location attached to the request.  The value is a stable
    # LinkedIn geography identifier, not a user credential.
    country_geo_ids = {
        "belgium": "100565514",
        "netherlands": "102890719",
        "france": "105015875",
        "germany": "101282230",
    }
    normalized_location = str(location or "").strip().casefold()
    if normalized_location in country_geo_ids:
        params["geoId"] = country_geo_ids[normalized_location]
        params["refresh"] = "true"
    # Apply the time window in the URL as well as (optionally) through the
    # visible filter UI.  The URL parameter is deterministic and prevents a
    # failed/changed LinkedIn filter selector from silently falling back to
    # all historical results.  LinkedIn uses seconds for f_TPR.
    tpr = {
        "Past 24 hours": "r86400",
        "Past Week": "r604800",
        "Past 2 weeks": "r1209600",
        "Past Month": "r2592000",
    }.get(str(date_posted or "").strip())
    if tpr:
        params["f_TPR"] = tpr
    query = urllib.parse.urlencode(params)
    return f"{LINKEDIN_JOBS_URL}search/?{query}"


def apply_search_filters(page: Page, config: Dict[str, Any]) -> bool:
    """在搜索结果页面应用筛选条件。

    尝试点击 LinkedIn 筛选器来设置日期范围、经验级别等。
    注意：LinkedIn 的 DOM 结构经常变化，此方法需要容错。

    Args:
        page: Playwright Page 对象
        config: 搜索配置

    Returns:
        True 如果筛选应用成功（或不需要筛选）
    """
    search_config = config.get("search", {})

    # 等待筛选面板加载
    page.wait_for_timeout(2000)

    # 尝试点击 "All filters" 按钮
    try:
        all_filters_btn = page.query_selector(
            'button:has-text("All filters"), [aria-label*="filter" i]'
        )
        if not all_filters_btn:
            # LinkedIn 有时直接在页面上显示筛选器
            logger.debug("All filters button not found, filters may already be visible")
        else:
            all_filters_btn.click()
            page.wait_for_timeout(1500)
    except Exception as e:
        logger.debug(f"Could not click All filters: {e}")

    # 日期筛选
    date_posted = search_config.get("date_posted", "")
    if date_posted and date_posted != "Any Time":
        try:
            _click_filter_option(page, "Date posted", date_posted)
            page.wait_for_timeout(1000)
        except Exception as e:
            logger.debug(f"Date filter not applied: {e}")

    # 经验级别筛选
    experience_levels = search_config.get("experience_level", [])
    if experience_levels:
        try:
            for level in experience_levels:
                _click_filter_option(page, "Experience level", level)
                page.wait_for_timeout(500)
        except Exception as e:
            logger.debug(f"Experience level filter not applied: {e}")

    # 确认筛选（如果有 "Show results" 按钮）
    try:
        show_results_btn = page.query_selector('button:has-text("Show results")')
        if show_results_btn:
            show_results_btn.click()
            page.wait_for_timeout(2000)
    except Exception:
        pass

    logger.info("Search filters applied")
    return True


def _click_filter_option(page: Page, filter_name: str, option_text: str) -> None:
    """在筛选面板中点击一个选项。

    Args:
        page: Page 对象
        filter_name: 筛选器名称 (e.g. "Date posted")
        option_text: 选项文本 (e.g. "Past Week")
    """
    # 尝试找到并点击筛选器按钮
    filter_btn = page.query_selector(f'button:has-text("{filter_name}"), fieldset:has-text("{filter_name}") button, [id*="filter"]:has-text("{filter_name}")')
    if filter_btn:
        filter_btn.click()
        page.wait_for_timeout(500)

    # 点击选项
    option = page.query_selector(f'label:has-text("{option_text}"), span:has-text("{option_text}"), [role="option"]:has-text("{option_text}"), input[value*="{option_text}"]')
    if option:
        option.click()
        page.wait_for_timeout(300)


def collect_job_cards(page: Page) -> List[Dict[str, Any]]:
    """从当前搜索结果页面采集所有岗位卡片的基本信息。

    Args:
        page: Playwright Page 对象（应在搜索结果页）

    Returns:
        岗位卡片列表，每项包含 job_id, title, company, location, link
    """
    jobs = []

    # 等待岗位列表加载
    page.wait_for_timeout(2000)

    # LinkedIn 岗位卡片的多种选择器
    card_selectors = [
        ".job-card-container",
        ".jobs-search-results__list-item",
        '[data-job-id]',
        ".job-card-list__entity-lockup",
        "li.jobs-search-results__list-item",
    ]

    cards = []
    for selector in card_selectors:
        cards = page.query_selector_all(selector)
        if cards:
            logger.debug(f"Found {len(cards)} job cards using: {selector}")
            break

    # The AI-results surface often renders after the initial DOM load. Retry
    # before treating a visible search page as empty.
    if not cards:
        for _ in range(4):
            page.wait_for_timeout(1500)
            for selector in card_selectors:
                cards = page.query_selector_all(selector)
                if cards:
                    logger.debug(f"Found {len(cards)} job cards after retry using: {selector}")
                    break
            if cards:
                break
            jobs = _collect_ai_search_cards(page)
            if jobs:
                logger.info(f"Collected {len(jobs)} AI-search job cards from current page")
                return jobs

    if not cards:
        # 回退：尝试更宽泛的选择器
        cards = page.query_selector_all('[data-job-id], .job-card-container, li:has(.job-card-list__title)')
        logger.debug(f"Fallback: found {len(cards)} job cards")

    if not cards:
        jobs = _collect_ai_search_cards(page)
        if jobs:
            logger.info(f"Collected {len(jobs)} AI-search job cards from current page")
            return jobs

    for card in cards:
        try:
            job_data = _extract_card_data(card)
            if job_data and job_data.get("job_id"):
                jobs.append(job_data)
        except Exception as e:
            logger.debug(f"Failed to extract card data: {e}")
            continue

    logger.info(f"Collected {len(jobs)} job cards from current page")
    return jobs


def _collect_ai_search_cards(page: Page) -> List[Dict[str, Any]]:
    """Fallback for LinkedIn's AI-powered job search cards.

    This UI currently renders result cards as generated-class div[role=button]
    blocks without stable data-job-id attributes.
    """
    jobs: List[Dict[str, Any]] = []
    seen: set[str] = set()
    cards = page.query_selector_all('div[role="button"]')
    applied = rejected = duplicate = 0

    for idx, card in enumerate(cards):
        try:
            text = card.inner_text().strip()
        except Exception:
            continue
        if not _looks_like_ai_search_job_card(text):
            rejected += 1
            continue
        is_applied = bool(re.search(r"(?im)^\s*(applied|已申请|已应聘|gesolliciteerd)\s*$", text))
        if is_applied:
            logger.info("LinkedIn card marked Applied")
            applied += 1

        job = _parse_ai_search_card_text(text)
        if not job.get("title") or not job.get("company"):
            continue

        key = "|".join([
            job.get("title", "").lower(),
            job.get("company", "").lower(),
            job.get("location", "").lower(),
        ])
        if key in seen:
            duplicate += 1
            continue
        seen.add(key)

        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        job["job_id"] = f"li_ai_{digest}"
        job["link"] = page.url
        job["ai_card_index"] = idx
        job["ai_card_text"] = text[:1000]
        job["linkedin_applied"] = is_applied
        jobs.append(job)

    logger.info("AI-search scan: role buttons=%s, accepted=%s, applied=%s, duplicates=%s, non-card=%s", len(cards), len(jobs), applied, duplicate, rejected)
    return jobs


def _looks_like_ai_search_job_card(text: str) -> bool:
    if not text or len(text) < 20 or len([line for line in text.splitlines() if line.strip()]) < 3:
        return False
    low = text.lower()
    if low in {"date posted", "experience level", "employment type", "company"}:
        return False
    return (any(token in low for token in ["posted", "easy apply", "viewed", "applied", "promoted", "applicant"])
            or bool(re.search(r'\b\d+\s+(?:minute|hour|day|week|month)s?\s+ago\b', low)))


def _parse_ai_search_card_text(text: str) -> Dict[str, Any]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and ln.strip() != "·"]
    lines = [_clean_ai_card_line(ln) for ln in lines if _clean_ai_card_line(ln)]

    if lines and lines[0].lower().startswith("selected,"):
        lines[0] = lines[0].split(",", 1)[1].strip()

    title = lines[0] if lines else ""
    company_idx = 2 if len(lines) > 1 and lines[1].lower() == title.lower() else 1
    company = lines[company_idx] if len(lines) > company_idx else ""
    location = lines[company_idx + 1] if len(lines) > company_idx + 1 else ""

    salary = ""
    for line in lines:
        if re.search(r"(\bEUR\b|\$|€|/month|/year|/hr)", line, re.I):
            salary = line
            break

    return {
        "job_id": "",
        "title": title,
        "company": company,
        "location": location,
        "link": "",
        "salary_preview": salary,
    }


def _clean_ai_card_line(line: str) -> str:
    line = re.sub(r"\s+", " ", line).strip()
    line = re.sub(r"\s*\(Verified job\)\s*", "", line, flags=re.I).strip()
    if line.lower() in {"selected", "viewed", "applied", "promoted"}:
        return ""
    return line


def _extract_card_data(card) -> Optional[Dict[str, Any]]:
    """从单个岗位卡片提取数据。

    Args:
        card: Playwright ElementHandle

    Returns:
        岗位数据字典或 None
    """
    # 提取 job_id
    job_id = card.get_attribute("data-job-id") or card.get_attribute("data-entity-urn")
    if job_id and ":" in job_id:
        job_id = job_id.split(":")[-1]  # urn:li:jobPosting:123 → 123
    if not job_id:
        # 尝试从内部元素获取
        inner = card.query_selector('[data-job-id]')
        if inner:
            job_id = inner.get_attribute("data-job-id")

    # 提取标题
    title_el = card.query_selector(
        ".job-card-list__title, .job-card-container__title, .job-card-list__title--link, "
        ".artdeco-entity-lockup__title, a.job-card-container__link, h3"
    )
    title = title_el.inner_text().strip() if title_el else ""

    # 提取公司名
    company_el = card.query_selector(
        ".job-card-container__company-name, .job-card-container__primary-description, "
        ".artdeco-entity-lockup__subtitle, .job-card-list__company-name"
    )
    company = company_el.inner_text().strip() if company_el else ""

    # 提取地点
    location_el = card.query_selector(
        ".job-card-container__metadata-item, .artdeco-entity-lockup__caption, "
        ".job-card-list__metadata-item"
    )
    location = location_el.inner_text().strip() if location_el else ""

    # 提取链接
    link_el = card.query_selector("a")
    link = link_el.get_attribute("href") if link_el else ""
    if link and not link.startswith("http"):
        link = f"https://www.linkedin.com{link}"

    # 提取薪资（如果有）
    salary_el = card.query_selector('[class*="salary"], [class*="compensation"]')
    salary = salary_el.inner_text().strip() if salary_el else ""

    if not title and not company:
        return None

    return {
        "job_id": job_id or "",
        "title": title,
        "company": company,
        "location": location,
        "link": link,
        "salary_preview": salary,
    }


def go_to_next_page(page: Page, current_page: int) -> bool:
    """翻到下一页搜索结果。

    Args:
        page: Page 对象
        current_page: 当前页码

    Returns:
        True 如果翻页成功，False 如果已到最后一页
    """
    try:
        # LinkedIn 的分页按钮
        next_btn = page.query_selector(
            f'button[aria-label="Page {current_page + 1}"], '
            f'[data-test-pagination-page-btn="{current_page + 1}"], '
            f'button:has-text("{current_page + 1}")'
        )
        if not next_btn:
            # 尝试用 "next" / "→" 按钮
            next_btn = page.query_selector(
                'button[aria-label="Next"], [aria-label="Next Page"], '
                '.artdeco-pagination__button--next'
            )

        if not next_btn:
            logger.info("No next page button found — reached last page")
            return False

        if next_btn.is_disabled():
            logger.info("Next page button disabled — reached last page")
            return False

        next_btn.click()
        page.wait_for_timeout(3000)
        logger.info(f"Navigated to page {current_page + 1}")
        return True

    except Exception as e:
        logger.warning(f"Pagination failed: {e}")
        return False


def search_and_collect(
    page: Page,
    config: Dict[str, Any],
    max_pages: int = 5,
) -> List[Dict[str, Any]]:
    """执行搜索并采集所有岗位。

    Args:
        page: Playwright Page 对象
        config: 全局配置
        max_pages: 最大翻页数

    Returns:
        所有岗位卡片数据列表
    """
    search_config = config.get("search", {})

    # 构建搜索 URL
    keyword = search_config.get("keywords", ["Artificial Intelligence"])
    if isinstance(keyword, list):
        keyword = keyword[0] if keyword else "Artificial Intelligence"

    location = search_config.get("location", "")
    url = build_search_url(keyword, location, search_config.get("date_posted", ""))

    logger.info(f"Searching: '{keyword}' in '{location}'")
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    # 应用筛选条件
    apply_search_filters(page, config)

    # 采集所有页面的岗位
    all_jobs = []
    for page_num in range(1, max_pages + 1):
        logger.info(f"Collecting page {page_num}/{max_pages}...")
        page.wait_for_timeout(2000)

        jobs = collect_job_cards(page)
        all_jobs.extend(jobs)

        if page_num < max_pages:
            if not go_to_next_page(page, page_num):
                break

    # 按 job_id 去重
    seen = set()
    unique_jobs = []
    for j in all_jobs:
        jid = j.get("job_id", "")
        if jid and jid not in seen:
            seen.add(jid)
            unique_jobs.append(j)

    logger.info(f"Total unique jobs collected: {len(unique_jobs)}")
    return unique_jobs
