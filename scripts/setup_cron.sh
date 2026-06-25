#!/usr/bin/env bash
# Installs a daily schedule. Detects macOS (launchd) vs Linux (cron) and sets
# up a run at 09:00 local time. Idempotent-ish: re-running replaces the entry.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"
RUN_HOUR="${RUN_HOUR:-9}"   # override: RUN_HOUR=8 bash scripts/setup_cron.sh

OS="$(uname -s)"
if [ "$OS" = "Darwin" ]; then
  # ---- macOS: launchd ----
  PLIST="$HOME/Library/LaunchAgents/com.athr.outreach.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  sed -e "s#__PROJECT_DIR__#${PROJECT_DIR}#g" -e "s#__HOUR__#${RUN_HOUR}#g" \
      deploy/launchd/com.athr.outreach.plist > "$PLIST"
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  echo "Installed launchd job -> runs daily at ${RUN_HOUR}:00."
  echo "  Inspect : launchctl list | grep athr"
  echo "  Logs    : ${PROJECT_DIR}/data/cron.log"
  echo "  Remove  : launchctl unload $PLIST && rm $PLIST"
else
  # ---- Linux: cron ----
  CRON_LINE="0 ${RUN_HOUR} * * * ${PROJECT_DIR}/scripts/run_once.sh"
  ( crontab -l 2>/dev/null | grep -v "athr_outreach_system/scripts/run_once.sh" ; echo "$CRON_LINE" ) | crontab -
  echo "Installed cron job -> runs daily at ${RUN_HOUR}:00."
  echo "  Inspect : crontab -l"
  echo "  Logs    : ${PROJECT_DIR}/data/cron.log"
  echo "  Remove  : crontab -e  (delete the run_once.sh line)"
  echo
  echo "For a systemd timer instead of cron, see deploy/systemd/README."
fi
