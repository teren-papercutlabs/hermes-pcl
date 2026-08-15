#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 gpt-5.4-mini|gpt-5.6-luna|gpt-5.6-luna-low|gpt-5.6-luna-xhigh|gpt-5.6-terra-medium|gpt-5.6-terra-high" >&2
  exit 2
fi
case "$1" in
  gpt-5.4-mini|gpt-5.6-luna|gpt-5.6-luna-low|gpt-5.6-luna-xhigh|gpt-5.6-terra-medium|gpt-5.6-terra-high) ;;
  *) echo "invalid engine slot: $1" >&2; exit 2 ;;
esac

APP_ROOT="${APP_ROOT:-/home/pclaw/apps/hermes-pcl}"
HERMES_HOME="${HERMES_HOME:-/home/pclaw/.hermes-christopher-tgg}"
DEPLOY_ROOT="$APP_ROOT/deploy/tgg/christopher"
old_slot="$(<"$HERMES_HOME/runtime/engine-slot")"

"$APP_ROOT/.venv/bin/python" "$DEPLOY_ROOT/scripts/apply_engine_slot.py" \
  --app-root "$APP_ROOT" \
  --hermes-home "$HERMES_HOME" \
  --slot "$1"
systemctl restart christopher-tgg-hermes.service
if ! "$APP_ROOT/.venv/bin/python" "$DEPLOY_ROOT/scripts/verify_release_minimal.py" \
  --app-root "$APP_ROOT" --hermes-home "$HERMES_HOME"; then
  "$APP_ROOT/.venv/bin/python" "$DEPLOY_ROOT/scripts/apply_engine_slot.py" \
    --app-root "$APP_ROOT" --hermes-home "$HERMES_HOME" --slot "$old_slot"
  systemctl restart christopher-tgg-hermes.service
  "$APP_ROOT/.venv/bin/python" "$DEPLOY_ROOT/scripts/verify_release_minimal.py" \
    --app-root "$APP_ROOT" --hermes-home "$HERMES_HOME"
  echo "engine switch failed; previous slot restored" >&2
  exit 1
fi
