"""Small, stable CLI seam around the current workflow implementation."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - doctor reports this clearly
    yaml = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _config_path(value: str | None) -> Path:
    path = Path(value or "config.yaml")
    return path if path.is_absolute() else PROJECT_ROOT / path


def _load_config(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is not installed; run pip install -r requirements.txt")
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _worker_command(config: Path, args: argparse.Namespace) -> list[str]:
    command = [sys.executable, str(PROJECT_ROOT / "scripts" / "run_all_keywords.py")]
    command.extend(["--config", str(config)])
    command.extend(["--max-pages", str(args.max_pages)])
    if args.location:
        command.extend(["--location", args.location])
    if args.date_posted:
        command.extend(["--date-posted", args.date_posted])
    if args.start_index:
        command.extend(["--start-index", str(args.start_index)])
    if args.no_supervisor:
        command.append("--no-supervisor")
    if args.dry_run:
        command.append("--dry-run")
    return command


def doctor(config: Path) -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("config.yaml", config.exists(), str(config)))
    checks.append(("PyYAML", yaml is not None, "configuration parser"))
    config_data = _load_config(config) if config.exists() and yaml is not None else {}
    resume = str((config_data.get("resume") or {}).get("path") or "").strip()
    resume_path = Path(resume)
    if resume and not resume_path.is_absolute():
        resume_path = PROJECT_ROOT / resume_path
    checks.append(("resume", bool(resume and resume_path.is_file()), str(resume_path)))
    ai = config_data.get("ai") or {}
    backend = str(ai.get("backend") or "codex").strip().lower()
    if backend in {"api", "openai", "openai_compatible"}:
        base_url = str(ai.get("base_url") or "").strip()
        model = str(ai.get("model") or "").strip()
        key_env = str(ai.get("api_key_env") or "OPENAI_API_KEY").strip()
        key_present = bool(os.environ.get(key_env, "").strip())
        checks.append(("API backend", bool(base_url and model and key_present), f"{base_url or 'base_url missing'}; model={model or 'missing'}; key={key_env}"))
    else:
        codex = shutil.which("codex") or shutil.which("codex.exe")
        checks.append(("Codex CLI", bool(codex), codex or "not found on PATH"))
    try:
        import playwright  # noqa: F401
        checks.append(("Playwright", True, "Python package"))
    except ImportError:
        checks.append(("Playwright", False, "Python package missing"))
    for label, ok, detail in checks:
        print(f"[{ 'OK' if ok else 'FAIL' }] {label}: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


def run_workflow(args: argparse.Namespace) -> int:
    config = _config_path(args.config)
    if not config.exists():
        print(f"Missing {config}. Copy config.example.yaml to config.yaml first.", file=sys.stderr)
        return 2
    env = os.environ.copy()
    env["LINKEDIN_AUTOAPPLY_AI_MODE"] = "codex"
    if args.dry_run:
        env["LINKEDIN_AUTOAPPLY_DRY_RUN"] = "1"
    command = _worker_command(config, args)
    print("Starting:", " ".join(command), flush=True)
    return subprocess.call(command, cwd=PROJECT_ROOT, env=env)


def show_queue() -> int:
    path = PROJECT_ROOT / "data" / "manual_apply_queue.json"
    if not path.exists():
        print("Manual queue is empty.")
        return 0
    records = json.loads(path.read_text(encoding="utf-8"))
    pending = [row for row in records if row.get("status") in {"待投递", "pending", "needs_info"}]
    print(f"Manual queue: {len(pending)} pending / {len(records)} total")
    for row in pending:
        print(f"- {row.get('title', '')} @ {row.get('company', '')} [{row.get('status', '')}]")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="linkedin-autoapply")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor_parser = sub.add_parser("doctor", help="Check local prerequisites and configuration")
    doctor_parser.add_argument("--config", default="config.yaml")

    run_parser = sub.add_parser("run", help="Run the current multi-keyword workflow")
    run_parser.add_argument("--config", default="config.yaml")
    run_parser.add_argument("--max-pages", type=int, default=5)
    run_parser.add_argument("--start-index", type=int, default=0)
    run_parser.add_argument("--location", default=None)
    run_parser.add_argument("--date-posted", default=None)
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("--no-supervisor", action="store_true")

    sub.add_parser("queue", help="Show the manual application queue").set_defaults(queue=True)
    sub.add_parser("configure", help="Open the desktop configuration window").set_defaults(configure=True)

    args = parser.parse_args(argv)
    if getattr(args, "queue", False):
        return show_queue()
    if getattr(args, "configure", False):
        from .gui import main as gui_main
        return gui_main()
    if args.command == "doctor":
        return doctor(_config_path(args.config))
    if args.command == "run":
        return run_workflow(args)
    parser.error("unknown command")
    return 2
