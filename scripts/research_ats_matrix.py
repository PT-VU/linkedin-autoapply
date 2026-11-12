"""Controlled ATS form probe for research.

This script deliberately stops before the final submit action.  It records
form structure, required-field status, upload evidence, CAPTCHA signals and
same-host application-looking traffic without capturing field values or POST
bodies.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


DEFAULT_CASES = {
    "recruitee": "https://lleverage.recruitee.com/o/forward-deployed-ai-engineer-1/c/new",
    "greenhouse": "https://job-boards.eu.greenhouse.io/dwelly/jobs/4954106101?gh_src=i97wjhb2teu",
    "lever": "https://jobs.lever.co/mendix/3be1cbcf-1ab4-4309-a764-2c336d1f7f37/apply?source=LinkedIn",
    "teamtailor": "https://eworgmbh.teamtailor.com/jobs/8123878-agentic-ai-co-founder-coo-100-remote-m-f-d",
}


def visible(locator) -> bool:
    try:
        return locator.is_visible()
    except Exception:
        return False


def text_or_empty(locator) -> str:
    try:
        return (locator.inner_text(timeout=500) or "").strip()
    except Exception:
        return ""


def label_for(page: Page, element) -> str:
    return element.evaluate(
        """el => {
          const labelled = el.getAttribute('aria-label');
          if (labelled) return labelled.trim();
          const placeholder = el.getAttribute('placeholder');
          if (placeholder) return placeholder.trim();
          const id = el.id;
          if (id) {
            const label = document.querySelector(`label[for="${CSS.escape(id)}"]`);
            if (label && label.innerText) return label.innerText.trim();
          }
          const parent = el.closest('label');
          if (parent && parent.innerText) return parent.innerText.trim();
          return (el.getAttribute('name') || el.id || el.tagName || '').trim();
        }""",
    )


def open_apply_page(page: Page, url: str) -> dict[str, Any]:
    result: dict[str, Any] = {"input_url": url, "navigation_error": ""}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(2500)
    except Exception as exc:
        result["navigation_error"] = type(exc).__name__ + ": " + str(exc)[:240]
        return result

    # Job pages commonly expose a link/button named Apply.  Never click a
    # submit control on a form; only use this before a form is present.
    form_count = page.locator("form").count()
    if form_count == 0 and not re.search(r"/(apply|c/new)(?:[/?#]|$)", page.url, re.I):
        candidates = page.get_by_role("link", name=re.compile(r"apply", re.I))
        if candidates.count() == 0:
            candidates = page.get_by_role("button", name=re.compile(r"apply", re.I))
        for idx in range(min(candidates.count(), 5)):
            candidate = candidates.nth(idx)
            if visible(candidate):
                try:
                    candidate.click(timeout=3000)
                    page.wait_for_timeout(2500)
                    break
                except Exception:
                    continue
    result["final_url"] = page.url
    result["title"] = page.title()
    return result


def field_inventory(page: Page) -> dict[str, Any]:
    fields: list[dict[str, Any]] = []
    for element in page.locator("input, textarea, select").all():
        try:
            typ = (element.get_attribute("type") or element.evaluate("el => el.tagName.toLowerCase()")).lower()
            fields.append(
                {
                    "tag": element.evaluate("el => el.tagName.toLowerCase()"),
                    "type": typ,
                    "label": label_for(page, element)[:180],
                    "name": (element.get_attribute("name") or "")[:140],
                    "required": bool(element.get_attribute("required")),
                    "disabled": element.is_disabled(),
                    "visible": visible(element),
                    "value_present": bool(element.input_value() if typ not in {"file", "checkbox", "radio"} else ""),
                    "file_count": len(element.input_value().splitlines()) if typ == "file" else None,
                }
            )
        except Exception:
            continue
    return {
        "fields": fields,
        "field_count": len(fields),
        "visible_field_count": sum(1 for field in fields if field["visible"]),
        "required_count": sum(1 for field in fields if field["required"]),
        "file_inputs": sum(1 for field in fields if field["type"] == "file"),
        "select_count": sum(1 for field in fields if field["tag"] == "select"),
        "custom_combobox_count": page.locator('[role="combobox"], [aria-haspopup="listbox"]').count(),
        "contenteditable_count": page.locator('[contenteditable="true"]').count(),
        "dom_counts": page.evaluate(
            "() => ({inputs: document.querySelectorAll('input').length, textareas: document.querySelectorAll('textarea').length, selects: document.querySelectorAll('select').length, forms: document.querySelectorAll('form').length, contenteditables: document.querySelectorAll('[contenteditable=\"true\"]').length})"
        ),
    }


def fill_safe_fields(page: Page, resume_path: Path | None) -> dict[str, Any]:
    filled: list[str] = []
    selected: list[str] = []
    uploads: list[dict[str, Any]] = []
    # The values are smoke-test values only.  We do not submit them.
    for element in page.locator("input, textarea, select").all():
        try:
            if element.is_disabled():
                continue
            tag = element.evaluate("el => el.tagName.toLowerCase()")
            typ = (element.get_attribute("type") or tag).lower()
            label = label_for(page, element)[:180]
            if typ == "file":
                if resume_path and resume_path.is_file():
                    element.set_input_files(str(resume_path))
                    uploads.append({"label": label, "file_name": resume_path.name, "file_count": 1})
                continue
            if not visible(element):
                continue
            if typ == "radio":
                continue
            if typ in {"hidden", "submit", "button", "image", "reset", "checkbox"}:
                continue
            if tag == "select":
                options = element.locator("option").all()
                chosen = None
                for option in options:
                    value = option.get_attribute("value") or ""
                    if value and not option.is_disabled():
                        chosen = value
                        break
                if chosen:
                    element.select_option(chosen)
                    selected.append(label)
                continue
            lower = label.lower()
            if "email" in lower:
                value = "qa.ats.probe@example.com"
            elif "phone" in lower or "mobile" in lower or "telephone" in lower:
                value = "+31600000000"
            elif "name" in lower:
                value = "QA ATS Probe"
            elif tag == "textarea":
                value = "Controlled ATS matrix probe; not submitted."
            else:
                value = "QA smoke test"
            element.fill(value)
            filled.append(label)
        except Exception:
            continue

    # Select one deterministic option per required radio group.  This only
    # makes the pre-submit form structurally complete; it never clicks submit.
    radio_groups: dict[str, list[Any]] = {}
    for radio in page.locator('input[type="radio"]').all():
        try:
            group = radio.get_attribute("name") or radio.get_attribute("id") or "radio-group"
            radio_groups.setdefault(group, []).append(radio)
        except Exception:
            continue
    for group, radios in radio_groups.items():
        try:
            if any(radio.is_checked() for radio in radios):
                continue
            if any(radio.get_attribute("required") is not None for radio in radios):
                radios[0].check(timeout=1500)
                selected.append(label_for(page, radios[0])[:180])
        except Exception:
            continue

    for element in page.locator('[contenteditable="true"]').all():
        try:
            if visible(element) and not element.get_attribute("aria-disabled"):
                element.fill("Controlled ATS matrix probe; not submitted.")
                filled.append(label_for(page, element)[:180])
        except Exception:
            continue

    # For custom listboxes, open an empty combobox and select the first
    # visible option.  This is a measurement of the control, not a submit.
    custom_selected: list[str] = []
    for combo in page.locator('[role="combobox"], [aria-haspopup="listbox"]').all():
        try:
            if visible(combo) and not (combo.input_value() if combo.evaluate("el => 'value' in el") else ""):
                combo.click(timeout=1500)
                page.wait_for_timeout(250)
                options = page.locator('[role="option"]:visible')
                if options.count():
                    options.first.click(timeout=1500)
                    custom_selected.append(label_for(page, combo)[:180])
        except Exception:
            continue
    return {"filled_labels": filled, "selected_labels": selected, "custom_selected": custom_selected, "uploads": uploads}


def status_snapshot(page: Page) -> dict[str, Any]:
    required: list[dict[str, Any]] = []
    for element in page.locator("input[required], textarea[required], select[required]").all():
        try:
            if not visible(element):
                continue
            typ = (element.get_attribute("type") or "").lower()
            valid = bool(element.evaluate("el => el.checkValidity()"))
            file_count = None
            if typ == "file":
                file_count = element.evaluate("el => el.files ? el.files.length : 0")
                valid = bool(file_count)
            required.append(
                {
                    "label": label_for(page, element)[:180],
                    "type": typ,
                    "id": (element.get_attribute("id") or "")[:120],
                    "name": (element.get_attribute("name") or "")[:120],
                    "valid": valid,
                    "file_count": file_count,
                }
            )
        except Exception:
            continue

    form_validity = []
    for form in page.locator("form").all():
        try:
            form_validity.append(bool(form.evaluate("el => el.checkValidity()")))
        except Exception:
            continue

    buttons = []
    for button in page.locator("button, input[type=submit]").all():
        try:
            if visible(button):
                buttons.append(
                    {
                        "text": text_or_empty(button)[:120],
                        "type": button.get_attribute("type") or "button",
                        "disabled": button.is_disabled(),
                    }
                )
        except Exception:
            continue
    body = page.locator("body").inner_text(timeout=3000)[:12000]
    captcha_frames = []
    for frame in page.locator("iframe").all():
        try:
            src = frame.get_attribute("src") or ""
            if re.search(r"captcha|hcaptcha|recaptcha", src, re.I):
                captcha_frames.append(src[:300])
        except Exception:
            continue
    challenge_text = bool(re.search(r"captcha|verify you are human|security check|challenge", body, re.I))
    same_host = page.url.split("/", 3)[2] if "://" in page.url else ""
    forms = []
    for form in page.locator("form").all():
        try:
            forms.append(
                {
                    "action": form.get_attribute("action") or "",
                    "method": form.get_attribute("method") or "get",
                    "enctype": form.get_attribute("enctype") or "",
                    "elements": form.locator("input,textarea,select,button").count(),
                }
            )
        except Exception:
            continue
    return {
        "url": page.url,
        "title": page.title(),
        "required": required,
        "missing_required": [item["label"] for item in required if not item["valid"]],
        "form_validity": form_validity,
        "has_invalid_form": any(value is False for value in form_validity),
        "buttons": buttons,
        "application_submitters": [
            button for button in buttons if re.search(r"submit application|submit|send|apply", button["text"], re.I)
        ],
        "forms": forms,
        "captcha_frames": captcha_frames,
        "challenge_text": challenge_text,
        "step_text": [line.strip() for line in body.splitlines() if re.search(r"step|page \d|\d+%|next|continue", line, re.I)][:20],
        "body_signals": [line.strip() for line in body.splitlines() if re.search(r"captcha|recaptcha|hcaptcha|verify|application|resume|cv", line, re.I)][:30],
        "same_host": same_host,
    }


def probe(browser: Browser, name: str, url: str, resume_path: Path | None) -> dict[str, Any]:
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, locale="en-US", accept_downloads=True)
    page = context.new_page()
    requests: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    page.on("request", lambda request: requests.append({"method": request.method, "url": request.url[:400]}))
    page.on("response", lambda response: responses.append({"status": response.status, "url": response.url[:400]}))
    result: dict[str, Any] = {"case": name, "url": url, "test": "no_final_submit"}
    try:
        result["open"] = open_apply_page(page, url)
        if result["open"].get("navigation_error"):
            return result
        result["before"] = status_snapshot(page)
        result["inventory"] = field_inventory(page)
        result["fill"] = fill_safe_fields(page, resume_path)
        page.wait_for_timeout(1200)
        result["after"] = status_snapshot(page)
        result["ready_to_submit"] = (
            not result["after"]["missing_required"]
            and not result["after"].get("has_invalid_form", False)
            and bool(result["after"].get("application_submitters"))
        )
        same_host = result["after"].get("same_host", "")
        result["application_like_requests"] = [
            item for item in requests if same_host and same_host in item["url"] and re.search(r"apply|application|candidate|submit|upload", item["url"], re.I)
        ][-30:]
        result["response_summary"] = [
            item for item in responses if same_host and same_host in item["url"] and re.search(r"apply|application|candidate|submit|upload", item["url"], re.I)
        ][-30:]
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)[:300]
    finally:
        context.close()
    return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append", help="name=url; may be repeated")
    parser.add_argument("--resume", default="")
    args = parser.parse_args()
    cases = dict(DEFAULT_CASES)
    if args.case:
        cases = {}
        for item in args.case:
            name, sep, url = item.partition("=")
            if not sep or not name or not url:
                parser.error("--case must be name=url")
            cases[name] = url
    resume = Path(args.resume)
    if not resume.is_absolute():
        resume = Path.cwd() / resume

    with sync_playwright() as playwright:
        browser = None
        launch_error = ""
        try:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
        except Exception as exc:
            launch_error = type(exc).__name__ + ": " + str(exc)[:300]
            browser = playwright.chromium.launch(headless=True)
        all_results = {name: probe(browser, name, url, resume) for name, url in cases.items()}
        browser.close()
    output = {"run": "controlled_ats_matrix_probe", "resume_file_present": resume.is_file(), "launch_fallback": launch_error, "results": all_results}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
