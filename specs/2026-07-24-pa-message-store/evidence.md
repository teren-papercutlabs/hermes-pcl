# PA Message Store — Local Verification Evidence

Work item: `ab3cd97c-dcbe-4060-869f-2a73230ed3a0`

Scope stayed local. No production host was accessed and nothing was deployed.
All message fixtures were synthetic and created in pytest temporary directories.

Build commits:

- Hermes: `0ac2563aea`
- Systems: `8841d89b30e6c6dfc107974ae7a3364e47d118c0`

## Hermes

```text
TZ=UTC python -m pytest -o addopts= -n 4 \
  tests/gateway/test_pa_message_store.py \
  tests/gateway/test_durable_jsonl_consumer.py -q

63 passed in 23.16s
```

This includes the two-feed CLI backfill fixture, capture-wins overlap,
idempotent admission, conflict holds, photo-description single-write behavior,
media-description search linkage, BM25 sanity, context, existing-schema
migration, and the full durable-consumer suite.

```text
TZ=UTC python -m pytest -o addopts= tests/test_pa_business_facts.py -q

81 passed in 41.44s
```

This verifies the PA business toolset contains the new retrieval tools and
retains the existing PA boundary checks.

```text
python -m compileall -q \
  gateway/pa_message_store.py \
  scripts/pa_message_store.py \
  tools/pa_message_store.py \
  gateway/durable_jsonl_consumer.py

python scripts/plane_lint.py --strict

plane-lint: total 68 (client-token=68), suppressed by baseline: 68, new: 0
plane-lint: OK (strict)
```

## Systems

Run in an isolated worktree based on current `origin/main`:

```text
pnpm test

Test Files  33 passed (33)
Tests       375 passed (375)
Duration    14.40s
```

The suite includes attach-only media convergence, missing-row hard failure,
reconciliation/reversal fixtures, importer plane separation, and all existing
TGG behavior.

## Known unrelated baseline

`tests/cron/test_pa_job_brief.py::test_run_job_selects_pa_brief_and_restricts_toolsets`
expects the scheduler's fourth toolset to be `pa-business`; current runtime
behavior supplies `custom`. It fails unchanged when run alone and is outside
this change. The dedicated PA business suite above is green.
