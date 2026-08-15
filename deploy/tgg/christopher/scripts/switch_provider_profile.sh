#!/usr/bin/env bash
set -euo pipefail
if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $0 openai-direct-primary|openai-codex [credential-label]" >&2; exit 2
fi
case "$1" in
  openai-direct-primary) [[ $# -eq 1 ]] || { echo "direct provider accepts no credential label" >&2; exit 2; } ;;
  openai-codex) [[ $# -eq 2 && -n "$2" ]] || { echo "openai-codex requires a credential label" >&2; exit 2; } ;;
  *) echo "invalid provider profile: $1" >&2; exit 2 ;;
esac
APP_ROOT="${APP_ROOT:-/home/pclaw/apps/hermes-pcl}"
HERMES_HOME="${HERMES_HOME:-/home/pclaw/.hermes-christopher-tgg}"
DEPLOY_ROOT="$APP_ROOT/deploy/tgg/christopher"
args=(--app-root "$APP_ROOT" --hermes-home "$HERMES_HOME" --provider-profile "$1")
[[ "$1" == "openai-codex" ]] && args+=(--credential-label "$2")
"$APP_ROOT/.venv/bin/python" "$DEPLOY_ROOT/scripts/apply_engine_slot.py" "${args[@]}"
systemctl restart christopher-tgg-hermes.service
"$DEPLOY_ROOT/scripts/verify_runtime.sh" --full
