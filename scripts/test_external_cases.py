#!/usr/bin/env python3
"""Exercise a historical external application directly, without LinkedIn search/scoring.

This performs a REAL application. A started case stays locked until the user
checks the employer receipt and explicitly clears it; this survives interruption.
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from main import connect_browser
from src.config import get_resume_path, load_config
from src.external_apply import handle_external_apply_flow
from src.logger import setup_logger
from src.storage import is_already_applied, save_for_manual_apply
from src.tracker import log_application
from scripts.run_all_keywords import start_supervisor

CASES = ROOT / 'data' / 'external_regression_cases.json'
LOCKS = ROOT / 'data' / 'external_regression_pending.json'


def clear_external_ai_artifacts() -> None:
    """A regression case must never consume instructions from an interrupted run."""
    folder = ROOT / 'data' / 'linkedin'
    for name in ('ext_help_needed.flag', 'ext_help_done.flag', 'ext_help.json', 'ext_instructions.json'):
        try:
            (folder / name).unlink()
        except FileNotFoundError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('case', nargs='?', help='case key; omit to list')
    parser.add_argument('--clear-verified', action='store_true', help='clear a lock only after checking no application was submitted')
    args = parser.parse_args()
    cases = {item['key']: item for item in json.loads(CASES.read_text(encoding='utf-8'))}
    if not args.case:
        for key, item in cases.items():
            print(f"{key}: {item['company']} | {item['title']} | {item['url']}")
        return 0
    if args.case not in cases:
        parser.error('Unknown case')
    case = cases[args.case]
    pending = json.loads(LOCKS.read_text(encoding='utf-8')) if LOCKS.exists() else {}
    if args.clear_verified:
        pending.pop(args.case, None)
        LOCKS.write_text(json.dumps(pending, indent=2), encoding='utf-8')
        print('Lock cleared. Check the employer receipt before retrying.')
        return 0
    if is_already_applied(case['job_id']):
        print('Already recorded as submitted; skipping to prevent a duplicate.')
        return 1
    if args.case in pending:
        print('Previous attempt interrupted or unresolved; check the employer before clearing its lock.')
        return 1
    config = load_config(None)
    setup_logger(level=config.get('logging', {}).get('level', 'INFO'))
    env = os.environ.copy()
    env['LINKEDIN_AUTOAPPLY_AI_MODE'] = 'codex'
    os.environ['LINKEDIN_AUTOAPPLY_AI_MODE'] = 'codex'
    clear_external_ai_artifacts()
    supervisor = start_supervisor(env)
    pw = browser = None
    try:
        pw, browser, context, page = connect_browser()
        page.goto(case['url'], wait_until='domcontentloaded', timeout=30000)
        pending[args.case] = {'url': case['url'], 'state': 'attempt_started'}
        LOCKS.write_text(json.dumps(pending, indent=2), encoding='utf-8')
        metadata = {}
        ok = handle_external_apply_flow(page, config.get('profile', {}), str(get_resume_path(config)), platform='linkedin', application_metadata=metadata)
        if ok:
            log_application(job_id=case['job_id'], company=case['company'], title=case['title'], match_score=case['score'], apply_method='external', cover_letter_status=metadata.get('cover_letter_status', 'not applicable'))
            pending[args.case]['state'] = 'submitted'
            LOCKS.write_text(json.dumps(pending, indent=2), encoding='utf-8')
            print('Confirmed submitted and recorded.')
            return 0
        save_for_manual_apply(case, metadata.get('handoff_reason', 'external_test_unconfirmed'), case['url'], score=case['score'])
        print('Not confirmed. Check employer status before retrying; case remains locked.')
        return 1
    finally:
        if browser:
            browser.close()
        if pw:
            pw.stop()
        if supervisor and supervisor.poll() is None:
            supervisor.terminate()
            supervisor.wait(timeout=15)


if __name__ == '__main__':
    raise SystemExit(main())
