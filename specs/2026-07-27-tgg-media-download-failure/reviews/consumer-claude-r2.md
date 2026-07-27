# External Review — claude

**Prompt:** Re-review the prior BLOCKED mandatory-media quarantine change after the corrective commit. Verify only the original contract blockers: exact complete source envelope (including sibling raw provider pa...

---

**CLEAR** — the corrective commit closes all of the original contract blockers I raised. Verification against each:

**1. Exact complete source envelope retained through staging**
- `gateway/durable_jsonl_consumer.py:568` stages `(start, end, item, decoded)` where `decoded` is the untouched parsed JSONL line, persisted to `source_envelope_json` at :595.
- The normalization mutation (`item["mediaMimes"] = [...]`, :569) is applied to `_bridge_item`'s shallow copy via a **top-level** key assignment, so `decoded` (and its nested `normalized`/`raw` objects) is not aliased-mutated. Envelope stored is byte-faithful modulo key sort.
- Quarantine copy reads `row["source_envelope_json"] or row["raw_json"]` (:1189) inside the same `BEGIN IMMEDIATE` — no source JSONL re-read, no file dependency, no cursor/offset seek. `raw_json` fallback only covers pre-v4 backfilled rows.

**2. Live wrapper shape in test**
`tests/gateway/test_durable_jsonl_consumer.py:278-291` now writes the real `{"type": "whatsapp_capture_event", "normalized": …, "raw": {message.imageMessage{directPath, mediaKey}, providerSibling}}` envelope, and asserts `preserved_envelope == source_envelope` plus explicit sibling-payload assertions. The normalized-vs-envelope distinction is asserted separately (`event["raw_json"]` keeps `providerMetadata`; `quarantine["raw_json"] == event["source_envelope_json"]`).

**3. Cap=5 / quarantine-then-bypass / new status fields declared and permitted**
- `pa.media_retention.max_attempts: 5` present in root `config.yaml`, all three runtime slots, the slot builder literal and its `_validate` assertion, and the spec validator's per-slot expected dict.
- `failureDisposition` string is byte-identical between `client-agent-deployment.yaml:172`, `validate_deployment_spec.py:148-151`, and `tests/deploy/test_tgg_verify_runtime.py`; `maxAttempts: 5` is in both spec and validator's `expected_retention` (exact-equality dict, so undeclared drift still fails closed).
- `retention_quarantined` / `retention_quarantine_status` appear in the same order in `statusMediaFields` and the validator's exact-order expected list; `_retention_status` emits both on every status-write branch (standby, held, running, once), so `verify_runtime.sh`'s new asserts cannot hit a missing key in any reachable state. The count/status-map cross-check is consistent with the table's `CHECK (status IN ('quarantined'))`.

**4. PermanentMediaRefusal remains non-quarantine**
`PermanentMediaRefusal(ConsumerError)` is not a `MediaRetentionError`, `retain_record_media` re-raises it untouched, and `ensure_record_media_retained` handles it via the `bypassed=True, refusal=…` branch of `record_retention`, which never reaches the `error is not None` cap/quarantine path. Regression-locked by `test_permanent_media_refusal_bypasses_without_quarantine`.

**Non-blocking notes (no action required for this WB)**
- Pre-v4 rows are backfilled `source_envelope_json = raw_json`, i.e. the normalized item, not the true envelope. That is the only envelope available without a source-file read, and the test asserts exactly this. Legacy rows that later quarantine will carry the lossy value.
- The fresh-schema column is `TEXT NOT NULL` while the ALTER-added column on migrated DBs is nullable. Harmless (the single INSERT always supplies it, reads use `COALESCE`-equivalent), but the two shapes are not identical.
- I could not verify the three rewritten `runtime-slots/SHA256SUMS` digests read-only, and no test in `tests/` exercises `validate_deployment_spec.py` or the checksum manifest. If a digest is wrong the validator raises and deploy fails closed, so this is a coverage gap rather than a safety hole.