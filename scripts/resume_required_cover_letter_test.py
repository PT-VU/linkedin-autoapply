from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import get_resume_path, load_config
from src.easy_apply import handle_easy_apply_flow


def main() -> int:
    config = load_config()
    profile = config.get("profile", {})
    resume_path = get_resume_path(config)
    job = {
        "job_id": "4474455477",
        "title": "Stagiair(e) AI Developer: bouw de MCP voor Upod",
        "company": "Utonomy",
        "link": "https://www.linkedin.com/jobs/view/4474455477/",
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")
        pages = [page for context in browser.contexts for page in context.pages]
        page = next((item for item in pages if "linkedin.com" in (item.url or "").lower()), None)
        if page is None:
            raise SystemExit("No LinkedIn Edge page is available")
        if page.locator("dialog").count() == 0:
            button = page.locator('button[aria-label*="Easy Apply" i]').first
            if not button.is_visible():
                raise SystemExit("Easy Apply button is not visible")
            button.click(force=True)
            page.wait_for_timeout(4000)
        ok = handle_easy_apply_flow(
            page,
            profile,
            str(resume_path),
            "",
            job_context=job,
            already_open=True,
        )
        print(f"REQUIRED_COVER_LETTER_TEST success={ok} url={page.url}")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
