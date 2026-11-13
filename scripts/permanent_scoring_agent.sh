#!/usr/bin/env bash
#===============================================================================
# Permanent Scoring Agent (Hybrid Bash + Python)
# Bash: polls every 5s for scoring_needed.flag (linkedin + glassdoor)
# Python: does all JSON parsing + scoring in a single fast call
# Runs forever, logs to data/scoring_agent.log
#
# Usage: bash scripts/permanent_scoring_agent.sh
#===============================================================================

BASE="${BASE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOG="$BASE/data/scoring_agent.log"
THRESHOLD=5.5

# Override WINBASE only when the embedded Python process needs a different
# filesystem representation (for example, a Windows drive path in Git Bash).
WINBASE="${WINBASE:-$BASE}"

# ------------------------------------------------------------------
# Log helper
# ------------------------------------------------------------------
log() {
  local ts
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  echo "[$ts] $*"
  echo "[$ts] $*" >> "$LOG"
}

# ------------------------------------------------------------------
# Python scoring script (embedded heredoc)
# Called once per platform to score all jobs and write output
# ------------------------------------------------------------------
PYTHON_SCORER=$(cat <<'PYEOF'
import json, os, sys, time
from datetime import datetime, timezone

platform = sys.argv[1]
winbase = sys.argv[2]
threshold = float(sys.argv[3])

flag = f"{winbase}/data/{platform}/scoring_needed.flag"
jd_json = f"{winbase}/data/{platform}/jds_for_review.json"
scores_json = f"{winbase}/data/{platform}/agent_scores.json"
done_flag = f"{winbase}/data/{platform}/scoring_done.flag"
log_file = f"{winbase}/data/scoring_agent.log"

def log_msg(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {platform}: {msg}"
    print(line, flush=True)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")

# Load JDs
if not os.path.isfile(jd_json):
    log_msg(f"jds_for_review.json not found, cleaning flag")
    try: os.unlink(flag)
    except FileNotFoundError: pass
    sys.exit(0)

with open(jd_json, "r", encoding="utf-8") as f:
    jds = json.load(f)
if not isinstance(jds, list):
    jds = [jds]

if not jds:
    log_msg("no jobs in jds_for_review.json")
    try: os.unlink(flag)
    except FileNotFoundError: pass
    sys.exit(0)

log_msg(f"read {len(jds)} jobs from jds_for_review.json")

# Score each job
scores = {}
for job in jds:
    jid = str(job.get("job_id") or job.get("id") or job.get("url") or str(hash(json.dumps(job, sort_keys=True))))
    title = job.get("job_title") or job.get("title") or ""
    desc = job.get("job_description") or job.get("description") or job.get("text") or ""
    text = (title + " " + desc).lower()

    score = 4.0  # base

    # AI/ML/agentic/llm → +3.0
    ai_kw = ["agentic","llm","ai engineer","generative ai","machine learning",
             "artificial intelligence","deep learning","nlp","large language model",
             "langchain","rag","retrieval augmented","neural network","langgraph",
             "computer vision","data scientist"]
    for kw in ai_kw:
        if kw in text:
            score += 3.0
            break

    # senior/lead/staff → -1.5
    sr_kw = ["senior","lead","staff","principal","head of"]
    for kw in sr_kw:
        if kw in text:
            score -= 1.5
            break

    # dutch/nederlands → -1.0
    nl_kw = ["dutch","nederlands","nederlands sprekend","vloeiend nederlands","nederlandstalig"]
    for kw in nl_kw:
        if kw in text:
            score -= 1.0
            break

    # intern/thesis → -1.0
    intern_kw = ["intern","internship","stage","afstudeer","thesis","graduation project"]
    for kw in intern_kw:
        if kw in text:
            score -= 1.0
            break

    score = max(2.0, min(10.0, score))
    scores[jid] = round(score, 1)

# Write agent_scores.json
result = {
    "reviewed_at": datetime.now(timezone.utc).isoformat(),
    "total_reviewed": len(scores),
    "scores": scores,
}
with open(scores_json, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)
log_msg(f"wrote {len(scores)} scores to agent_scores.json")

# Touch done flag
with open(done_flag, "w") as f:
    f.write(result["reviewed_at"])
log_msg("created scoring_done.flag")

# Remove needed flag
try:
    os.unlink(flag)
    log_msg("removed scoring_needed.flag")
except FileNotFoundError:
    pass

passed = sum(1 for s in scores.values() if s >= threshold)
log_msg(f"{passed}/{len(scores)} jobs passed threshold ({threshold})")
PYEOF
)

# ------------------------------------------------------------------
# process_platform: check flag → launch Python scorer
# ------------------------------------------------------------------
process_platform() {
  local platform="$1"
  local flag="$BASE/data/$platform/scoring_needed.flag"

  [ -f "$flag" ] || return 1

  log "$platform: scoring_needed.flag detected"
  python -X utf8 -c "$PYTHON_SCORER" "$platform" "$WINBASE" "$THRESHOLD"
  return 0
}

# ============================================================================
# MAIN LOOP
# ============================================================================
log "=== Permanent Scoring Agent started (Bash+Python hybrid) ==="
log "Polling every 5s for: linkedin, glassdoor"
log "Threshold: $THRESHOLD | Log: $LOG"
log "PID: $$"

cycle=0
while true; do
  cycle=$((cycle + 1))

  process_platform "linkedin" || true
  process_platform "glassdoor" || true

  # Heartbeat every ~60s
  if [ $((cycle % 12)) -eq 0 ]; then
    ts=$(date '+%H:%M:%S')
    log "Heartbeat $ts — cycle #$cycle, watching 2 platforms"
  fi

  sleep 5
done
