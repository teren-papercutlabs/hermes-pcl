#!/usr/bin/env bash
# bootstrap_local.sh — build the hermes-mtu Studio-local pilot HERMES_HOME from this deploy dir.
#
# Usage:
#   HERMES_HOME=/path/to/profile bootstrap_local.sh <telegram_bot_token_file> <allowed_user_ids_csv>
#
# - Copies config.yaml / mtu_constitution.yaml / SOUL.md into the explicitly set $HERMES_HOME
#   and syncs only the constitution-declared knowledge files.
# - Writes $HERMES_HOME/.env (chmod 600) with the Telegram bot token, the allowlist, and the
#   OpenAI key sourced from ~/.marshal/secrets.env. NO secret value is hardcoded here.
# - Idempotent: safe to re-run (overwrites config/SOUL/.env from source-of-truth).
#
# The runtime executes with:  HERMES_HOME=~/.hermes-mtu <hermes-venv>/python <hermes>/hermes gateway run
set -euo pipefail

TOKEN_FILE="${1:-$HOME/.hermes-mtu-token.tmp}"
ALLOWED_USERS="${2:-}"
: "${HERMES_HOME:?set HERMES_HOME explicitly; this script never defaults to a live profile}"
DEP="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$DEP/../../.." && pwd)"
SECRETS="$HOME/.marshal/secrets.env"

if [ -x "$REPO/.venv/bin/python" ]; then
  HERMES_PYTHON="$REPO/.venv/bin/python"
elif [ -x "$REPO/venv/bin/python" ]; then
  HERMES_PYTHON="$REPO/venv/bin/python"
else
  HERMES_PYTHON="${HERMES_PYTHON:-python3}"
fi

[ -f "$TOKEN_FILE" ] || { echo "ERROR: telegram token file not found: $TOKEN_FILE" >&2; exit 1; }
[ -f "$SECRETS" ]    || { echo "ERROR: secrets file not found: $SECRETS" >&2; exit 1; }
OPENAI_KEY="$(grep -E '^(export )?OPENAI_API_KEY=' "$SECRETS" | head -1 | sed -E 's/^(export )?OPENAI_API_KEY=//; s/^"//; s/"$//')"
[ -n "$OPENAI_KEY" ] || { echo "ERROR: OPENAI_API_KEY not in $SECRETS" >&2; exit 1; }
TG_TOKEN="$(tr -d ' \n' < "$TOKEN_FILE")"

mkdir -p "$HERMES_HOME"
cp "$DEP/config.yaml"            "$HERMES_HOME/config.yaml"
cp "$DEP/mtu_constitution.yaml" "$HERMES_HOME/mtu_constitution.yaml"
cp "$DEP/SOUL.md"               "$HERMES_HOME/SOUL.md"
"$HERMES_PYTHON" "$REPO/hermes" pa sync-knowledge \
  --source-dir "$DEP" \
  --constitution "$DEP/mtu_constitution.yaml" \
  --target-dir "$HERMES_HOME/knowledge" \
  --manifest "$HERMES_HOME/knowledge-sync.manifest.json"

umask 077
cat > "$HERMES_HOME/.env" <<ENV
# hermes-mtu pilot secrets — NOT committed. Regenerate via bootstrap_local.sh.
TELEGRAM_BOT_TOKEN=$TG_TOKEN
TELEGRAM_ALLOWED_USERS=$ALLOWED_USERS
OPENAI_API_KEY=$OPENAI_KEY
# Home channel silences the one-time /sethome onboarding nag and is where the
# gateway sends shutdown/cron notices. Point it at the TESTER chat, NOT a real
# advisor/principal — otherwise every gateway restart DMs them a "shutting down"
# notice. (276672685 = controlled tester.)
TELEGRAM_HOME_CHANNEL=${TELEGRAM_HOME_CHANNEL:-276672685}
ENV
chmod 600 "$HERMES_HOME/.env"

echo "OK: HERMES_HOME=$HERMES_HOME bootstrapped."
echo "  config.yaml, mtu_constitution.yaml, SOUL.md and declared knowledge installed."
echo "  .env written (chmod 600): TELEGRAM_BOT_TOKEN=${TG_TOKEN:0:9}..., ALLOWED_USERS=$ALLOWED_USERS, OPENAI_API_KEY set."
