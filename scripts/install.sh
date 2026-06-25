#!/usr/bin/env bash
# One-time setup. Works on macOS and Linux (VPS).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Creating Python virtual environment (.venv)"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing dependencies"
pip install --upgrade pip
pip install -r requirements.txt

echo "==> (Optional) Installing Chrome for the Playwright jobs.bg fallback"
echo "    Skip with CTRL-C if you only use zaplata."
python -m playwright install chrome || echo "   (Playwright browser install skipped/failed - fine if only using zaplata)"

echo "==> Creating .env from template (edit it to add API keys when ready)"
[ -f .env ] || cp .env.example .env

echo
echo "Done. Next steps:"
echo "  1. (optional) edit .env to add PROSPEO / ANTHROPIC / SMARTLEAD keys"
echo "  2. test it:   source .venv/bin/activate && python run_daily.py --max-new 3 --dry-run"
echo "  3. schedule:  bash scripts/setup_cron.sh"
