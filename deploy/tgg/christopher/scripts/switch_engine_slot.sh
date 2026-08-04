#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 gpt-5.4-mini|gpt-5.6-luna|gpt-5.6-luna-low|gpt-5.6-luna-xhigh|gpt-5.6-terra-medium" >&2
  exit 2
fi
case "$1" in
  gpt-5.4-mini|gpt-5.6-luna|gpt-5.6-luna-low|gpt-5.6-luna-xhigh|gpt-5.6-terra-medium) ;;
  *) echo "invalid engine slot: $1" >&2; exit 2 ;;
esac

APP_ROOT="${APP_ROOT:-/home/pclaw/apps/hermes-pcl}"
HERMES_HOME="${HERMES_HOME:-/home/pclaw/.hermes-christopher-tgg}"
DEPLOY_ROOT="$APP_ROOT/deploy/tgg/christopher"

"$APP_ROOT/.venv/bin/python" "$DEPLOY_ROOT/scripts/apply_engine_slot.py" \
  --app-root "$APP_ROOT" \
  --hermes-home "$HERMES_HOME" \
  --slot "$1"
systemctl restart christopher-tgg-hermes.service
"$DEPLOY_ROOT/scripts/verify_runtime.sh" --full
