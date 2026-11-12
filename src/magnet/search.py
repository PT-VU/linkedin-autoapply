"""
Magnet.me search + JD collection module.

Magnet is used as a non-LinkedIn source while the LinkedIn account is disabled.
The implementation keeps the channel conservative: collect public opportunity
pages, score them through the existing agent protocol, try external apply when
Magnet exposes an external application path, and otherwise save for manual
handling.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Dict, List, Optional
from urllib.parse import quote_plus

from playwright.sync_api import Page

logger = logging.getLogger("magnet")

BASE_URL = "https://magnet.me"
JOBS_URL = f"{BASE_URL}/en/jobs"


def build_search_url(keyword: str, location: str = "") -> str:
    """Return a readable fallback URL for Magnet search pages.

    Magnet currently applies most filters client-side, so the main flow still
    fills the search inputs after navigation. The query string is useful for
    logging and for future compatibility if Magnet starts honoring it.
    """
    parts = []
    if keyword:
        parts.append(f"q={quote_plus(keyword)}")
    if location:
        parts.append(f"location={quote_plus(location)}")
    return JOBS_URL + (("?" + "&".join(parts)) if parts else "")


def accept_cookies(page: Page) -> None:
    """Dismiss common Magnet cookie banners when present."""
    texts = [
        "accept all",
        "accept cookies",
        "allow all",
        "alles accepteren",
        "accepteer alles",
    ]
    for btn in page.query_selector_all("button"):
        try:
            if not btn.is_visible():
                continue
            txt = (btn.inner_text() or "").strip().lower()
            if any(t in txt for t in texts):
                btn.click()
                page.wait_for_timeout(800)
                return
        except Exception:
            continue


def execute_search(page: Page, keyword: str, location: str = "") -> bool:
    """Open Magnet jobs and apply keyword/location filters when possible."""
    try:
        url = build_search_url(keyword, location)
        logger.info(f"Navigating to: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2500)
        accept_cookies(page)

        keyword_input = _find_input(
            page,
            [
                "search by title",
                "keyword",
                "zoek op functie",
                "functie of trefwoord",
            ],
        )
        if keyword_input:
            keyword_input.click()
            keyword_input.fill(keyword)
            page.wait_for_timeout(400)
            keyword_input.press("Enter")
            page.wait_for_timeout(3000)
        else:
            logger.warning("Magnet keyword input not found; collecting from current jobs page")

        if location:
            location_input = _find_input(page, ["city or country", "location", "stad", "land"])
            if location_input:
                location_input.click()
                location_input.fill(location)
                page.wait_for_timeout(400)
                location_input.press("Enter")
                page.wait_for_timeout(3000)

        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        logger.info(f"Magnet search: '{keyword}' @ '{location or '-'}' -> {page.url[:120]}")
        return True
    except Exception as exc:
        logger.error(f"Magnet search error: {exc}")
        return False


def _find_input(page: Page, needles: List[str]):
    for inp in page.query_selector_all("input"):
        try:
            if not inp.is_visible():
                continue
            placeholder = (inp.get_attribute("placeholder") or "").lower()
            aria = (inp.get_attribute("aria-label") or "").lower()
            name = (inp.get_attribute("name") or "").lower()
            combined = f"{placeholder} {aria} {name}"
            if any(n in combined for n in needles):
                return inp
        except Exception:
            continue
    return None


def collect_job_cards(page: Page, keyword: str) -> List[Dict]:
    """Collect visible Magnet opportunity links from the current result page."""
    jobs: List[Dict] = []
    seen_urls = set()
    try:
        page.bring_to_front()
        for y in [500, 1200, 2200]:
            page.evaluate(f"window.scrollTo(0, {y})")
            page.wait_for_timeout(900)

        links = page.query_selector_all("a[href*='/opportunity/']")
        logger.info(f"Found {len(links)} Magnet opportunity links")

        for link in links[:30]:
            try:
                href = (link.get_attribute("href") or "").strip()
                if not href:
                    continue
                if href.startswith("/"):
                    href = BASE_URL + href
                if "/opportunity/" not in href or href in seen_urls:
                    continue
                seen_urls.add(href)

                title = (link.inner_text() or "").strip()
                if not title or len(title) < 4:
                    continue

                card_text = _extract_card_text(link)
                company = _guess_company(card_text, title)
                location = _guess_location(card_text)
                salary = _guess_salary(card_text)

                job_id = _job_id_from_url(href)
                jobs.append(
                    {
                        "job_id": job_id,
                        "title": title[:120],
                        "company": company[:80],
                        "location": location[:100],
                        "salary": salary[:80],
                        "url": href[:400],
                        "source": "magnet",
                        "card_text": card_text[:1500],
                    }
                )
            except Exception as exc:
                logger.warning(f"Magnet card parse error: {exc}")
                continue

        logger.info(f"Collected {len(jobs)} Magnet job cards for '{keyword}'")
    except Exception as exc:
        logger.error(f"Magnet collect error: {exc}")
    return jobs


def _extract_card_text(link) -> str:
    try:
        return link.evaluate(
            """el => {
                let node = el;
                for (let i = 0; i < 7 && node; i++) {
                    const text = (node.innerText || '').trim();
                    if (text.includes('Apply') || text.includes('Solliciteren') || text.length > 120) {
                        return text;
                    }
                    node = node.parentElement;
                }
                return el.innerText || '';
            }"""
        ) or ""
    except Exception:
        return ""


def _clean_lines(text: str) -> List[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _guess_company(card_text: str, title: str) -> str:
    lines = _clean_lines(card_text)
    bad = {
        "like",
        "apply",
        "solliciteren",
        "job",
        "internship",
        "traineeship",
        "image",
    }
    for idx, line in enumerate(lines):
        if line == title and idx > 0:
            for prev in reversed(lines[:idx]):
                low = prev.lower()
                if low in bad or "employees" in low or "medewerkers" in low or low == "•":
                    continue
                if len(prev) <= 80:
                    return prev
    return ""


def _guess_location(card_text: str) -> str:
    city_words = [
        "amsterdam",
        "rotterdam",
        "utrecht",
        "the hague",
        "den haag",
        "eindhoven",
        "paris",
        "netherlands",
        "nederland",
        "remote",
        "hybrid",
    ]
    for line in _clean_lines(card_text):
        low = line.lower()
        if any(w in low for w in city_words):
            return line[:100]
    return ""


def _guess_salary(card_text: str) -> str:
    for line in _clean_lines(card_text):
        if "€" in line or " per month" in line.lower() or " per year" in line.lower():
            return line[:100]
    return ""


def _job_id_from_url(url: str) -> str:
    match = re.search(r"/opportunity/(\d+)/", url)
    if match:
        return f"magnet_{match.group(1)}"
    return f"magnet_{hashlib.md5(url.encode()).hexdigest()[:16]}"


def open_job_detail(page: Page, job: Dict) -> bool:
    """Open a Magnet opportunity and extract full JD text + metadata."""
    url = job.get("url", "")
    if not url:
        return False
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(2500)
        accept_cookies(page)

        text = extract_jd_text(page)
        if len(text) > 100:
            job["jd_text"] = text[:6000]
            _merge_detail_metadata(page, job)
            return True
        logger.warning(f"Magnet JD too short for {job.get('title', '?')}")
        return False
    except Exception as exc:
        logger.error(f"Magnet open detail error: {exc}")
        return False


def extract_jd_text(page: Page) -> str:
    selectors = ["main", "article", "[class*='Opportunity']", "[class*='opportunity']", "body"]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if not el:
                continue
            text = (el.inner_text() or "").strip()
            if len(text) > 100:
                return text
        except Exception:
            continue
    return ""


def _merge_detail_metadata(page: Page, job: Dict) -> None:
    text = job.get("jd_text", "")
    lines = _clean_lines(text)
    if not job.get("title"):
        try:
            h1 = page.query_selector("h1")
            if h1:
                job["title"] = (h1.inner_text() or "").strip()[:120]
        except Exception:
            pass
    if not job.get("location"):
        job["location"] = _guess_location(text)
    if not job.get("salary"):
        job["salary"] = _guess_salary(text)
    if not job.get("company"):
        for idx, line in enumerate(lines[:25]):
            if line == job.get("title") and idx > 0:
                job["company"] = lines[idx - 1][:80]
                break


def has_apply_button(page: Page) -> bool:
    return _find_apply_control(page) is not None


def is_external_apply(page: Page) -> bool:
    """Return True for visible Magnet controls that imply leaving Magnet."""
    for el in page.query_selector_all("a, button"):
        try:
            if not el.is_visible():
                continue
            text = (el.inner_text() or "").strip().lower()
            href = (el.get_attribute("href") or "").strip().lower()
            if "apply on website" in text or "solliciteren op website" in text:
                return True
            if href.startswith("http") and "magnet.me" not in href and ("apply" in text or "solliciteer" in text):
                return True
        except Exception:
            continue
    return False


def click_apply(page: Page) -> Optional[Page]:
    """Click Magnet apply/external apply and return the target page if one opens."""
    control = _find_apply_control(page)
    if not control:
        return None

    try:
        href = (control.get_attribute("href") or "").strip()
        if href.startswith("http") and "magnet.me" not in href.lower():
            page.goto(href, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2500)
            return page
    except Exception:
        pass

    original_pages = list(page.context.pages)
    try:
        with page.expect_popup(timeout=7000) as popup_info:
            control.click()
        new_page = popup_info.value
        new_page.wait_for_load_state("domcontentloaded", timeout=12000)
        new_page.wait_for_timeout(2500)
        return new_page
    except Exception:
        pass

    try:
        before = page.url
        control.click()
        page.wait_for_timeout(3000)
        if page.url != before:
            return page
    except Exception:
        pass

    new_pages = [p for p in page.context.pages if p not in original_pages]
    if new_pages:
        try:
            new_pages[0].bring_to_front()
            new_pages[0].wait_for_load_state("domcontentloaded", timeout=12000)
            return new_pages[0]
        except Exception:
            return new_pages[0]
    return None


def _find_apply_control(page: Page):
    apply_texts = [
        "apply on website",
        "apply",
        "solliciteren op website",
        "solliciteren",
    ]
    for el in page.query_selector_all("a, button"):
        try:
            if not el.is_visible():
                continue
            text = (el.inner_text() or "").strip().lower()
            if text in {"like", "connect"}:
                continue
            if any(text == t or text.startswith(t) for t in apply_texts):
                return el
        except Exception:
            continue
    return None
