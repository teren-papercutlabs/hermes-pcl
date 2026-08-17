# TGG Systems storage retention and alerting

This is deterministic Systems machinery, not Christopher reasoning. It removes only
policy-named, aged direct children: host test artefacts, terminal direct-deploy
staging/transactions, and old named pre-import backups. For report-cycle runs it
retains the run directory and manifest indefinitely and removes only named
database payloads (`preview.db`, `backup-*.db`) from old terminal reconciliation
runs. The newest five are retained and nothing younger than seven days is
eligible. Live tenant databases, WhatsApp media/capture/corpus, imports, active
runtime, and current releases are protected explicitly.

Terminal direct-deploy staging is eligible immediately only after its matching
transaction receipt is terminal. The newest two terminal runs are retained; for
older terminal transactions only `preimage.db` is removed, never its receipt,
manifest, or run directory. Incomplete attempts are not candidates.

Each approved apply first writes a durable `*.intent.json` with exact selected paths,
counts, and bytes; it then writes a completion (or partial-failure) receipt at
`/home/pclaw/.systems-pcl/data/retention-receipts/systems-retention-*.json`.
The cleanup unit provisions its receipt directory before sandboxing, runs as root
because direct-deploy producers are root-owned, and intentionally has no private
`/tmp`, so the closed host-test allowlist sees the actual host files.

## Review and install

Do not run the `--apply` command manually. After review, install the four unit
files and two scripts from the deployed Hermes checkout, then enable the new
Systems names (and disable the retired Christopher timer):

```sh
sudo systemctl disable --now christopher-tgg-retention-cleanup.timer
sudo install -m 0755 deploy/tgg/systems/scripts/systems_retention_cleanup.py /home/pclaw/apps/hermes-pcl/deploy/tgg/systems/scripts/
sudo install -m 0755 deploy/tgg/systems/scripts/systems_storage_monitor.py /home/pclaw/apps/hermes-pcl/deploy/tgg/systems/scripts/
sudo install -m 0644 deploy/tgg/systems/retention-policy.tgg-app-1.yaml /home/pclaw/apps/hermes-pcl/deploy/tgg/systems/
sudo install -m 0644 deploy/tgg/systems/systemd/*.service deploy/tgg/systems/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now systems-papercut-labs-tgg-retention-cleanup.timer systems-papercut-labs-storage-monitor.timer
sudo systemctl start systems-papercut-labs-tgg-retention-cleanup.service
sudo systemctl start systems-papercut-labs-storage-monitor.service
```

The installed cleanup unit is receipt-only by default. Start it once, inspect
the exact `selected` paths in its first production receipt, and obtain approval
before adding `--apply` to its `ExecStart` and reloading systemd. The monitor
also starts in `--dry-run`: it records transitions but sends nothing.

## Telegram activation

Put only nonsecret `SYSTEMS_TELEGRAM_CHAT_ID` and the existing `@pcl_edna_bot`
token in root-owned `/etc/systems-papercut-labs/alerting.env` (mode `0600`). The
target chat ID must be recovered from an inbound marker through the bot's existing
update stream; do not put tokens, raw updates, or the chat ID discovery output in
Git or receipts. After approval of one labelled outbound test, remove `--dry-run`
from `systems-papercut-labs-storage-monitor.service` and restart that service.

The monitor alerts at 75% used / 15 GiB free and 85% used / 8 GiB free, if cleanup
fails, or if its receipt is older than 26 hours. It deduplicates state changes,
repeats unresolved alerts daily, and emits recovery. Each alert reports usage,
daily growth, and the last cleanup outcome. Its durable deduplication state is
kept in the systemd-managed `/var/lib/systems-papercut-labs` state directory.

## Rollback

```sh
sudo systemctl disable --now systems-papercut-labs-tgg-retention-cleanup.timer systems-papercut-labs-storage-monitor.timer
sudo rm -f /etc/systemd/system/systems-papercut-labs-tgg-retention-cleanup.{service,timer} /etc/systemd/system/systems-papercut-labs-storage-monitor.{service,timer}
sudo systemctl daemon-reload
```

Rollback stops future cleanup/alerts; it cannot restore already deleted policy
artefacts. Retained live state and backups are outside the policy.
