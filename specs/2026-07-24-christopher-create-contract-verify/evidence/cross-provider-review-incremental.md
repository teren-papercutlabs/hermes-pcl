# External Review — claude

**Prompt:** Incremental blocking review after the prior CLEAR on the Christopher/TGG create-contract fix. This small follow-up adds Baileys protobuf-Long timestamp handling, strips model-supplied observed_at/obse...

---

Reviewed the two code changes against the worktree, the refreshed evidence, and the systems-side consumers.

**Code**
- `gateway/run.py:15061` — dict branch is confined to the local `epoch()` inside `_event_source_message_timestamps`; it only widens what previously returned `None`, so no path that used to bind a timestamp changes. Matches the existing convention in `gateway/platforms/whatsapp.py:815,1820`. New unit test `tests/gateway/test_source_message_timestamps.py:26` pins it; the bundle and session-scoping tests are unchanged.
- `tools/pa_business_tools.py:1834` and `:2114` — both observation entry points (direct handler and the `pa_business_write` → `_bind_observation_source_refs` route) now drop `observed_at`/`observedAt` from `fields`, and both still set top-level `observedAt = _observed_at_for_source_refs(refs)` (`:1850`, `:2123`). No `KeyError` risk at `:2115` since `payload["fields"]` is guaranteed present when `fields is not None`. Create path (`:1894`) and the citation gate are untouched.
- Downstream: `systems/src/tenants/tgg/routes.ts:1402` reads `body.observedAt ?? body.observed_at ?? fields.observed_at`, so the persisted `case_observations.observed_at` was already governed by the bound top-level value — the strip cannot change stored state, only the evidence blob. No consumer of `fields.observed_at` remains for Christopher writes.

**Evidence**
`comparison.json` all checks `pass: true`; `observed_at.actual_epochs` = `[1784545200, 1784545200]` against the expected epoch; `replay_dedupe.persisted_observations: 1`; due date 1787068800 (+30d), `static_plus_7_present: false`. `replay-observation.json` attempt 2 shows a model-supplied `observed_at: 1784545200` in the tool input and no `observed_at` in the persisted fields — the strip is exercised end to end. Systems suite committed green (378 passed, typecheck clean).

Non-blocking notes:
1. `epoch()` ignores the `high` word. A Long with `high != 0` (e.g. a millisecond value) would now bind a small bogus epoch silently instead of failing closed; `low + high*2**32`, or refusing when `high` is set, keeps the fail-closed property. `or` also treats `low == 0` as absent (immaterial).
2. Sibling parsers accept a `seconds` key (`gateway/replay.py:1029`, `gateway/durable_jsonl_consumer.py:2273`, `gateway/platforms/telegram.py:5105`); `run.py` does not, so that shape still yields no timestamp and hard-refuses every write in the turn. Pre-existing and fail-closed, but one word from parity.
3. `_bind_observation_source_refs` now matches the direct handler on `observed_at` but still does not strip model-supplied `media_refs`/`photo_count` that `pa_business_tools.py:1840` pops — visible as `media_refs: []`, `photo_count: 0` in `label-drift.json`/`replay-observation.json`. Pre-existing divergence, not introduced here.
4. No unit test pins the `fields` strip on either path (gate evidence only). Low stakes given the route precedence above.
5. Evidence asymmetry: the systems side commits a full vitest log, the Hermes side commits only a one-line prose summary (`hermes-focused.txt`) plus a note that the first full-suite log was destroyed; the uncommitted `evidence/hermes-tests.txt` is truncated at ~0% with no summary line. The "known pre-existing failure" claim is itself substantiated by `hermes-origin-known-failure.txt`.

No blocking correctness, contract, concurrency, or regression defect found in this increment.

CLEAR