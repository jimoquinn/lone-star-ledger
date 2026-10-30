#!/usr/bin/env bash
# Cron entry point, every 15 minutes. SOS turnout + all county files every run;
# SOS per-county voter rosters every ROSTER_EVERY_MIN minutes (default 30).
set -u
cd "$(dirname "$0")/.."
mkdir -p data/logs
# Optional settings (REMOTE_FETCH_HOST=..., ROSTER_EVERY_MIN=...), one VAR=value per line.
[[ -f collector/env ]] && set -a && . collector/env && set +a
ROSTER_EVERY_MIN="${ROSTER_EVERY_MIN:-30}"
args=()
mins=$(( 10#$(date -u +%H) * 60 + 10#$(date -u +%M) ))
if (( mins % ROSTER_EVERY_MIN < 15 )) || [[ "${1:-}" == "--rosters" ]]; then args+=(--rosters); fi
python3 collector/collect.py "${args[@]}" >> "data/logs/collect-$(date -u +%F).log" 2>&1
# VUID match (SOS rosters vs county rosters) every MATCH_EVERY_MIN minutes (default 60); counts only in the log.
MATCH_EVERY_MIN="${MATCH_EVERY_MIN:-60}"
if (( mins % MATCH_EVERY_MIN < 15 )); then
  python3 collector/match.py >> "data/logs/match-$(date -u +%F).log" 2>&1
fi
