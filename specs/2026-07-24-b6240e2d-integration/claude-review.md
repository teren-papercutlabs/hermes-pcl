# External Review — claude

**Prompt:** Review the attached resolved integration files against review-brief.md. Focus only on conflict-resolution correctness and whether both feature sets survive. Return CLEAR or BLOCKED first, then concise...

---

CLEAR

## Evidence

**Both feature sets are present and mutually consistent in the resolved tree.**

### Main/later features retained
- `PermanentMediaRefusal` defined (`durable_jsonl_consumer.py`); `_event_spreadsheets` present and used in `_retain_record_media_impl`, which imports `validate_tgg_spreadsheet` from `tools.pa_business_tools`. Missing-MIME spreadsheets raise `PermanentMediaRefusal`; `ensure_record_media_retained` converts that to a durable bypass (`record_retention(bypassed=True)`), so it is not retried — matched by `test_permanent_spreadsheet_refusal_is_durable_and_not_retried`.
- Retention gating (`retain_pending_media`), Systems-envelope citation check (`_converge_retained_media` requires `ledgerChanged`/`observationsChanged`), and management full-turn compatibility (`require_response=False` in `process_live_records`) all present.
- Bounded replay provider-error/runtime-config behavior intact: `process_live_records(defer_provider_errors=…)`, `_captured_provider_error`, and `_runtime_config_context` binding `HERMES_HOME` in `run_bounded_backplay`.

### Branch feature contract retained
- `pending_chat_batches` returns `(management, site)` lanes, filtered on `retention_state IN ('complete','bypassed')`, one chat per batch, FIFO by seq.
- Reserved management lanes + site bound in `run_consumer`: `available_site = max(0, site_concurrency - active_site)`; management tasks never take a site slot; `exclude_chats=set(tasks)` prevents concurrent re-claim of an in-flight chat.
- `TGG_DEMO_MANAGEMENT_ONLY` gate present and covered by tests.
- Atomic batch termination via `finish_processed_batch`; source-native reply key `f"{chat_id}::{anchor or 'no-anchor'}"` (media key adds identity+ordinal).
- systemd `ExecStart` is module-based (`python -m gateway.durable_jsonl_consumer run …`) **and** carries `--site-concurrency 4 --chat-batch-size 25`; deployment YAML mirrors `siteConcurrency: 4`, `chatBatchSize: 25`, `scheduler: per-chat-parallel`.

### `hashlib` restoration confirmed
`import hashlib` at top; genuinely used by newer media delivery (`media_identity = hashlib.sha256(...)`), retention identity digests, and `_secret_hash`. Not dead.

### Subtle-interaction checks pass
- **Shared runner concurrency:** single `runner` fanned into per-chat tasks; `GatewayRunner.replay` isolates each call via `self.adapters.task_local(...)` and per-run `replay_context` (both ContextVar/task-local), with unique `live-drain-<uuid>` run_ids for turn attribution. `test_gateway_runner_concurrent_replays_*` asserts adapter/context/session isolation.
- **Retention-before-claim ordering:** capture-lane `retain_pending_media` runs before `pending_chat_batches`; only complete/bypassed rows are schedulable; `_process_claimed_chat_batch` re-runs `ensure_record_media_retained` as an idempotent post-claim safety net that short-circuits on the durable result.
- **Task exception handling:** `_process_claimed_chat_batch` requeues on cancellation/`MediaRetentionError`, marks `failed` and re-raises on genuine errors; delivery runs in a separate swallowing try/except and cannot mutate terminal state. Loop drains done tasks and keeps other lanes alive on retention holds.
- **Reply dedup:** `claim_reply_delivery` (INSERT OR IGNORE) claims before send; unknown/202 outcomes recorded undelivered without blind retry.

No reversion of later behavior detected; tests from both histories coexist in `test_durable_jsonl_consumer.py` and `test_replay_runner.py`.