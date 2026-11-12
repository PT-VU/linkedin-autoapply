"""PyInstaller worker entry point.

The GUI uses this executable in a frozen build and the Python script in a
source checkout. The actual workflow remains the current run_all_keywords.py.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
runpy.run_path(str(ROOT / "scripts" / "run_all_keywords.py"), run_name="__main__")
