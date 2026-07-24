# External Review — claude

**Prompt:** Independent merge-gate review. Compare both patches to the ratified spec and task constraints. Check correctness and safety in detail: exactly one production message_ledger row-creation writer; live a...

---

## BLOCK — round 1

Two patches read in full against the spec, plus the live consumer code at `gateway/durable_jsonl_consumer.py:530-644, 2917-2947`. The one-writer contract, attach-only demotion, and retrieval surface are largely right; the ingress failure path, the cutover sequence, and the missing export backfill are not.

### Blockers

**B1 — Writer errors wedge the entire live capture pipeline.** `stage_from_source` calls `record_message` for every staged item with no per-record hold (`durable_jsonl_consumer.py:591-603`). Any `MessageConflictError` ("message identity crosses chats", "aliases resolve to multiple messages", "alias belongs to another message") or `MessageStoreError` (missing/naive/unparseable timestamp in `normalize_event._timestamp`) propagates out of staging → cursor never advances → every subsequent message is blocked, and `Restart=always` turns it into a crash loop. The backfill lane has a `held_sink`; the live lane has none, so a single odd record halts ingestion. This directly contradicts spec §6 ("blast radius: zero on Christopher's runtime") and §3.4 ("conflicts go to a held list"). Conflicts must be held per-record (quarantine row + status counter), not raised into the admission path.

**B2 — Consumer depends on schema that it never creates and never preflights.** `run_consumer` builds `MessageStore(path)` but never calls `initialize()` (`durable_jsonl_consumer.py:2786-2787`). `pa_message_aliases`, `pa_message_fts`, `description`, and `source_keys_json` do not exist in production `tgg.db` until the CLI `init` runs. Start the service before init — or execute the runbook rollback, which restores the pre-backfill snapshot while the unit file still carries `--message-store-db` — and every message write raises `OperationalError` into B1's fatal path. Needs an explicit startup schema check (or `initialize()` on open), and the rollback must remove the flag before restart.

**B3 — Systems is never restarted, so the second writer stays live through cutover.** The runbook installs the Systems commit but says "Do not restart either service yet" (§2) and then starts only `christopher-tgg-hermes` (§5). The attach-only demotion in `store.ts:4126` does not take effect until the Systems service restarts, so the retro-link insert branch keeps creating contaminated rows against the same DB during and after backfill. The runbook also never stops Systems during backfill, so the "pre-backfill snapshot" rollback would silently discard concurrent Systems writes to `tgg.db`.

**B4 — Deep-history export backfill is absent.** Spec §3.3 requires the four WA export zips replayed through the same writer, reusing the importer parser as the feed adapter. The Systems patch deletes `parseWhatsAppExport` and `discoverWaExports` (`src/tenants/tgg/importer.ts`), Hermes `scripts/pa_message_store.py backfill` accepts only `--capture-jsonl`/`--history-jsonl`, and the runbook never mentions the zips. `SOURCE_PRIORITY` reserves `export`/`whatsapp_export` slots that nothing feeds. The ratified deliverable — full chat history — is not delivered and is not declared as an exclusion.

**B5 — "Repair" is a whole-row overwrite that destroys facts and provenance.** In `record_message`, `incoming_wins` replaces `text`, `raw_json`, `source_key`, `sender_id`, `chat_name` wholesale:
- `raw_json` overwrite destroys `raw_json.importProvenance`, the only per-row provenance for the 23,928 existing rows (the importer report explicitly cites it);
- empty `event.text` (media with no caption) overwrites an existing caption — the same empty-text bug class the spec set out to fix, in the other direction;
- `source_key` is rewritten from `chat::id` to `capture:<id>`, mutating a UNIQUE key that Systems attach and observation refs key on.

And the live lane discards the before-image (`durable_jsonl_consumer.py:596` ignores `WriteResult.before_image`), so live repairs are unrecoverable. This must be a field-level merge (prefer non-empty, retain prior raw/provenance, keep prior `source_key` as alias).

**B6 — Unratified source ranking.** `SOURCE_PRIORITY` puts `history-sync` (2) above `export` (1). Spec §2 ranks capture over export only. History-sync payloads are routinely body-less or truncated, so a history replay can overwrite verified export text (compounding B5b). Unknown legacy sources (`whatsapp`, `whatsapp-capture-v1`) silently default to 0 rather than being held.

### Important gaps

- **I1 — Eager description will batch-describe history.** `pending_image_descriptions(eager_only=True)` filters `source='capture'`, but the capture-archive replay writes `source='capture'` for the whole archive. Post-cutover the consumer eagerly describes thousands of historical photos, up to 100 per 2s poll, with vision calls inline in the run loop (`durable_jsonl_consumer.py:2943-2947`) — contradicts "never batch-pre-described" and stalls capture behind vision latency/cost.
- **I2 — Lazy path is unbounded.** `_handle_search` describes up to 20 photos sequentially inside one tool call and then re-runs the search; `_handle_context` up to 41 rows. No cap, timeout, or budget.
- **I3 — Backfill is not resumable as claimed.** `run_backfill` aborts if any artifact exists, so a resumed run needs a fresh directory and snapshots the *already-mutated* DB, destroying the true pre-state. The runbook has no "reuse the original snapshot" instruction.
- **I4 — Backfill holds only `json.JSONDecodeError`/`MessageStoreError`.** `sqlite3.IntegrityError`/`OperationalError` (UNIQUE collision, lock) abort mid-run with partial writes.
- **I5 — `initialize()` does `DELETE FROM pa_message_fts` + full rebuild** on every invocation; it is not safe to run against a live reader. Runbook ordering happens to be correct — make the constraint explicit.
- **I6 — No test exercises a real feed record.** Every fixture is a hand-built flat `{messageId, chatId, timestamp, body, mediaUrls}` (`tests/gateway/test_pa_message_store.py:20-41`). `normalize_event`'s timestamp-key set, nested-item unwrap, and media shape are unverified against the actual `events.jsonl`/`history-sync.jsonl` the writer will be pointed at Sunday. Given B1, this is the highest-risk untested seam. No test covers the live lane when the writer raises.
- **I7 — Media refs are remote URLs.** Capture payloads carry `mediaUrls` like `https://capture.invalid/media/...` (per the Systems fixture); `first_local_image` accepts only existing local files, so eager description likely no-ops and re-scans the same rows every poll forever. Nothing links descriptions to the retention `media_root` that actually holds the bytes.
- **I8 — Systems reversal semantics changed silently.** `tests/tgg-media-convergence.test.ts` flips `{ledgerReversed: 0, conflictsHeld: 1}` → `{ledgerReversed: 1, conflictsHeld: 0}` for the "row referenced by a later message" case, because mutations are now never `inserted`. Confirm the hold existed only to guard row deletion; a test rewritten to match new behavior is not evidence.
- **I9 — Config surface** lives under `pa:` while `pa.enabled: false` in all four deploy configs; the writer is wired by CLI flag and `describe_images` is read regardless. Harmless today, confusing at the next config audit.

### Verified good

Attach-only demotion fails loud (`store.ts:4126`, `MessageRowMissingError` → 409 in `routes.ts`), importer no longer reads or writes message rows and its wholesale guard no longer straddles planes, retrieval omits `media_refs`/`raw_json` and carries `message_id` citations, `bm25()` ascending ordering is correct, `_fts_query` tokenizes and escapes safely, the writer runs before cursor advance and is retry-idempotent, Option B placement and client-agnostic tool naming are honored, and no raw client data or host access appears anywhere in the change or evidence.
