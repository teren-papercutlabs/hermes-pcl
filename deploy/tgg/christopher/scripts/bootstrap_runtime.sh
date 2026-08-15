#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/home/pclaw/apps/hermes-pcl}"
HERMES_HOME="${HERMES_HOME:-/home/pclaw/.hermes-christopher-tgg}"
TEST_HOME="${TEST_HOME:-/home/pclaw/.hermes-christopher-tgg-test}"
CAPTURE_SOURCE="${CAPTURE_SOURCE:-/var/lib/tgg-capture/whatsapp/capture/events.jsonl}"
DEPLOY_ROOT="$APP_ROOT/deploy/tgg/christopher"
RUNTIME_ROOT="$HERMES_HOME/runtime"

if ! getent passwd pclaw >/dev/null; then
  useradd --create-home --shell /bin/bash pclaw
fi
if getent group tggcapture >/dev/null; then
  usermod -a -G tggcapture pclaw
fi

if ! dpkg-query -W -f='${Status}' python3-venv 2>/dev/null | grep -q 'install ok installed'; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq python3-venv
fi

install -d -m 0755 -o pclaw -g pclaw /home/pclaw /home/pclaw/apps
install -d -m 0750 -o pclaw -g pclaw "$HERMES_HOME" "$RUNTIME_ROOT" "$TEST_HOME"
# pa-agent creates newly introduced parent directories as root mode 0700.
# Normalize the plugin source path before the runtime user or verifier reads it.
install -d -m 0755 -o root -g root \
  "$DEPLOY_ROOT/plugins" \
  "$DEPLOY_ROOT/plugins/report-operations"
test -s "$HERMES_HOME/.env" || {
  echo "missing $HERMES_HOME/.env; run prepare_host_secrets.sh first" >&2
  exit 20
}
grep -qE '^OPENAI_API_KEY=' "$HERMES_HOME/.env" || {
  echo "missing OPENAI_API_KEY in Hermes env" >&2
  exit 20
}
grep -qE '^CHRISTOPHER_TGG_PS_SERVICE_TOKEN=' "$HERMES_HOME/.env" || {
  echo "missing CHRISTOPHER_TGG_PS_SERVICE_TOKEN in Hermes env" >&2
  exit 20
}
chmod 0600 "$HERMES_HOME/.env"
chown pclaw:pclaw "$HERMES_HOME/.env"

if [[ ! -x "$APP_ROOT/.venv/bin/python" ]]; then
  python3 -m venv "$APP_ROOT/.venv"
fi
# The immutable app tree is root-owned by pcl pa-agent. Editable installation
# writes its egg-info beside pyproject.toml, so bootstrap performs the install
# as root and leaves both source and venv non-writable to the runtime user.
"$APP_ROOT/.venv/bin/python" -m pip install \
  --disable-pip-version-check --no-input \
  --editable "$APP_ROOT" \
  'websockets==15.0.1'
chown -R root:root "$APP_ROOT/.venv"

"$APP_ROOT/.venv/bin/python" \
  "$DEPLOY_ROOT/scripts/validate_deployment_spec.py" \
  --app-root "$APP_ROOT" \
  --spec "$DEPLOY_ROOT/client-agent-deployment.yaml"

install -m 0640 -o root -g pclaw "$DEPLOY_ROOT/SOUL.md" "$HERMES_HOME/SOUL.md"
install -d -m 0750 -o root -g pclaw "$HERMES_HOME/plugins"
ln -sfn "$DEPLOY_ROOT/plugins/report-operations" "$HERMES_HOME/plugins/report-operations"

# Deployment owns code/config refresh, not activation state. Create the gate
# fail-closed on first install, then validate and preserve either live boolean
# state on every subsequent idempotent bootstrap.
"$APP_ROOT/.venv/bin/python" \
  "$DEPLOY_ROOT/scripts/ensure_processing_gate.py" \
  "$RUNTIME_ROOT/processing-gate.json"
chown root:pclaw "$RUNTIME_ROOT/processing-gate.json"
chmod 0640 "$RUNTIME_ROOT/processing-gate.json"

slot_args=()
if [[ -n "${CHRISTOPHER_ENGINE_SLOT:-}" ]]; then
  slot_args=(--slot "$CHRISTOPHER_ENGINE_SLOT")
fi
"$APP_ROOT/.venv/bin/python" "$DEPLOY_ROOT/scripts/apply_engine_slot.py" \
  --app-root "$APP_ROOT" \
  --hermes-home "$HERMES_HOME" \
  "${slot_args[@]}"

if [[ ! -e "$RUNTIME_ROOT/capture-cursor.json" ]]; then
  runuser -u pclaw -- "$APP_ROOT/.venv/bin/python" \
    "$APP_ROOT/gateway/durable_jsonl_consumer.py" init-cursor \
    --source "$CAPTURE_SOURCE" \
    --cursor "$RUNTIME_ROOT/capture-cursor.json" \
    --position end >/dev/null
fi

for unit in \
  christopher-tgg-hermes.service \
  christopher-tgg-hermes-health.service \
  christopher-tgg-hermes-health.timer \
  christopher-tgg-retention-cleanup.service \
  christopher-tgg-retention-cleanup.timer \
  christopher-tgg-report-weekly.service \
  christopher-tgg-report-weekly.timer \
  christopher-tgg-report-monthly.service \
  christopher-tgg-report-monthly.timer; do
  ln -sfn "$DEPLOY_ROOT/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
systemctl enable christopher-tgg-hermes.service >/dev/null
systemctl enable --now christopher-tgg-hermes-health.timer >/dev/null
systemctl enable --now christopher-tgg-retention-cleanup.timer >/dev/null

schedule_enabled="$($APP_ROOT/.venv/bin/python - "$HERMES_HOME/config.yaml" <<'PY'
import sys, yaml
config = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
enabled = (((config.get("pa") or {}).get("report_operations") or {}).get("schedule") or {}).get("enabled")
print("true" if enabled is True else "false")
PY
)"
if [[ "$schedule_enabled" == "true" ]]; then
  systemctl enable --now christopher-tgg-report-weekly.timer >/dev/null
  systemctl enable --now christopher-tgg-report-monthly.timer >/dev/null
else
  systemctl disable --now christopher-tgg-report-weekly.timer >/dev/null 2>&1 || true
  systemctl disable --now christopher-tgg-report-monthly.timer >/dev/null 2>&1 || true
fi

echo "Christopher Hermes runtime bootstrap complete"
