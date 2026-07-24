# Sunday Driver Runbook — PA Message Store Cutover and Backfill

This is an execution handoff, not evidence that the cutover ran. Run only on
the client host during the approved Sunday window.

## Inputs

- Reviewed Hermes code commit: `0ac2563aea`
- Reviewed Systems commit: `8841d89b30e6c6dfc107974ae7a3364e47d118c0`
- Physical database:
  `/home/pclaw/.systems-pcl/data/tenants/tgg.db`
- Capture feed:
  `/var/lib/tgg-capture/whatsapp/capture/events.jsonl`
- History-sync feed:
  `/home/pclaw/.hermes-christopher-tgg/whatsapp-v2/history-sync.jsonl`

The two JSONL feeds are read locally on the client host. Do not copy them into
any repository.

## 1. Preflight and quiesce

```bash
set -euo pipefail
export HERMES_APP=/home/pclaw/apps/hermes-pcl
export SYSTEMS_APP=/home/pclaw/apps/systems-papercut-labs
export DB=/home/pclaw/.systems-pcl/data/tenants/tgg.db
export CAPTURE=/var/lib/tgg-capture/whatsapp/capture/events.jsonl
export HISTORY=/home/pclaw/.hermes-christopher-tgg/whatsapp-v2/history-sync.jsonl
export RUN=/home/pclaw/backups/pa-message-store-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -m 700 "$RUN"

test -s "$DB"
test -s "$CAPTURE"
test -s "$HISTORY"
systemctl is-active christopher-tgg-hermes.service
systemctl stop christopher-tgg-hermes.service
```

Stop any old scheduled invocation of
`scripts/tgg_message_ledger_sync.py` before proceeding. The reviewed Systems
version is a fail-loud tombstone, not a writer.

## 2. Install the reviewed source while the writer is stopped

Use the normal deployment mechanism to put exactly `0ac2563aea` at
`$HERMES_APP` and exactly `8841d89b30e6c6dfc107974ae7a3364e47d118c0` at
`$SYSTEMS_APP`. Build Systems from that canonical checkout. Do not restart
either service yet.

Record the pre-cutover revisions:

```bash
git -C "$HERMES_APP" rev-parse HEAD > "$RUN/hermes.before.sha"
git -C "$SYSTEMS_APP" rev-parse HEAD > "$RUN/systems.before.sha"
```

## 3. Initialize and backfill through the one writer

```bash
"$HERMES_APP/.venv/bin/python" \
  "$HERMES_APP/scripts/pa_message_store.py" init --db "$DB" \
  | tee "$RUN/init.json"

"$HERMES_APP/.venv/bin/python" \
  "$HERMES_APP/scripts/pa_message_store.py" backfill \
  --db "$DB" \
  --capture-jsonl "$CAPTURE" \
  --history-jsonl "$HISTORY" \
  --snapshot "$RUN/tgg.pre-backfill.db" \
  --before-images "$RUN/before-images.jsonl" \
  --held-conflicts "$RUN/held-conflicts.jsonl" \
  --report "$RUN/report.json" \
  | tee "$RUN/backfill.stdout.json"
```

The command deliberately runs capture first and history-sync second through
the same writer. Capture facts win overlap; history aliases are retained.
Zero-byte before-image and held-conflict files are valid.

## 4. Verify before enabling ingress

```bash
"$HERMES_APP/.venv/bin/python" \
  "$HERMES_APP/scripts/pa_message_store.py" verify --db "$DB" \
  | tee "$RUN/verify.json"

python3 - "$RUN/report.json" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
r = json.loads(p.read_text())
assert r["ok"] is True, r
v = r["verification"]
assert v["integrity_check"] == "ok", v
assert v["rows"] == v["fts_rows"], v
assert v["duplicate_message_ids"] == 0, v
assert v["duplicate_source_keys"] == 0, v
print(json.dumps({
    "rows": v["rows"],
    "fts_rows": v["fts_rows"],
    "capture": r["feeds"]["capture"],
    "history_sync": r["feeds"]["history_sync"],
}))
PY

wc -l "$RUN/before-images.jsonl" "$RUN/held-conflicts.jsonl"
```

Read every held-conflict record before enabling. Do not infer a winner for a
held record. If a conflict cannot be dispositioned from source evidence in the
window, leave it held and record that gap.

## 5. Enable and smoke-test

```bash
systemctl daemon-reload
systemctl start christopher-tgg-hermes.service
systemctl is-active christopher-tgg-hermes.service
journalctl -u christopher-tgg-hermes.service -n 100 --no-pager

grep -F -- '--message-store-db /home/pclaw/.systems-pcl/data/tenants/tgg.db' \
  /etc/systemd/system/christopher-tgg-hermes.service

"$HERMES_APP/.venv/bin/python" \
  "$HERMES_APP/scripts/pa_message_store.py" verify --db "$DB"
```

Admit one controlled tester message through the deployed capture path. Verify
all three consumer surfaces before declaring the cutover complete:

1. the source cursor advanced;
2. exactly one `message_ledger` row exists for the controlled message id;
3. `messages_search` returns that message id and no raw media bytes.

For a controlled photo, also verify a single stored `description` and that a
second retrieval does not generate another description.

## Rollback

```bash
systemctl stop christopher-tgg-hermes.service
cp -p "$DB" "$RUN/tgg.failed-cutover.db"
cp -p "$RUN/tgg.pre-backfill.db" "$DB"
```

Restore the recorded pre-cutover Hermes and Systems revisions through the
normal deployment mechanism, rebuild Systems, reload units, and restart the
previous service. Then verify SQLite integrity and service health. Keep
`report.json`, `before-images.jsonl`, `held-conflicts.jsonl`, and the failed DB
copy under the run directory for diagnosis.
