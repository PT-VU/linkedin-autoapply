"""Verify an external application after a human completes a visible challenge.

This script intentionally does not click CAPTCHA controls or submit the form. It
only attaches to the retained Edge session, waits for a normal employer receipt,
and writes a confirmed result when one is visible.
"""
import argparse
import json
import time
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

RECEIPTS=(
    "thank you for applying", "we have received your application",
    "thanks for applying", "application submitted", "application received",
    "your application has been submitted", "your application has been received",
    "application successfully submitted", "sollicitatie ontvangen",
    "bedankt voor je sollicitatie", "bedankt voor uw sollicitatie",
    "bewerbung eingegangen",
)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("run_dir", help="external_agent_runs/<case>_<timestamp>")
    ap.add_argument("--endpoint", default="http://127.0.0.1:9227")
    ap.add_argument("--timeout", type=int, default=120)
    args=ap.parse_args()
    run=Path(args.run_dir).resolve()
    result_path=run/"result.json"
    if not run.is_dir(): raise SystemExit(f"Run directory not found: {run}")
    with sync_playwright() as pw:
        browser=pw.chromium.connect_over_cdp(args.endpoint,timeout=10000)
        page=next((p for c in browser.contexts for p in c.pages if p.url and "/c/" in p.url),None)
        if page is None: raise SystemExit("No retained application page found")
        deadline=time.monotonic()+max(1,args.timeout)
        while time.monotonic()<deadline:
            body=page.locator("body").inner_text(timeout=5000).casefold()
            found=next((x for x in RECEIPTS if x in body),"")
            if found:
                data={"status":"submitted","reason":"Employer receipt verified after human handoff","receipt":found,"url":page.url,"backend":"edge","handoff_required":False}
                result_path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
                key_file=run/"case_key.txt"
                case_key=key_file.read_text(encoding="utf-8").strip() if key_file.exists() else run.name.rsplit("_",2)[0]
                cases_path=ROOT/"data"/"external_regression_cases.json"
                if cases_path.exists():
                    cases={x["key"]:x for x in json.loads(cases_path.read_text(encoding="utf-8"))}
                    case=cases.get(case_key)
                    if case:
                        from src.storage import is_already_applied
                        from src.tracker import log_application
                        if not is_already_applied(case["job_id"]):
                            log_application(job_id=case["job_id"],company=case["company"],title=case["title"],match_score=case["score"],apply_method="external",cover_letter_status="not applicable")
                print(f"Confirmed submitted; receipt={found}; result={result_path}")
                return 0
            print(f"Waiting for receipt: url={page.url} hcaptcha_frames={page.locator('iframe[src*="captcha" i]').count()}",flush=True)
            time.sleep(5)
        data={"status":"unconfirmed","reason":"No employer receipt observed during handoff verification","url":page.url,"backend":"edge","handoff_required":True}
        result_path.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
        print(f"Not confirmed; result={result_path}")
        return 1

if __name__=="__main__": raise SystemExit(main())
