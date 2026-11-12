"""
Glassdoor 搜索 + JD 收集模块。
Glassdoor DOM (2026-06): 卡片使用 div.JobCard_jobCardWrapper__vX29z,
标题链接 a.JobCard_jobTitle__GLyJ1, JD 在 [class*="jobDescription"]。
卡片文本行序: 公司名 → 评分 → 标题 → 地点 → 薪资 → Apply类型。
"""
import hashlib
import logging
import time
from typing import List, Dict
from playwright.sync_api import Page

logger = logging.getLogger("glassdoor")

KEYWORDS = ["AI Engineer", "Software Engineer", "Data Engineer", "Cloud Engineer", "Agentic AI"]


def build_search_url(keyword: str) -> str:
    """构建 Glassdoor 搜索结果 URL — 直接导航，无需搜索表单。
    正确格式: /Vacature/{keyword}-vacatures-SRCH_KO0,{N}.htm
    """
    encoded = keyword.replace(" ", "-").lower()
    # "AI Engineer" → "ai-engineer" (11 chars), KO12 = offset
    return f"https://www.glassdoor.nl/Vacature/{encoded}-vacatures-SRCH_KO0,{len(encoded)}.htm"


def execute_search(page: Page, keyword: str) -> bool:
    """在 Glassdoor 首页搜索框输入关键词并搜索。返回 True 如果成功。"""
    try:
        page.wait_for_timeout(2000)
        search_input = None
        for sel in ['input[id*="search"]', 'input[name="q"]',
                     'input[placeholder*="Zoek"]', 'input[placeholder*="Job"]',
                     'input[aria-label*="Search"]', 'input[type="text"]:not([data-test*="button"])']:
            search_input = page.query_selector(sel)
            if search_input and search_input.is_visible():
                # 验证是真正的输入框(不是按钮)
                tag = search_input.evaluate("el => el.tagName")
                itype = search_input.get_attribute("type") or ""
                if tag == "INPUT" and itype != "submit" and itype != "button":
                    break
                search_input = None
        if not search_input:
            logger.warning("No search input on Glassdoor page")
            return False
        search_input.click()
        search_input.fill("")
        search_input.type(keyword, delay=50)
        page.wait_for_timeout(500)
        search_input.press("Enter")
        page.wait_for_timeout(5000)
        try: page.wait_for_load_state("networkidle", timeout=10000)
        except: pass
        page.wait_for_timeout(3000)
        logger.info(f"Glassdoor search: '{keyword}' → {page.url[:100]}")
        return True
    except Exception as e:
        logger.error(f"Glassdoor search error: {e}")
        return False


def collect_job_cards(page: Page, keyword: str) -> List[Dict]:
    """在 Glassdoor 搜索结果页收集岗位卡片。"""
    jobs = []
    try:
        # 确保我们在正确的页面
        page.bring_to_front()
        page.wait_for_timeout(2000)

        # 跳过 Glassdoor paywall (如果存在)
        page.evaluate('''() => {
            document.querySelectorAll('div').forEach(el => {
                const t = el.innerText || '';
                if ((t.includes('toegang') || t.includes('content')) &&
                    t.length < 300 && el.offsetWidth > 300) {
                    el.style.display = 'none';
                }
            });
            document.body.style.overflow = 'auto';
        }''')
        # 点击"跳过"按钮 (技能提问弹窗)
        for btn in page.query_selector_all('button'):
            try:
                if btn.is_visible() and 'overslaan' in (btn.inner_text() or '').lower():
                    btn.click(); break
            except: pass
        page.wait_for_timeout(1000)

        # 滚动以触发懒加载
        page.evaluate("window.scrollTo(0, document.body.scrollHeight/2)")
        page.wait_for_timeout(2000)

        # 通过标题链接定位卡片 — 这是最可靠的选择器
        title_links = page.query_selector_all("a.JobCard_jobTitle__GLyJ1")
        logger.info(f"[DEBUG] a.JobCard: {len(title_links)} links")
        if not title_links:
            title_links = page.query_selector_all("a[data-test='job-link'], a[href*='job-listing']")
            logger.info(f"[DEBUG] fallback: {len(title_links)} links")
        if not title_links:
            # Last resort: check what links exist
            all_links = page.query_selector_all("a")
            job_links = [a for a in all_links if a.is_visible() and 'job-listing' in (a.get_attribute('href') or '')]
            logger.info(f"[DEBUG] all a: {len(all_links)}, job-listing: {len(job_links)}")
            title_links = job_links
        logger.info(f"Found {len(title_links)} job title links")

        seen_urls = set()
        skip_no_jl = 0; skip_dup = 0; skip_other = 0
        for link in title_links[:20]:
            try:
                href = link.get_attribute("href") or ""
                if not href or "job-listing" not in href:
                    skip_no_jl += 1
                    continue
                if not href.startswith("http"):
                    href = "https://www.glassdoor.nl" + href
                if href in seen_urls:
                    skip_dup += 1
                    continue
                seen_urls.add(href)

                # Filter out non-apply links (salary estimates, company overviews, etc.)
                EXCLUDE_URL_PATTERNS = [
                    "salaris", "salary", "bedrijf/overzicht", "company/overview",
                    "waarom", "why-work", "foto", "photo", "interview", "sollicitatievragen",
                ]
                href_lower = (href or "").lower()
                if any(p in href_lower for p in EXCLUDE_URL_PATTERNS):
                    continue

                title = link.inner_text().strip()
                if not title or len(title) < 5:
                    continue

                # 从父级卡片提取其他信息 (直接用 page.evaluate, 避免 JSHandle 链)
                try:
                    card_text = link.evaluate("""el => {
                        const card = el.closest('div[class*=\"JobCard\"]');
                        return card ? card.innerText : '';
                    }""")
                except Exception:
                    card_text = ""

                lines = [l.strip() for l in (card_text or "").split("\n") if l.strip()]

                # 解析: lines[0]=公司, lines[1]=评分, lines[2]=标题
                company = lines[0] if len(lines) > 0 and lines[0] != title else ""
                # 跳过评分行（纯数字+逗号, 如 "3,9"）
                rating_idx = 1 if (len(lines) > 1 and _looks_like_rating(lines[1])) else -1
                loc_idx = 2 if rating_idx > 0 else 1

                location = ""
                salary = ""
                is_quick_apply = False

                for line in lines:
                    l = line.lower()
                    if any(city in l for city in ["amsterdam", "rotterdam", "utrecht",
                                                    "eindhoven", "nederland", "netherlands",
                                                    "den haag", "the hague", "remote"]):
                        if not location:
                            location = line.strip()
                    if "€" in line or "$" in line:
                        salary = line.strip()
                    if "snelsollicitatie" in l:
                        is_quick_apply = True

                job_id = f"gd_{hashlib.md5(href.encode()).hexdigest()[:16]}"

                jobs.append({
                    "job_id": job_id,
                    "title": title[:100],
                    "company": company[:50],
                    "location": location[:80],
                    "salary": salary[:50],
                    "url": href[:300],
                    "is_quick_apply": is_quick_apply,
                    "source": "glassdoor",
                })
            except Exception as e:
                logger.warning(f"Card parse error: {e}")
                skip_other += 1
                continue

        logger.info(f"Collected {len(jobs)} job cards for '{keyword}' (skipped: {skip_no_jl} no-job-listing, {skip_dup} dup, {skip_other} other)")
    except Exception as e:
        logger.error(f"Collect error: {e}")

    return jobs


def _looks_like_rating(text: str) -> bool:
    """检查文本是否像评分 (e.g. '3,9', '4.1')。"""
    import re
    return bool(re.match(r'^[\d,.]+$', text.strip())) and len(text.strip()) <= 5


def open_job_detail(page: Page, job: Dict) -> bool:
    """通过 URL 打开岗位详情页，提取 JD。

    直接导航到 job URL（避免点击 overlay 冲突）。
    """
    try:
        url = job.get("url", "")
        if not url:
            return False

        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        jd_text = extract_jd_text(page)
        if jd_text and len(jd_text) > 100:
            job["jd_text"] = jd_text
            return True

        logger.warning(f"JD too short for: {job.get('title', '?')[:60]}")
        return False
    except Exception as e:
        logger.error(f"Open detail error: {e}")
        return False


def extract_jd_text(page: Page) -> str:
    """从 Glassdoor 页面提取 JD 文本。"""
    selectors = [
        "[class*='jobDescription']",
        "[class*='JobDetails']",
        "[class*='description']",
        "[class*='job-details']",
        "article",
    ]
    for sel in selectors:
        el = page.query_selector(sel)
        if el:
            try:
                text = el.inner_text()
                if len(text.strip()) > 100:
                    return text.strip()[:5000]
            except Exception:
                continue
    try:
        return page.inner_text("body")[:5000]
    except Exception:
        return ""


def is_quick_apply_available(page: Page) -> bool:
    """检测当前 Glassdoor 详情页是否有 Snelsollicitatie 按钮 (P1-4: 扩展文本)。"""
    QUICK_APPLY_TEXTS = [
        "snelsollicitatie", "snel solliciteren", "direct solliciteren",
        "easy apply", "quick apply", "instant apply", "1-click apply",
        "solliciteer direct", "solliciteer nu", "direct apply",
    ]
    for btn in page.query_selector_all("button, a"):
        try:
            if not btn.is_visible(): continue
            txt = (btn.inner_text() or "").strip().lower()
            if any(kw in txt for kw in QUICK_APPLY_TEXTS):
                return True
        except Exception:
            continue
    return False


def is_external_apply(page: Page) -> bool:
    """检测是否有跳转到外部网站的申请链接 (P1-4: 扩展检测)。"""
    EXTERNAL_KEYWORDS = [
        "solliciteren op site", "apply on company", "apply externally",
        "solliciteer op de website", "op de website van",
        "solliciteren via", "apply through", "apply now",
        "apply on website", "external apply", "company website",
        "solliciteren op werkgeverssite",
    ]
    for el in page.query_selector_all("a, button"):
        try:
            if not el.is_visible():
                continue
            txt = (el.inner_text() or "").strip().lower()
            # 检查按钮/链接文本
            if any(kw in txt for kw in EXTERNAL_KEYWORDS):
                return True
            # 检查 <a> 标签的 href
            href = (el.get_attribute("href") or "").lower()
            if href and ("apply" in txt or "solliciteer" in txt) and \
               href.startswith("http") and "glassdoor" not in href:
                return True
        except Exception:
            continue
    return False
