#!/usr/bin/env python3
"""Run every configured LinkedIn keyword with one command."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CODEX_QUOTA_EXIT = 75
HUMAN_HANDOFF_EXIT = 74
CODEX_MODEL = ""


def load_search_config(config_path: Path) -> tuple[list[str], str]:
    with config_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}
    search = config.get("search", {})
    keywords = [str(keyword).strip() for keyword in search.get("keywords", []) if str(keyword).strip()]
    locations = search.get("locations") or [search.get("location", "")]
    return keywords, str(locations[0] or "")


def supervisor_is_running() -> bool:
    if os.name != "nt":
        return False
    command = "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'codex_supervisor.py' }).Count"
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() not in {"", "0"}


def start_supervisor(environment: dict[str, str]) -> subprocess.Popen[str] | None:
    if supervisor_is_running():
        print("[run-all] existing Codex supervisor detected; reusing it", flush=True)
        return None
    print("[run-all] starting Codex supervisor", flush=True)
    return subprocess.Popen(
        [
            sys.executable, "-X", "utf8", "scripts/codex_supervisor.py",
            "--sandbox", "workspace-write",
            "--config", str(environment.get("LINKEDIN_AUTOAPPLY_CONFIG", "config.yaml")),
        ],
        cwd=PROJECT_ROOT,
        env=environment,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run all configured LinkedIn keywords.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Maximum pages per keyword; 0 means all available pages",
    )
    parser.add_argument(
        "--start-index", type=int, default=0,
        help="Zero-based keyword index to resume from without replaying earlier keywords",
    )
    parser.add_argument("--location", default=None, help="Temporary location override for this run")
    parser.add_argument(
        "--date-posted",
        choices=["Any Time", "Past 24 hours", "Past Week", "Past 2 weeks", "Past Month"],
        default=None,
        help="Temporary date-window override for this run",
    )
    parser.add_argument("--no-supervisor", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Collect and score without submitting applications")
    args = parser.parse_args()
    if args.max_pages < 0:
        parser.error("--max-pages must be 0 or greater")

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    keywords, location = load_search_config(config_path)
    if args.location:
        location = args.location
    if not keywords:
        print("[run-all] no search keywords configured", flush=True)
        return 2
    start_index = max(0, int(args.start_index))
    if start_index >= len(keywords):
        print(f"[run-all] start index {start_index} is beyond the configured keyword list", flush=True)
        return 2

    environment = os.environ.copy()
    environment["LINKEDIN_AUTOAPPLY_AI_MODE"] = "codex"
    environment["LINKEDIN_AUTOAPPLY_CONFIG"] = str(config_path)
    supervisor = None if args.no_supervisor else start_supervisor(environment)
    failures: list[str] = []
    child: subprocess.Popen[str] | None = None
    try:
        for index, keyword in enumerate(keywords[start_index:], start_index + 1):
            print(f"[run-all] {index}/{len(keywords)}: {keyword}", flush=True)
            child_command = [
                sys.executable, "-X", "utf8", "main.py", "--run",
                "--keyword", keyword, "--location", location,
                "--page", "1", "--max-pages", str(args.max_pages),
                "--config", str(config_path),
            ]
            if args.date_posted is not None:
                child_command.extend(["--date-posted", args.date_posted])
            if args.dry_run:
                child_command.append("--dry-run")
            child = subprocess.Popen(
                child_command,
                cwd=PROJECT_ROOT,
                env=environment,
            )
            try:
                returncode = child.wait()
            except KeyboardInterrupt:
                print(f"[run-all] interrupt: stopping keyword child: {keyword}", flush=True)
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
                raise
            result = subprocess.CompletedProcess(child.args, returncode)
            child = None
            if result.returncode:
                failures.append(keyword)
                print(f"[run-all] keyword failed with exit {result.returncode}: {keyword}", flush=True)
                if result.returncode == CODEX_QUOTA_EXIT:
                    print(
                        "[run-all] STOPPED: Codex usage limit reached. "
                        "No further keywords will run; retry after the limit resets.",
                        flush=True,
                    )
                    return CODEX_QUOTA_EXIT
                if result.returncode == HUMAN_HANDOFF_EXIT:
                    print(
                        "[run-all] PAUSED: Easy Apply requires human intervention. "
                        "No further keywords will run until the handoff is completed.",
                        flush=True,
                    )
                    return HUMAN_HANDOFF_EXIT
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        if supervisor is not None and supervisor.poll() is None:
            supervisor.terminate()
            try:
                supervisor.wait(timeout=15)
            except subprocess.TimeoutExpired:
                supervisor.kill()
                supervisor.wait(timeout=5)

    if failures:
        print(f"[run-all] completed with failed keywords: {', '.join(failures)}", flush=True)
        return 1
    print(f"[run-all] completed all {len(keywords)} keywords", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
