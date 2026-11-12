"""Frozen worker entry point used by the desktop launcher."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

# These imports are intentionally explicit: the workflow scripts are loaded
# with runpy at runtime, so PyInstaller cannot discover their third-party
# dependencies from a normal import graph.
import docx  # noqa: F401
import openpyxl  # noqa: F401
import pdfplumber  # noqa: F401
import playwright  # noqa: F401
import yaml  # noqa: F401
from ctypes import wintypes  # noqa: F401


ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def dispatch() -> None:
    argv = list(sys.argv[1:])
    known_targets = {
        "main.py": ROOT / "main.py",
        "run_all_keywords.py": ROOT / "scripts" / "run_all_keywords.py",
        "codex_supervisor.py": ROOT / "scripts" / "codex_supervisor.py",
    }
    target_index = next(
        (index for index, value in enumerate(argv) if Path(value).name.lower() in known_targets),
        None,
    )
    if target_index is None:
        target = known_targets["run_all_keywords.py"]
        target_args = argv
    else:
        target = known_targets[Path(argv[target_index]).name.lower()]
        target_args = argv[target_index + 1 :]
    sys.argv[:] = [str(target), *target_args]
    runpy.run_path(str(target), run_name="__main__")


dispatch()
