#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "Created .env — edit it and fill in GITLAB_TOKEN and ANTHROPIC_API_KEY."
fi

echo ""
echo "Install complete."
echo "Next:"
echo "  1. Edit $ROOT/.env"
echo "  2. Test a dry run: DRY_RUN=1 $ROOT/.venv/bin/python $ROOT/run_agent.py"
echo "  3. Schedule with: bash $ROOT/scripts/cron_setup.sh"
