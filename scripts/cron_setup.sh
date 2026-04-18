#!/usr/bin/env bash
# Installs a crontab entry that runs the README agent twice a day: 07:00 and 19:00 local time.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
RUN="$PY $ROOT/run_agent.py >> $ROOT/logs/cron.log 2>&1"

MORNING="0 7 * * * $RUN"
EVENING="0 19 * * * $RUN"

TMP="$(mktemp)"
# Keep every existing cron line that does NOT belong to this agent.
crontab -l 2>/dev/null | grep -vF "$ROOT/run_agent.py" > "$TMP" || true
{
  echo "$MORNING"
  echo "$EVENING"
} >> "$TMP"

crontab "$TMP"
rm -f "$TMP"

echo "Installed cron:"
echo "  $MORNING"
echo "  $EVENING"
echo ""
echo "Verify with:  crontab -l"
echo "Tail logs:    tail -f $ROOT/logs/cron.log $ROOT/logs/agent.log"
