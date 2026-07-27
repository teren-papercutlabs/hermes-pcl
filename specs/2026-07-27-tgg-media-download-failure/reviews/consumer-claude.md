# External Review — claude

**Prompt:** Independent cold code review. Review the Hermes durable consumer mandatory-media retry-cap/quarantine change against this contract: retryable mandatory media must cap, quarantine/dead-letter the compl...

---

## BLOCKED

Two defects undermine the stated purpose of the dead-letter (later recovery of mandatory media); the rest are contract/observability gaps.

### 1. Quarantine does not preserve the original event — only the normalized item

`stage_from_source` (`gateway/durable_jsonl_consumer.py:573-590`) persists `json.dumps(item, ...)` where `item = _bridge_item(decoded)`. For the live capture shape (`{"type":"whatsapp_capture_event","normalized":{...},"raw":{...}}`) the outer envelope is discarded at staging. `record_retention` then copies that same `raw_json` into `media_retention_quarantine` (`:1160-1180`).

Consequence: a quarantined mandatory-media event retains only `mediaUrls` pointing at a local capture path that no longer exists. The provider payload the bridge would need to re-download — `url`, `mediaKey`, `directPath` inside `raw.message.imageMessage`, exactly the fields your own `specs/2026-07-27-tgg-media-download-failure/diagnosis.md` identifies as the recovery keys — is not in the quarantine row, and not in `ingress_events` either. The quarantine table also stores no `source_device`/`source_inode`/`start_offset`, so there is no pointer back to the source line; recovery requires joining `ingress_seq → ingress_events` and hoping the source JSONL has not rotated (`stage_from_source` already fails closed on inode change, so rotation is an expected event).

Related test defect: `test_mandatory_media_retry_cap_quarantines_full_event_and_history` asserts `providerMetadata` survives, but plants that key *inside* the normalized item rather than the sibling `raw` block. It therefore passes while proving nothing about the live envelope. Contrast with `test_stage_preserves_provider_document_mime_for_spreadsheet_gate`, which does exercise the real wrapper shape and shows only a single MIME string is lifted out before `raw` is dropped.

### 2. Quarantine has no resolution state — the dead-letter cannot be drained

The table pins `status TEXT NOT NULL DEFAULT 'quarantined' CHECK (status IN ('quarantined'))`. There is no transition to recovered/discarded, no CLI or export path, and the only reader is `retention_quarantine_status()` (a `GROUP BY status` count) plus the `LEFT JOIN` in `retention_result` (`:1086-1104`). Once the bridge retry fix lands and the media becomes downloadable again, nothing can record that a quarantined event was resolved, and `retention_quarantined` grows monotonically with no drain — so it cannot serve as an alertable metric. The `media_retention_quarantine_status_idx` on `(status, quarantined_at)` is also dead weight given a single permitted value.

### 3. Deployment spec now misdescribes actual behavior, and the validator pins the stale string

`deploy/tgg/christopher/client-agent-deployment.yaml:172` still declares:

```
failureDisposition: retention-held-business-pending-retry-without-lane-death
```

That is no longer true. After the cap, the row is set to `bypassed` and enters `pending_chat_batches` (`:667`), reaching the model with `hasMedia=true` and no media. `deploy/tgg/christopher/scripts/validate_deployment_spec.py:148-150,163` asserts that exact string, so the validator actively enforces a description the code contradicts.

Likewise `statusMediaFields` (yaml `:196-207`, validator `:165-182`, exact-list equality) omits `retention_quarantined` and `retention_quarantine_status`, both of which `_retention_status` now emits. The new terminal disposition is outside the declared monitoring surface.

### 4. `max_attempts` is unreachable from the deployed config

`_retention_config` reads `max_attempts` (default 5), but `validate_deployment_spec.py:235-243` compares `config["pa"]["media_retention"]` against an exact dict with no `max_attempts` key. Adding it to any runtime slot fails validation. The cap is therefore hardcoded-by-default in production and cannot be tuned without a validator change — worth resolving in the same change rather than discovering it at deploy time.

### 5. Migrated DBs skip the `retention_state` CHECK constraint

`_init_schema:459-465` adds the column via `ALTER TABLE ... ADD COLUMN retention_state TEXT NOT NULL DEFAULT 'pending'`. SQLite cannot attach the `CHECK (retention_state IN ('pending','complete','bypassed','held'))` from the `CREATE TABLE` path this way, so pre-v3 inboxes — including the live one — permanently lack that guard while fresh DBs have it. The classification `UPDATE` loop at `:472-495` is also a one-shot, irreversible rewrite of every row with no before-image, unlike the before-image discipline applied everywhere else in this file (`requeue_selected_for_readjudication`, `_inject_bounded_source_evidence`). Recommend copying the inbox DB before first start on the new schema, and stating that in the deploy runbook.

### Verified as correct

- Cap arithmetic and the terminal transition are atomic under `BEGIN IMMEDIATE`, with the CAS guarded by `retention_state IN ('pending','held')` (`:1128-1180`).
- `PermanentMediaRefusal` remains a non-quarantine bypass, and `ensure_record_media_retained` orders the refusal branch before the retryable branch — `test_permanent_media_refusal_bypasses_without_quarantine` covers it.
- FIFO progress is preserved: quarantined rows leave `retention_candidates` (`:1063-1071`) and become eligible business work; the `pending`-before-`held` ordering prevents a stuck row from starving new arrivals.
- Live rows already at ~94k failures quarantine on the fifth post-migration attempt rather than immediately, which is the safe direction.
- No source change mutates live state; the only production write is the schema migration at next start, which the freeze gates.