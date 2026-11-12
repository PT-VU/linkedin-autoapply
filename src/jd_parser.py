"""
JD 解析模块 — 从 LinkedIn 岗位详情页提取完整的职位描述 (Job Description)。
"""

import logging
from typing import Optional

from playwright.sync_api import Page

logger = logging.getLogger("linkedin_autoapply.jd_parser")


def open_job_detail(page: Page, job: dict, verify_description=True) -> bool:
    """在搜索结果页上点击岗位卡片，打开侧边栏详情。

    关键：不离开搜索页！不导航到 /jobs/view/{id}/。
    这样投递完成后可以直接回到搜索结果继续下一个岗位。

    Returns:
        True 如果侧边栏详情打开成功
    """
    job_id = job.get("job_id", "")

    # LinkedIn's AI-powered search may expose anonymous result cards without
    # data-job-id. Use the captured div[role=button] index from job_search.py.
    ai_card_index = job.get("ai_card_index") if not job_id.isdigit() else None
    if ai_card_index is not None:
        try:
            from .job_search import _parse_ai_search_card_text
            # The collector records the exact role-button index.  Use it first
            # instead of reparsing every role button for every card (the old
            # fallback made collection O(cards²)).  Fall back to identity
            # matching when LinkedIn reorders the generated buttons.
            buttons = page.query_selector_all('div[role="button"]')
            direct = None
            try:
                index = int(ai_card_index)
                if 0 <= index < len(buttons):
                    candidate = buttons[index]
                    parsed = _parse_ai_search_card_text(candidate.inner_text())
                    if all(str(parsed.get(key, "")).strip().lower() == str(job.get(key, "")).strip().lower()
                           for key in ("title", "company", "location") if job.get(key)):
                        direct = candidate
            except Exception:
                direct = None
            if direct is not None:
                direct.scroll_into_view_if_needed()
                page.wait_for_timeout(300)
                direct.click()
                page.wait_for_timeout(1800)
                title_matches = job.get("title", "").lower() in page.title().lower()
                if title_matches and (not verify_description or len(extract_jd_text(page)) > 100):
                    logger.debug("Opened detail via stored AI-search card index: %s", ai_card_index)
                    return True
            matches = []
            for candidate in buttons:
                parsed = _parse_ai_search_card_text(candidate.inner_text())
                if all(str(parsed.get(key, "")).strip().lower() == str(job.get(key, "")).strip().lower()
                       for key in ("title", "company", "location") if job.get(key)):
                    matches.append(candidate)
            if len(matches) != 1:
                logger.warning("AI-search card identity missing or ambiguous: %s", job.get("title"))
                return False
            card = matches[0]
            card.scroll_into_view_if_needed()
            page.wait_for_timeout(500)
            card.click()
            page.wait_for_timeout(3000)
            title_matches = job.get("title", "").lower() in page.title().lower()
            if title_matches and (not verify_description or len(extract_jd_text(page)) > 100):
                logger.debug(f"Opened detail via AI-search card identity: {ai_card_index}")
                return True
        except Exception as e:
            logger.debug(f"AI-search card click failed: {e}")

    if job_id.startswith("li_ai_"):
        logger.warning("Anonymous search card was not opened; refusing synthetic job URL")
        return False

    # 方式 1: 在搜索页点击卡片（首选 — 不离开搜索页）
    if job_id:
        for selector in [f'[data-job-id="{job_id}"]', f'[data-occludable-job-id="{job_id}"]']:
            card = page.query_selector(selector)
            if card:
                try:
                    # 滚动到卡片可见区域
                    card.scroll_into_view_if_needed()
                    page.wait_for_timeout(500)
                    card.click()
                    page.wait_for_timeout(3000)
                    # 额外等待侧边栏的 Apply 按钮渲染（不抛异常）
                    try:
                        page.wait_for_selector('.jobs-s-apply', timeout=5000)
                    except Exception:
                        pass
                    page.wait_for_timeout(500)
                    # 验证 JD 是否加载
                    if not verify_description or len(extract_jd_text(page)) > 100:
                        logger.debug(f"Opened detail via card click: {job_id}")
                        return True
                except Exception as e:
                    logger.debug(f"Card click failed: {e}")

    # 方式 2: 通过卡片中的链接点击
    link_selectors = [
        f'[data-job-id="{job_id}"] a',
    ]
    for sel in link_selectors:
        link = page.query_selector(sel)
        if link:
            try:
                link.click()
                page.wait_for_timeout(2000)
                if not verify_description or len(extract_jd_text(page)) > 100:
                    return True
            except Exception:
                pass

    # 方式 3: 回退 — URL 导航（会离开搜索页，需要后续 go back）
    if job_id:
        try:
            page.goto(f"https://www.linkedin.com/jobs/view/{job_id}/", wait_until="domcontentloaded", timeout=10000)
            page.wait_for_timeout(2000)
            return True
        except Exception:
            pass

    logger.warning(f"Could not open detail for: {job.get('title', 'Unknown')}")
    return False


def extract_jd_text(page: Page) -> str:
    """从岗位详情页提取完整 JD 文本。

    使用多种策略：选择器 → 标题锚点 → 全页回退
    """
    # 策略 1: LinkedIn 经典 JD 选择器
    jd_selectors = [
        ".jobs-description__content",
        ".jobs-description",
        ".jobs-box__html-content",
        "#job-details",
        '[class*="description"]',
        ".jobs-unified-top-card__description-container",
        "article.jobs-description",
        ".job-details-jobs-unified-top-card__description-container",
    ]

    for selector in jd_selectors:
        el = page.query_selector(selector)
        if el:
            text = el.inner_text().strip()
            if len(text) > 100:
                logger.info(f"JD found via selector '{selector}': {len(text)} chars")
                return text

    # 策略 2: 通过多语言标题锚点定位 JD 内容区域
    # LinkedIn 新版 UI 的 JD 标题
    jd_headings = [
        "about the job", "about this job",
        "关于职位", "职位描述",
        "over de functie", "functieomschrijving",
        "job description", "omschrijving",
    ]

    body = page.inner_text("body")
    best_start = -1
    for heading in jd_headings:
        idx = body.lower().find(heading)
        if idx >= 0 and (best_start < 0 or idx < best_start):
            best_start = idx

    if best_start >= 0:
        # 从标题后提取内容（到下一个主要标题或页尾）
        jd_section = body[best_start:]
        # 截断在 "… more" / "更多" / "Show more" / 下一个明确的分隔
        cut_markers = [
            "\n… more", "\n更多", "\nShow more",
            "\nSeniority level", "\nEmployment type",
            "\nJob function", "\nIndustries",
            "\n订阅相似职位", "\nSimilar jobs",
            "\nMeet the hiring team", "\nAbout the company",
        ]
        for marker in cut_markers:
            cut_idx = jd_section.find(marker)
            if cut_idx > 100:  # 确保有足够的 JD 内容
                jd_section = jd_section[:cut_idx]
                break

        jd_section = jd_section.strip()
        if len(jd_section) > 100:
            logger.info(f"JD found via heading anchor: {len(jd_section)} chars")
            return jd_section

    # 策略 3: 全页回退 — 提取 body 中 JD 相关的核心部分
    logger.warning("Could not extract structured JD — using body fallback")
    if len(body) > 5000:
        # 尝试提取中间部分（JD 通常在页面中间）
        body = body[2000:7000]
    logger.info(f"JD fallback: {len(body)} chars")
    return body if len(body) > 100 else ""


def extract_job_info(page: Page) -> dict:
    """从岗位详情页提取元信息（公司、地点、薪资、职位级别等）。

    Args:
        page: Page 对象（应在岗位详情页）

    Returns:
        岗位元信息字典
    """
    info: dict = {
        "company": "",
        "location": "",
        "seniority_level": "",
        "employment_type": "",
        "salary": "",
        "posted_date": "",
        "applicant_count": "",
    }

    try:
        # 公司名称
        company_el = page.query_selector(
            ".jobs-unified-top-card__company-name, "
            ".job-details-jobs-unified-top-card__company-name, "
            "a.jobs-unified-top-card__company-name"
        )
        if company_el:
            info["company"] = company_el.inner_text().strip()

        # 地点
        location_el = page.query_selector(
            ".jobs-unified-top-card__bullet, "
            ".jobs-unified-top-card__location"
        )
        if location_el:
            info["location"] = location_el.inner_text().strip()

        # 薪资
        salary_el = page.query_selector('[class*="salary"], [class*="compensation"]')
        if salary_el:
            info["salary"] = salary_el.inner_text().strip()

        # 收集所有列表项（经验级别、工作类型等）
        subtitle_els = page.query_selector_all(
            ".jobs-unified-top-card__job-insight, "
            ".job-details-jobs-unified-top-card__job-insight, "
            ".jobs-unified-top-card__subtitle-secondary-grouping span"
        )
        metadata_texts = []
        for el in subtitle_els:
            t = el.inner_text().strip()
            if t:
                metadata_texts.append(t)

        full_meta = " ".join(metadata_texts)
        if "Entry" in full_meta or "Mid-Senior" in full_meta or "Associate" in full_meta:
            info["seniority_level"] = full_meta
        if "Full-time" in full_meta or "Part-time" in full_meta or "Contract" in full_meta:
            info["employment_type"] += full_meta

        # 发布日期
        date_el = page.query_selector('[class*="posted"], [class*="time-ago"]')
        if date_el:
            info["posted_date"] = date_el.inner_text().strip()

        # 申请人数
        applicant_el = page.query_selector('[class*="applicant"], [class*="num-applicants"]')
        if applicant_el:
            info["applicant_count"] = applicant_el.inner_text().strip()

    except Exception as e:
        logger.debug(f"Error extracting job info: {e}")

    return info


def get_full_job_context(page: Page, job: dict, already_open: bool = False) -> dict:
    """打开岗位详情并获取完整上下文（JD 文本 + 元信息）。

    Args:
        page: Page 对象
        job: 岗位卡片数据

    Returns:
        job 数据扩展后的字典，包含 jd_text 和 info
    """
    if not already_open and not open_job_detail(page, job):
        return {**job, "jd_text": "", "info": {}}

    jd_text = extract_jd_text(page)
    info = extract_job_info(page)

    return {
        **job,
        "jd_text": jd_text,
        "info": info,
    }
