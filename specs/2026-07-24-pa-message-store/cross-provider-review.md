# External Review — claude

**Prompt:** Round-3 independent merge gate. Task authority: backfill is exactly two feeds, capture + history-sync; exports are superseded/out of scope. Review cumulative Hermes v3 and Systems patches, not summari...

---

## CLEAR — round 3

The round-2 deploy-bundle blocker is genuinely fixed at the generator level, and the round-1/2 code fixes are intact in the working tree (not just in the patch).

### Round-2 blocker D1 — fixed and durable

- `deploy/tgg/christopher/scripts/build_pa_agent_manifest.py:27` adds `scripts/pa_message_store.py` to `TOP_LEVEL_RUNTIME_FILES`. This is the canonical generator, so the fix survives regeneration rather than being a hand-edited manifest.
- Regenerated manifest contains all three modules in correct sorted position: `gateway/pa_message_store.py` (manifest:215), `tools/pa_message_store.py` (581), `scripts/pa_message_store.py` (509). `gateway/__init__.py` (204) and `tools/__init__.py` (510) are present, so `scripts/pa_message_store.py:13-17` (`parents[1]` → app root → `from gateway.pa_message_store import …`) resolves on the host.
- Drift can't silently reopen: `deploy_runtime.sh:8,29` runs the builder with `--check`, which raises `pa-agent runtime file set drifted` (`build_pa_agent_manifest.py:121-125`). The permissive `required ⊆ include` check in `validate_deployment_spec.py:379-397` is no longer the only gate.
- Both import paths for the consumer now resolve: `-m gateway.durable_jsonl_consumer` (unit line 20, `WorkingDirectory=/home/pclaw/apps/hermes-pcl`, line 11) and the direct-file bootstrap entrypoint via the `pa_message_store` fallback (`durable_jsonl_consumer.py:39-42`).

### Round-1/2 fixes re-verified in the repo

- Per-record hold + continuation: `stage_from_source` calls `record_or_hold` before inbox/cursor admission (`durable_jsonl_consumer.py:613-629`); held rows insert as `status='skipped'`, `last_error='message-store-held'` (653-666), a legal status per the `CHECK` at 462. Column/placeholder/value arity is 15/15/15 — correct.
- Startup fail-closed: `assert_ready()` before the singleton lock (2827-2830); `PA_MESSAGE_STORE_NOT_INITIALIZED` names missing tables and columns (`pa_message_store.py:321-346`).
- No provenance loss on merge: canonical `source_key` pinned (519), `importProvenance` preserved from the prior row (514-515), empty incoming text cannot clobber a stored caption (530-534), cross-owner aliases raise → held (578-583).
- Eager descriptions stay admission-scoped and capture-only (`_describe_ingress_images:123-126`); lazy path bounded at `MAX_LAZY_DESCRIPTIONS_PER_CALL = 3` (`tools/pa_message_store.py:19`).
- Two-feed-only backfill matches task authority: `run_backfill` snapshots before `initialize()` (`scripts/pa_message_store.py:54-60`), then capture → history-sync (63-76); `normalize_event` rejects unknown sources (139-140); export sources exist only as low-priority legacy fold targets, and the Systems patch deletes `parseWhatsAppExport`/`discoverWaExports` and the `waExports` input entirely.
- One writer: Systems `store.ts:4126` throws `MessageRowMissingError` → 409 (`routes.ts:1326-1328`); `tgg_message_ledger_sync.py` is a fail-loud tombstone. (Systems source is not in this worktree; verified from the supplied patch only.)
- Unit + verify guard: `--message-store-db …/tgg.db` in `christopher-tgg-hermes.service:20`, asserted by `verify_runtime.sh:50-51`. Runbook stops/starts both DB consumers and restores code before the database on rollback.
- No raw client data in the diff; nothing deployed.

### Standing non-blocking items (unchanged from round 2, accepted)

1. Held rows sit at `status='skipped'` and so are excluded from media retention (`durable_jsonl_consumer.py:1149` requires `status='pending'`) — drain holds inside the capture retention window.
2. `has_media`/`media_refs` are rewritten regardless of priority (496, 566-569); a media-less history fold can flip `has_media` to 0 and drop the row from description candidacy.
3. Only `MessageStoreError`/`IntegrityError` are held; `sqlite3.OperationalError` still crashes the unit (cursor unmoved, so no data loss, but a full ingest stall).
4. `normalize_event` is still exercised only against hand-built fixtures — do one recorded shape check against a real `events.jsonl` and `history-sync.jsonl` record on the host before backfill.
5. `pa_message_aliases`' `ON DELETE CASCADE` never fires from the backfill/verify CLI paths that use read-only connections; out-of-band row deletion leaves orphan aliases.

None of these change correctness of the cutover as written or block the merge.