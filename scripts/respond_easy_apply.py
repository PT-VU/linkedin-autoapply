#!/usr/bin/env python3
"""Answer an Easy Apply question while the current modal remains open.

The running LinkedIn process writes ``data/linkedin/form_user_question.json``
when Codex cannot answer a truthful question.  This script writes the answer
to the matching event; the live process consumes it and resumes the same
browser modal without restarting the job.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "linkedin"
QUESTION_FILE = DATA_DIR / "form_user_question.json"
ANSWER_FILE = DATA_DIR / "form_user_answer.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Answer the currently waiting Easy Apply question.")
    parser.add_argument("--answer", help="Answer for the only pending question.")
    parser.add_argument(
        "--answers-json",
        help="JSON object mapping the exact question text to its answer.",
    )
    parser.add_argument("--event-id", default="", help="Optional event id from form_user_question.json.")
    args = parser.parse_args()

    if not QUESTION_FILE.exists():
        raise SystemExit(f"No pending Easy Apply question: {QUESTION_FILE}")
    question_payload = json.loads(QUESTION_FILE.read_text(encoding="utf-8"))
    event_id = args.event_id or str(question_payload.get("event_id") or "")
    questions = [str(q) for q in question_payload.get("questions", [])]

    if args.answers_json:
        answers = json.loads(args.answers_json)
        if not isinstance(answers, dict):
            raise SystemExit("--answers-json must be a JSON object")
        answers = {str(k): str(v) for k, v in answers.items()}
    elif args.answer is not None and len(questions) == 1:
        answers = {questions[0]: args.answer}
    elif args.answer is not None and questions:
        raise SystemExit("There are multiple questions; use --answers-json with exact question keys.")
    else:
        raise SystemExit("Provide --answer or --answers-json")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = ANSWER_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"event_id": event_id, "answers": answers}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(ANSWER_FILE)
    print(f"Easy Apply answer written for event {event_id or '(unspecified)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
