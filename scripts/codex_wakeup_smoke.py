#!/usr/bin/env python3
"""
Minimal Codex wake-up smoke test.

It proves that a running process can:
1. wait for a local flag,
2. read the incident payload,
3. pass that payload to `codex exec`,
4. persist Codex's response for the automation layer.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_DIR = PROJECT_ROOT / "data" / "codex_smoke"
INCIDENT_FILE = RUN_DIR / "incident.json"
FLAG_FILE = RUN_DIR / "codex_needed.flag"
RESULT_FILE = RUN_DIR / "codex_result.md"
STDOUT_FILE = RUN_DIR / "codex_stdout.log"
SCHEMA_FILE = PROJECT_ROOT / "scripts" / "codex_wakeup_response.schema.json"
CODEX_MODEL = ""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_incident() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    incident = {
        "event_id": f"smoke-{int(time.time())}",
        "created_at": now_iso(),
        "source": "codex_wakeup_smoke.py",
        "platform": "smoke",
        "kind": "form_help_needed",
        "problem": {
            "summary": "A fake running auto-apply process is blocked on one missing form field.",
            "form_state": {
                "url": "https://example.test/jobs/123/apply",
                "required_unfilled": [
                    {
                        "type": "input",
                        "label": "How many years of Python experience do you have?",
                        "value": "",
                    }
                ],
                "buttons": ["Next"],
            },
            "expected_output": "Return a compact machine-readable recommendation.",
        },
    }
    INCIDENT_FILE.write_text(json.dumps(incident, ensure_ascii=False, indent=2), encoding="utf-8")
    FLAG_FILE.write_text(now_iso(), encoding="utf-8")


def build_prompt(incident: dict) -> str:
    return (
        "You were invoked by a running local automation supervisor after it detected a flag file.\n"
        "This is a smoke test. Do not modify files or run commands.\n"
        "Read the incident JSON and respond with exactly one JSON object containing:\n"
        '- "wakeup_received": true\n'
        '- "event_id": the input event_id\n'
        '- "recommended_actions": an array of form actions\n'
        '- "explanation": one short sentence\n\n'
        "Incident JSON:\n"
        f"{json.dumps(incident, ensure_ascii=False, indent=2)}\n"
    )


def invoke_codex(incident: dict) -> int:
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("codex CLI was not found on PATH")

    prompt = build_prompt(incident)
    cmd = [
        codex,
        "--ask-for-approval",
        "never",
        "--model",
        CODEX_MODEL,
        "exec",
        "-C",
        str(PROJECT_ROOT),
        "-s",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-rules",
        "--output-schema",
        str(SCHEMA_FILE),
        "--output-last-message",
        str(RESULT_FILE),
        "-",
    ]
    with STDOUT_FILE.open("w", encoding="utf-8") as out:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            input=prompt,
            stdout=out,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=180,
        )
    return proc.returncode


def watcher(done: threading.Event) -> None:
    print("[watcher] started; waiting for codex_needed.flag", flush=True)
    while not done.is_set():
        if FLAG_FILE.exists() and INCIDENT_FILE.exists():
            print("[watcher] flag detected; invoking codex exec", flush=True)
            incident = json.loads(INCIDENT_FILE.read_text(encoding="utf-8"))
            started = time.time()
            try:
                returncode = invoke_codex(incident)
                elapsed = time.time() - started
                print(f"[watcher] codex exited code={returncode} elapsed={elapsed:.1f}s", flush=True)
            finally:
                done.set()
            return
        time.sleep(0.5)


def main() -> int:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    for path in [INCIDENT_FILE, FLAG_FILE, RESULT_FILE, STDOUT_FILE]:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    done = threading.Event()
    thread = threading.Thread(target=watcher, args=(done,), daemon=True)
    thread.start()

    time.sleep(1)
    print("[main] writing incident.json and codex_needed.flag", flush=True)
    write_incident()

    thread.join(timeout=210)
    if thread.is_alive():
        done.set()
        print("[main] timeout waiting for watcher", flush=True)
        return 2

    if not RESULT_FILE.exists():
        print("[main] codex_result.md was not created", flush=True)
        return 3

    print("[main] result file:", RESULT_FILE, flush=True)
    print(RESULT_FILE.read_text(encoding="utf-8"), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
