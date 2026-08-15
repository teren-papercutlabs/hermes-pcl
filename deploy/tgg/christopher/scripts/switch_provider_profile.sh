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
read -r old_provider old_label < <("$APP_ROOT/.venv/bin/python" - "$HERMES_HOME/runtime/provider-profile.json" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
profile = json.loads(path.read_text()) if path.exists() else {"provider": "openai-direct-primary", "credential_label": None}
print(profile["provider"], profile.get("credential_label") or "-")
PY
)
apply_profile() {
  local provider="$1" label="${2:--}"
  local profile_args=(--app-root "$APP_ROOT" --hermes-home "$HERMES_HOME" --provider-profile "$provider")
  [[ "$provider" == "openai-codex" ]] && profile_args+=(--credential-label "$label")
  "$APP_ROOT/.venv/bin/python" "$DEPLOY_ROOT/scripts/apply_engine_slot.py" "${profile_args[@]}"
}
apply_profile "$1" "${2:--}"
systemctl restart christopher-tgg-hermes.service
if ! "$APP_ROOT/.venv/bin/python" "$DEPLOY_ROOT/scripts/verify_release_minimal.py" \
  --app-root "$APP_ROOT" --hermes-home "$HERMES_HOME"; then
  apply_profile "$old_provider" "$old_label"
  systemctl restart christopher-tgg-hermes.service
  "$APP_ROOT/.venv/bin/python" "$DEPLOY_ROOT/scripts/verify_release_minimal.py" \
    --app-root "$APP_ROOT" --hermes-home "$HERMES_HOME"
  echo "provider switch failed; previous profile restored" >&2
  exit 1
fi
