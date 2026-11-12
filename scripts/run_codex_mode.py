#!/usr/bin/env python3
"""
Start LinkedIn-AutoApply in Codex mode.

This starts the configured monitor channels and a Codex supervisor beside them.
Stop with Ctrl+C; both child processes will be terminated.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_LOCK = threading.Lock()
LOG_FILE: Path | None = None
DEFAULT_EDGE_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
DEFAULT_EDGE_PROFILE = r"C:\Temp\edge-cdp-profile"
CODEX_MODEL = ""


def log_line(message: str) -> None:
    print(message, flush=True)
    if LOG_FILE is None:
        return
    with LOG_LOCK:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(message + "\n")


def stream_output(name: str, proc: subprocess.Popen[str]) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        log_line(f"[{name}] {line.rstrip()}")


def start_process(name: str, cmd: list[str], env: dict[str, str] | None = None) -> subprocess.Popen[str]:
    log_line(f"[codex-mode] starting {name}: {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def cdp_is_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def ensure_cdp_browser(edge_path: str, debug_port: int, profile_dir: str) -> bool:
    if cdp_is_ready(debug_port):
        log_line(f"[codex-mode] browser CDP ready on port {debug_port}")
        return True

    edge = Path(edge_path)
    if not edge.exists():
        log_line(f"[codex-mode] Edge not found: {edge_path}")
        return False

    log_line(f"[codex-mode] launching Edge CDP on port {debug_port}")
    subprocess.Popen(
        [
            str(edge),
            f"--remote-debugging-port={debug_port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    for _ in range(20):
        time.sleep(1)
        if cdp_is_ready(debug_port):
            log_line(f"[codex-mode] browser CDP ready on port {debug_port}")
            return True
    log_line(f"[codex-mode] browser CDP did not become ready on port {debug_port}")
    return False


def main() -> int:
    global LOG_FILE
    parser = argparse.ArgumentParser(description="Run monitor_parallel_v3.py with Codex as the background AI.")
    parser.add_argument("--supervisor-only", action="store_true", help="Start only scripts/codex_supervisor.py.")
    parser.add_argument("--monitor-only", action="store_true", help="Start only monitor_parallel_v3.py.")
    parser.add_argument("--codex-sandbox", default="read-only", choices=["read-only", "workspace-write"])
    parser.add_argument(
        "--codex-model",
        default=CODEX_MODEL,
        help=f"Deprecated compatibility option; Codex is always forced to {CODEX_MODEL}.",
    )
    parser.add_argument("--codex-timeout", type=int, default=260)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--log-file", default="", help="Persistent Codex mode log file. Defaults to logs/codex_mode_<timestamp>.log.")
    parser.add_argument("--skip-browser-preflight", action="store_true", help="Do not check or launch Edge CDP before monitor startup.")
    parser.add_argument("--edge-path", default=DEFAULT_EDGE_PATH)
    parser.add_argument("--debug-port", type=int, default=9222)
    parser.add_argument("--profile-dir", default=DEFAULT_EDGE_PROFILE)
    args = parser.parse_args()

    if args.supervisor_only and args.monitor_only:
        raise SystemExit("--supervisor-only and --monitor-only cannot be used together")

    LOG_FILE = Path(args.log_file) if args.log_file else PROJECT_ROOT / "logs" / f"codex_mode_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    if not LOG_FILE.is_absolute():
        LOG_FILE = PROJECT_ROOT / LOG_FILE
    log_line(f"[codex-mode] log file: {LOG_FILE}")

    child_env = os.environ.copy()
    child_env["LINKEDIN_AUTOAPPLY_AI_MODE"] = "codex"

    if not args.supervisor_only and not args.skip_browser_preflight:
        if not ensure_cdp_browser(args.edge_path, args.debug_port, args.profile_dir):
            log_line("[codex-mode] browser preflight failed; start Edge with --remote-debugging-port=9222 or pass --skip-browser-preflight")
            return 2

    procs: list[tuple[str, subprocess.Popen[str]]] = []
    threads: list[threading.Thread] = []

    try:
        if not args.monitor_only:
            supervisor_cmd = [
                sys.executable,
                "-X",
                "utf8",
                "scripts/codex_supervisor.py",
                "--sandbox",
                args.codex_sandbox,
                "--codex-timeout",
                str(args.codex_timeout),
                "--poll-interval",
                str(args.poll_interval),
            ]
            if args.codex_model:
                supervisor_cmd.extend(["--model", args.codex_model])
            procs.append(("codex", start_process("codex", supervisor_cmd, env=child_env)))

        if not args.supervisor_only:
            monitor_cmd = [sys.executable, "-X", "utf8", "monitor_parallel_v3.py"]
            procs.append(("monitor", start_process("monitor", monitor_cmd, env=child_env)))

        for name, proc in procs:
            thread = threading.Thread(target=stream_output, args=(name, proc), daemon=True)
            thread.start()
            threads.append(thread)

        while procs:
            for name, proc in list(procs):
                code = proc.poll()
                if code is not None:
                    log_line(f"[codex-mode] {name} exited with code {code}")
                    procs.remove((name, proc))
                    if code != 0:
                        return code
            time.sleep(1)
    except KeyboardInterrupt:
        log_line("[codex-mode] stopping...")
        return 130
    finally:
        for _, proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for _, proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
