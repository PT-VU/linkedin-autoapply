"""First-run runtime checks for the packaged desktop application.

Python dependencies are bundled by PyInstaller.  The remaining user-machine
dependency is a browser executable: prefer installed Edge, and install the
Playwright Chromium runtime into the user's normal Playwright cache when Edge
is unavailable.  Credentials and browser profiles are never downloaded or
created here.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Callable


@dataclass(frozen=True)
class BootstrapResult:
    ok: bool
    browser: str
    codex_path: str
    message: str


def _edge_candidates() -> list[Path]:
    candidates: list[Path] = []
    found = shutil.which("msedge") or shutil.which("msedge.exe")
    if found:
        candidates.append(Path(found))
    if os.name == "nt":
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(variable)
            if base:
                candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    return candidates


def edge_path() -> Path | None:
    return next((path for path in _edge_candidates() if path.is_file()), None)


def codex_path() -> Path | None:
    candidates = [
        os.environ.get("LINKEDIN_CODEX_CLI"),
        os.environ.get("CODEX_APP_RUNTIME_CLI_PATH"),
        shutil.which("codex"),
        shutil.which("codex.exe"),
    ]
    for candidate in candidates:
        if candidate:
            path = Path(candidate)
            if path.is_file():
                return path
    return None


def _chromium_executable() -> Path:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        return Path(playwright.chromium.executable_path)


def _install_chromium(log: Callable[[str], None]) -> tuple[bool, str]:
    try:
        from playwright._impl._driver import compute_driver_executable

        node, cli = compute_driver_executable()
        if not Path(node).is_file() or not Path(cli).is_file():
            return False, "Playwright driver is missing from the packaged application"
        log("[bootstrap] Edge is unavailable; downloading Playwright Chromium...")
        env = os.environ.copy()
        completed = subprocess.run(
            [node, cli, "install", "chromium"],
            cwd=str(Path(sys.executable).resolve().parent),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode:
            detail = (completed.stdout or "").strip().splitlines()[-1:] or ["unknown installer error"]
            return False, f"Chromium download failed: {detail[0]}"
        return True, "Chromium downloaded"
    except Exception as exc:  # pragma: no cover - depends on local runtime
        return False, f"Chromium download failed: {exc}"


def ensure_runtime(project_root: Path, log: Callable[[str], None] | None = None) -> BootstrapResult:
    """Prepare local directories and ensure a usable browser is available."""

    emit = log or (lambda _message: None)
    for name in ("data", "logs", "user_files", "cover_letters"):
        (project_root / name).mkdir(parents=True, exist_ok=True)

    codex = codex_path()
    edge = edge_path()
    if edge:
        emit(f"[bootstrap] Edge found: {edge}")
        return BootstrapResult(True, "msedge", str(codex or ""), "Runtime ready")

    try:
        chromium = _chromium_executable()
    except Exception as exc:
        return BootstrapResult(False, "", str(codex or ""), f"Playwright browser check failed: {exc}")

    if not chromium.is_file():
        installed, message = _install_chromium(emit)
        if not installed:
            return BootstrapResult(False, "", str(codex or ""), message)
        chromium = _chromium_executable()

    if not chromium.is_file():
        return BootstrapResult(False, "", str(codex or ""), "Chromium installation completed but executable is missing")
    emit(f"[bootstrap] Playwright Chromium ready: {chromium}")
    return BootstrapResult(True, "chromium", str(codex or ""), "Runtime ready")
