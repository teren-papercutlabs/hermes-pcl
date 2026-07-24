# b6240e2d → main conflict-resolution report

Merge commit: `e334ca80d8708aa78df359e1786b3df9d444ed2f`
Parents: current main `2f9a481ef`; source branch `98effd45f`.

The resolved tree intentionally matches current main at merge time. Main had already gained the scheduler contract while adding xlsx admission, bounded replay, retained-media gating, citations, and manager full turns. The merge therefore records source ancestry without replacing newer combined code with the older branch snapshots.

## Pick-one-side hunks (all 29)

1. `client-agent-deployment.yaml` consumer fields — kept main `retentionBatchSize: 25`; branch omitted it. Scheduler fields on both sides remained.
2. `verify_runtime.sh` status assertions — kept main retention/media-root/headroom checks; branch omitted them. Scheduler assertions immediately above remained.
3. `christopher-tgg-hermes.service` ExecStart — kept main module invocation (`-m gateway.durable_jsonl_consumer`) rather than the older file path. It already contains `--site-concurrency 4 --chat-batch-size 25`.
4. `durable_jsonl_consumer.py` pending-row query — kept main retention completion/bypass gate; branch would admit rows before retained-media processing.
5. `durable_jsonl_consumer.py` bounded-window/readjudication methods — kept main-only bounded replay APIs; branch omitted them.
6. `durable_jsonl_consumer.py` retention methods — kept main-only retention candidate/result/audit methods; branch omitted them.
7. `durable_jsonl_consumer.py` `process_live_records` signature — kept main `defer_provider_errors`; required by bounded replay retry semantics.
8. `durable_jsonl_consumer.py` reply delivery key — kept main media-aware SHA/ordinal key plus text source-native key; branch had text-only key. This is additive to source-stable reply anchoring.
9. `durable_jsonl_consumer.py` runner factory — kept main explicit-config-capable factory and runtime-home context; branch had no config argument.
10. `durable_jsonl_consumer.py` claimed batch preflight — kept main idempotent retained-media safety net before Hermes; branch omitted it.
11. `durable_jsonl_consumer.py` claimed batch exception path — kept main `MediaRetentionError` requeue; branch would terminal-fail the row.
12. `durable_jsonl_consumer.py` run arguments — kept main `retention_batch_size`; branch omitted it.
13. `durable_jsonl_consumer.py` completed task handling — kept main per-chat retention-hold isolation; branch would raise and kill the whole daemon.
14. `durable_jsonl_consumer.py` standby status — kept main retention metrics; branch omitted them.
15. `durable_jsonl_consumer.py` standby scheduler metadata — kept main `retention_batch_size`; branch omitted it.
16. `durable_jsonl_consumer.py` active status/headroom — kept main headroom refusal + retention state; branch had only plain running state. Scheduler metadata remains below.
17. `durable_jsonl_consumer.py` running metadata — kept main `retention_batch_size`; branch omitted it.
18. `durable_jsonl_consumer.py` capture-lane retention cycle — kept main pre-scheduler retention cycle; branch omitted it.
19. `durable_jsonl_consumer.py` scheduler selection/runner init — kept main precomputed available site lanes, demo-only suppression, and lazy runner creation; branch recomputed later and eagerly created a runner. Reserved management lanes remain.
20. `durable_jsonl_consumer.py` site loop — kept main `selected_site_batches`; branch repeated availability slicing inline.
21. `durable_jsonl_consumer.py` once-mode task collection/status — kept main retention-error isolation and retention-aware status; branch raised every task exception and omitted retention status.
22. `durable_jsonl_consumer.py` once-mode metadata — kept main retention batch/cycle metrics; branch omitted them.
23. `durable_jsonl_consumer.py` steady-state status — kept main retention-aware `held-pending` state; branch always wrote `running`.
24. `durable_jsonl_consumer.py` steady-state metadata — kept main retention batch/cycle metrics; branch omitted them.
25. `durable_jsonl_consumer.py` CLI parser — kept main `--retention-batch-size`; branch omitted it.
26. `gateway/run.py` replay context local — kept main `ctx = None` initialization for safe exception-path capture; branch omitted it. Branch replay isolation via `with replay_context(plan)` remains.
27. `test_durable_jsonl_consumer.py` imports — kept main `os` and `zipfile` additions alongside `sqlite3`; branch lacked them.
28. `test_durable_jsonl_consumer.py` post-scheduler tests — kept main bounded/provider/readjudication tests; branch side was empty. Branch scheduler tests earlier in the file remain.
29. `test_replay_runner.py` pre-concurrency test — kept main captured-outbound-on-exception test; branch side was empty. The branch concurrent replay-isolation test immediately following remains.

## Merge-specific correction

The older branch removed `hashlib`; newer main media retention and media reply identity still call it. The integration restored the main import before tests. No other merged-tree delta from current main remained.

## Verification outcome

- Focused conflict/feature tests: `99 passed` (`test_durable_jsonl_consumer.py`, `test_replay_runner.py`, `test_consumer_reply_delivery.py`).
- Required sequential full selection: `5706 passed, 54 skipped, 45 failed` in 2060.44s.
- Failure-only rerun: 11 order/transient failures cleared; 34 failed persistently.
- Detached `origin/main` baseline rerun of those 34: the same 34 failed. This proves the integration introduced zero test failures, but the brief's stricter acceptance condition (only the named baseline failures may remain) is not met because current main carries 31 additional persistent failures outside the integration surface.
- Of the four named expected failures, three still reproduce (`test_state_claim_gate_in_both_job_briefs`, both replay-preflight tests); `test_tgg_management_defaults_to_operator_db_before_ilinked` passed on current main.
