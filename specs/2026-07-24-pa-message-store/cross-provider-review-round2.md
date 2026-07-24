# External Review — claude

**Prompt:** Round-2 independent merge-gate review after fixes. IMPORTANT authority: the task-specific ratified brief supersedes the older draft spec on backfill inputs: this deliverable explicitly requires exactl...

---

## BLOCK — round 2

The round-1 code blockers are genuinely fixed. One new cutover defect blocks the merge: the deploy manifest does not ship the new modules.

### Blocker

**D1 — The client bundle omits `pa_message_store`, so the reviewed cutover cannot run.** `deploy/tgg/christopher/pa-agent.hermes.manifest.json` `include` (lines 58+) is the file allowlist that materializes `/home/pclaw/apps/hermes-pcl`. It lists `gateway/durable_jsonl_consumer.py` (line 211) but contains no `pa_message_store` entry anywhere (repo-wide, the name appears only in `gateway/`, `tools/`, `scripts/`, tests, and the specs). Consequences of running the runbook as written:

- `gateway/durable_jsonl_consumer.py:39-42` imports `MessageStore` at module import. With `gateway/pa_message_store.py` absent, both import paths fail → the service dies at start, and `bootstrap_runtime.sh:74-78` (`python "$APP_ROOT/gateway/durable_jsonl_consumer.py" init-cursor`) fails during bootstrap.
- `tools/pa_message_store.py` absent → `messages_search`/`message_context` never register on the host even though `toolsets.py` lists them (discovery is a glob over the deployed `tools/` dir, `tools/registry.py:57-74`).
- `scripts/pa_message_store.py` can never be included: `build_pa_agent_manifest.py:32-41` has no `scripts/` package root, so runbook §3's `"$HERMES_APP/scripts/pa_message_store.py" backfill` has no file on the host.

`validate_deployment_spec.py:378-397` only checks required ⊆ include, so the stale manifest passes validation silently. Fix: regenerate the manifest (the generator rules already accept `gateway/**` and `tools/**`) and decide a shipped location for the backfill CLI, then re-point the runbook at it.

### Verified fixes (repo, not summary)

- **Live per-record holds + continuation:** `stage_from_source` calls `record_or_hold` (`durable_jsonl_consumer.py:616-629`); held rows are inserted with `status='skipped'` + `last_error='message-store-held'` (639-666), which is a legal status (`CHECK` at 462), the cursor advances (675-686), and `pa_message_holds` is durable with `UNIQUE(source,record_sha256)`. Covered by `tests/gateway/test_pa_message_store.py:298`.
- **Startup schema assertion:** `run_consumer` calls `assert_ready()` before the singleton lock (2827-2830); `PA_MESSAGE_STORE_NOT_INITIALIZED` names the missing tables/columns (`pa_message_store.py:320-345`).
- **Field-level merge, no provenance loss:** canonical `source_key` preserved (518, asserted by the capture-wins test), `raw_json` merged with `importProvenance` pinned to the prior value (510-514), empty incoming text cannot clobber a stored caption (529-533), aliases accumulate and cross-owner aliases raise → held (457-460, 575-584).
- **Eager descriptions are admission-scoped:** only the current cycle's `admitted_message_ids`, capture-source only (`_describe_ingress_images:123-126`, wired at 2989-2993). No archive-wide batch describe.
- **Lazy bounded:** `MAX_LAZY_DESCRIPTIONS_PER_CALL = 3` in both handlers (`tools/pa_message_store.py:19,102,134`).
- **Two-feed capture-wins replay:** priority capture=3 > history-sync=2 > legacy/export (36-44); `normalize_event` rejects unknown sources; backfill snapshots *before* `initialize()` (`scripts/pa_message_store.py:54-60`), matching the runbook's claim.
- **Runbook two-service cutover:** both services stopped before backfill, code restored *before* the DB on rollback (removing `--message-store-db` before the old schema returns), explicit no-rerun-over-partial-state instruction.
- **One writer / attach-only:** `store.ts:4126` throws `MessageRowMissingError` → 409; importer no longer reads or writes message rows; `tgg_message_ledger_sync.py` is a fail-loud tombstone. (Systems source itself is not in this worktree; verified from the supplied patch only.)
- **Retrieval:** BM25 ascending, hard filters, `message_id` citations, no `media_refs`/`raw_json` in results, `set_description_once` writes exactly once. Direct-file entrypoint fallback (`durable_jsonl_consumer.py:39-42`) is genuinely required by bootstrap and resolves correctly. No raw client data in the diff; nothing deployed.

### Important, non-blocking

1. Held records sit at `status='skipped'`, so they are excluded from processing *and* from media retention (`retention_candidates` requires `status='pending'`, line 1149). Their capture media is never retained — drain holds inside the capture-store retention window.
2. `has_media`/`media_refs` are rewritten regardless of priority (495, 565). A row with `has_media=1` and empty `media_refs` folded against a media-less history record flips `has_media` to 0, which also removes it from description candidacy (610-621).
3. Only `MessageStoreError` is held live; `sqlite3.OperationalError`/`IntegrityError` still crash the unit (no data loss — cursor unmoved — but a full ingest stall). The backfill lane holds `IntegrityError` but not `OperationalError`.
4. Systems attach now hard-fails without a PA row → retention held → those messages never reach `pending()`. Any consumer started without `--message-store-db` stalls all media messages. `verify_runtime.sh` guards the deployed unit; add a runbook preflight assert.
5. No test covers the merge-preservation fixes (importProvenance retention, empty-text non-overwrite) or the eager admitted-only path; the Hermes suite moved 63→64. Every fixture is still hand-built, so `normalize_event` is unverified against a real `events.jsonl`/`history-sync.jsonl` record — worth one recorded shape check on the host before backfill.
6. `pa_message_aliases`' `ON DELETE CASCADE` never fires from the Python writer (no `PRAGMA foreign_keys=ON`); orphan aliases if a row is deleted out of band.
