#!/usr/bin/env bash
# Wrapper the scheduler calls. Activates the venv and runs one daily pass.
# Logs to data/cron.log so you can see what happened unattended.
set -euo pipefail
cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
[ -d .venv ] && source .venv/bin/activate

mkdir -p data
echo "===== run $(date -u +'%Y-%m-%dT%H:%M:%SZ') =====" >> data/cron.log
python run_daily.py "$@" >> data/cron.log 2>&1
echo "===== exit $? =====" >> data/cron.log
