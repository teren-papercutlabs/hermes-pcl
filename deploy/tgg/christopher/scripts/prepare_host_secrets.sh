#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRETS_FILE="${PCL_SECRETS_FILE:-$HOME/.marshal/secrets.env}"
raw="$(pcl service locate --system christopher --domain pa)"
target="$(python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["data"]["system"]["liveFacts"]["host"]["sshTargetAlias"])' <<<"$raw")"
[[ "$target" == "tgg-app-1" ]] || {
  echo "unexpected Christopher runtime target: $target" >&2
  exit 10
}
remote_hostname="$(ssh "$target" hostname)"
[[ "$remote_hostname" == "tgg-app-1" ]] || {
  echo "host identity mismatch: target=$target hostname=$remote_hostname" >&2
  exit 10
}

set -a
# shellcheck disable=SC1090
source "$SECRETS_FILE"
set +a
: "${OPENAI_API_KEY:?OPENAI_API_KEY missing from canonical secrets file}"

umask 077
tmp="$(mktemp)"
cleanup() {
  rm -f "$tmp"
  ssh "$target" 'rm -f /root/.pcl-secret-staging/christopher.env; rmdir /root/.pcl-secret-staging 2>/dev/null || true' \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT
python3 - "$tmp" <<'PY'
import json, os, pathlib, sys

keys = ["OPENAI_API_KEY"]
lines = []
for key in keys:
    value = os.environ.get(key)
    if value:
        lines.append(f"{key}={json.dumps(value)}")
# The current deployment boundary is management-only until Teren explicitly
# releases the site drain.  Keep the containment in the generated env so a
# routine deploy cannot silently remove it while replacing the secret file.
lines.append("TGG_DEMO_MANAGEMENT_ONLY=true")
pathlib.Path(sys.argv[1]).write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

# The TGG PS service token is deliberately not copied from Studio secrets and
# Bobby's legacy token is never reused. The processing activation transaction
# requires a separately migrated Christopher-scoped token in the co-located
# Systems authority and proves its identity/scopes read-only before either
# processing key can flip.

ssh "$target" 'set -euo pipefail
if ! getent passwd pclaw >/dev/null; then
  useradd --create-home --shell /bin/bash pclaw
fi
install -d -m 0750 -o pclaw -g pclaw /home/pclaw/.hermes-christopher-tgg
install -d -m 0700 -o root -g root /root/.pcl-secret-staging'
scp -q "$tmp" "$target:/root/.pcl-secret-staging/christopher.env"
# The processing activation transaction owns the first verified migration of
# the Christopher Systems credential. Routine deploys must not erase that
# already-migrated credential while refreshing the Studio-owned provider keys.
# Preserve the already-authorized live OpenAI key and Systems token entirely
# on the client host so neither value crosses argv, stdout, the Studio, or the
# deployment bundle during routine redeploys.
ssh "$target" python3 - \
  /home/pclaw/.hermes-christopher-tgg/.env \
  /root/.pcl-secret-staging/christopher.env \
  OPENAI_API_KEY \
  --allow-staged-fallback < "$SCRIPT_DIR/preserve_env_key.py"
ssh "$target" python3 - \
  /home/pclaw/.hermes-christopher-tgg/.env \
  /root/.pcl-secret-staging/christopher.env \
  CHRISTOPHER_TGG_PS_SERVICE_TOKEN < "$SCRIPT_DIR/preserve_env_key.py"

ssh "$target" 'set -euo pipefail
install -m 0600 -o pclaw -g pclaw \
  /root/.pcl-secret-staging/christopher.env \
  /home/pclaw/.hermes-christopher-tgg/.env
rm -f /root/.pcl-secret-staging/christopher.env
rmdir /root/.pcl-secret-staging 2>/dev/null || true'

echo "Christopher secret refs materialized on $target (values not printed)"
