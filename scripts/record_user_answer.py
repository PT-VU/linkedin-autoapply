#!/usr/bin/env python3
"""Record a confirmed user answer for Codex-mode form handling."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
QA_FILE = PROJECT_ROOT / "data" / "user_qa_preferences.json"


def load_preferences() -> dict:
    if QA_FILE.exists():
        with QA_FILE.open("r", encoding="utf-8-sig") as f:
            return json.load(f)
    return {
        "_description": "User Q&A preferences for LinkedIn-AutoApply Codex mode.",
        "_instructions": "Only store answers explicitly confirmed by the user.",
        "preferences": {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Store a reusable user answer.")
    parser.add_argument("--key", required=True, help="Stable preference key, e.g. birth_date.")
    parser.add_argument("--answer", required=True, help="Confirmed answer to reuse.")
    parser.add_argument("--patterns", default="", help="Comma-separated question patterns.")
    args = parser.parse_args()

    data = load_preferences()
    prefs = data.setdefault("preferences", {})
    patterns = [p.strip() for p in args.patterns.split(",") if p.strip()]
    prefs[args.key] = {
        "question_patterns": patterns,
        "answer": args.answer,
        "confirmed_at": datetime.now().isoformat(),
        "source": "codex_chat",
    }
    data["last_updated"] = datetime.now().isoformat()

    QA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with QA_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Recorded {args.key} in {QA_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
