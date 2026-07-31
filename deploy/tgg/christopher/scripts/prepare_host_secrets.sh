#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-$(git rev-parse --show-toplevel)}"
DEPLOY_ROOT="$APP_ROOT/deploy/tgg/christopher"
SPEC="$DEPLOY_ROOT/client-agent-deployment.yaml"
CONTRACT="$APP_ROOT/deploy/pa/provider_key_contract.py"
SECRETS_FILE="${PCL_SECRETS_FILE:-$HOME/.marshal/secrets.env}"
PROVENANCE_PATH="/home/pclaw/.hermes-christopher-tgg/runtime/provider-key-provenance.json"
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

umask 077
env_tmp="$(mktemp)"
provenance_tmp="$(mktemp)"
trap 'rm -f "$env_tmp" "$provenance_tmp"' EXIT
python3 "$CONTRACT" assemble \
  --spec "$SPEC" \
  --secrets-file "$SECRETS_FILE" \
  --output-env "$env_tmp" \
  --output-provenance "$provenance_tmp" >/dev/null
# The current deployment boundary is management-only until Teren explicitly
# releases the site drain. Keep the containment in the generated env so a
# routine deploy cannot silently remove it while replacing the secret file.
printf '%s\n' 'TGG_DEMO_MANAGEMENT_ONLY=true' >>"$env_tmp"

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
install -d -m 0750 -o pclaw -g pclaw /home/pclaw/.hermes-christopher-tgg/runtime
install -d -m 0700 -o root -g root /root/.pcl-secret-staging'
scp -q "$env_tmp" "$target:/root/.pcl-secret-staging/christopher.env"
scp -q "$provenance_tmp" \
  "$target:/root/.pcl-secret-staging/provider-key-provenance.json"
ssh "$target" 'set -euo pipefail
install -m 0600 -o pclaw -g pclaw \
  /root/.pcl-secret-staging/christopher.env \
  /home/pclaw/.hermes-christopher-tgg/.env
install -m 0600 -o pclaw -g pclaw \
  /root/.pcl-secret-staging/provider-key-provenance.json \
  /home/pclaw/.hermes-christopher-tgg/runtime/provider-key-provenance.json
rm -f \
  /root/.pcl-secret-staging/christopher.env \
  /root/.pcl-secret-staging/provider-key-provenance.json
rmdir /root/.pcl-secret-staging 2>/dev/null || true'

echo "Christopher per-client provider slots materialized on $target (values not printed)"
echo "provider provenance: $PROVENANCE_PATH"
