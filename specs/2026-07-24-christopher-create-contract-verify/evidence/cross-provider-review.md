# External Review — claude

**Prompt:** Independent blocking review of a two-repository Christopher/TGG case-create fix. Check the exact mission: (1) explicit priority/contactName/contactPhone/work_items/receipt/dueAt, nothing operational b...

---

I reviewed the Hermes side against the actual worktree (helpers `_coerce_observed_at_epoch`, `_validate_cited_turn_source_refs`, `_observed_at_for_source_refs`, `_current_turn_source_timestamps`, `_prepare_case_create_source_refs` all present and internally consistent) and the Systems side from the patch text. Walking the seven mission checkpoints:

1. **Explicit fields** — `TGG_CASE_CREATE_SCHEMA` promotes `priority/contactName/contactPhone/work_items/receivedAt/evidenceMessageRefs` to top-level with `additionalProperties: false`; route (`routes.ts:1733-1758`) forwards each into `decideAgentCase`. Evidence create body carries them top-level, only descriptive text under `evidence`. ✓
2. **Per-case citation** — `_validate_cited_turn_source_refs` refuses empty, out-of-turn, and timestamp-less refs for both create and observation. Concurrency evidence shows SK/PG creates correctly refused (`source refs are outside the current turn`) while AMK bundle bound both ids. ✓
3. **Mechanical observedAt** — create and observation both set `observedAt = _observed_at_for_source_refs(refs)` (max cited ingress epoch); model `observedAt` dropped from required schema; both replay attempts bind `1784545200`. ✓
4. **Route→store** — route forwards `receivedAt/observedAt/dueAt/workItems`; `createCase` persists `job_receipt_date=receivedAt`, `due_at=receivedAt+30d`, and the created branch emits the opening observation with `work_items` and `observedAt` inside the `decideAgentCase` transaction (`bobby-api.test.ts` asserts all of this). ✓
5. **+7d→+30d** — single `MAINTENANCE_DUE_SECONDS` constant replaces every `604800`/`7*86400` due fallback in `toSummary`, `caseListOrderBy`, `getAnalyticsSummary`, `EFFECTIVE_MAINTENANCE_DUE_SQL`, `createCase`; `static-store.txt` confirms the only surviving `7*86400` (line 5083) is a recency lookback. Code-only, no `UPDATE`. ✓
6. **Idempotency + race** — pre-check SELECT on `refs:<sorted-json>` key, partial `UNIQUE INDEX ... WHERE source_ref LIKE 'refs:%'`, and try/catch that re-resolves to the existing row on constraint violation. Order-independent (`.sort()`) and dedup-normalized; replay test asserts one row. ContextVar-scoped refs/timestamps prevent cross-turn clobber under the parallel scheduler (evidenced by clean 4-turn cross-chat run). ✓
7. **No deploy/client data** — synthetic fixtures, loopback stub, WhatsApp disabled, capture-only replay. ✓

Schema migration is safe: the `refs:` prefix is new to this patch, so no pre-existing row (all prior `source_ref` were NULL) matches the partial-index predicate — `CREATE UNIQUE INDEX IF NOT EXISTS` cannot fail on existing duplicates.

Non-blocking notes (not defects): `createCase` now sets `due_at=receivedAt+30d` irrespective of `service_line`, so a non-maintenance agent-created case would gain a due date `toSummary` treats as overdue-eligible — out of Christopher's maintenance-only scope; a turn whose ingress timestamps can't be extracted now hard-refuses all writes (happy path validated across single/bundle/concurrency shapes); model-supplied `fields.observed_at` is still stored alongside the authoritative bound `observedAt` (cosmetic).

No blocking correctness, race, migration, regression, or test-coverage defect found.

CLEAR