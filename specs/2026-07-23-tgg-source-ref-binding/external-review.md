# External Review — claude

**Prompt:** Cold review this TGG observation source-ref binding change. Verify: shared middleware (not TGG tool schema) requires explicit model-cited refs; cited refs must be a subset of current-turn inbound refs...

---

I've verified the committed code against the patch and traced all entry points. Findings below.

## Verdict: CLEAR (with residual risks the driver must weigh before deploy)

The change matches the teren-ruled shape. All required properties hold:

**Verified correct**
- **Shared-path gate.** The binding/validation lives in `_bind_observation_source_refs` (`tools/pa_business_tools.py:2010`), invoked behind the client-agnostic `_is_observation_write_operation` predicate (`:533`) at both generic entry points: `_handle_business_call:1960` and `_handle_business_write:984`. No `tgg_case_*` *schema* changed (schema at `:2497` already required `sourceRefs` minItems 1 pre-change). `test_second_client_observation_uses_shared_ref_validator` confirms `mofex_case_observation` traverses it unmodified. ✔ Meets the hard acceptance criterion.
- **Subset, not whole-turn.** `normalized["sourceRefs"] = cleaned` (`:2055`) binds exactly the model's cited, placeholder-stripped, deduped refs. The old `len(cleaned)==len(collected)` pass-through and the `_current_turn_source_refs()` auto-staple fallback are gone. ✔
- **Empty / placeholder / outside-turn all fail closed.** Empty → `SOURCE_REFS_REQUIRED` (`:2030`); outside-turn → `SOURCE_REFS_OUTSIDE_CURRENT_TURN` naming the invalid refs (`:2041`); `current_turn` still stripped via `_filter_placeholder_source_refs` (`:2003`). Raised as `ValueError` → `tool_error`, `captured == {}` in tests (no backend write). ✔
- **Direct + generic both traverse.** Direct handler `_handle_tgg_case_observation:1828` calls the same function; `_handle_tgg_write` does not re-bind, so no double-binding. ✔
- **Media derivation preserved.** `_handle_tgg_case_observation` still strips `mediaRefs/media_refs/photoCount/photo_count` (`:1850`) and maps cited `sourceRefs → fields.source_refs`; `test_direct_observation_keeps_cited_photo_refs_for_media_derivation` confirms cited photo refs survive for server-side derivation while the unrelated photo is excluded. ✔
- **Constitution.** Snippet now says cite EVERY message actually used (report + each used photo), "not every message batched into the turn," grouping = model judgment, no deterministic parsing. `build_runtime_slots.py` `_validate` asserts the new text and asserts the old auto-bind text is absent. ✔

**Residual risks / gaps (not implementation defects — surface to driver)**

1. **Contamination prevention is now purely model judgment; the middleware only bounds refs to the turn.** A model that cites *all* same-turn messages (including the unrelated photo) passes validation — every ref is a current-turn member — and reintroduces exactly the reported defect (unrelated photos stapled to a case). This is inherent to the mandated "no deterministic grouping" shape, so it's by-design, but it means the fix's core guarantee rests on the constitution + model behavior with only two hand-authored fixtures on `gpt-5.6-luna` as evidence. That is thin for a probabilistic behavior. Confirm the production Christopher slot model matches `gpt-5.6-luna` (the sandbox model), and consider more adversarial fixtures (e.g., interleaved two-case turns, photo-before-report ordering).

2. **New hard dependency on `HERMES_SESSION_SOURCE_MESSAGE_REFS` for every observation write.** Old behavior let a model citing real refs pass through *without* current-turn validation; new behavior requires membership. If any legitimate write context leaves that contextvar empty (cron/background writes, import replays, or the recently added "resolve manager answers through full agent turns" path — commit 3b5fd8101), *every* cited ref becomes `OUTSIDE_CURRENT_TURN`, the write is refused, and the retry nudge ("cite only ids from the current inbound turn") is unsatisfiable → potential per-turn livelock. No test or fixture covers the refs-empty runtime context. Confirm every observation-write path populates `source_message_refs` before deploy.

3. **Baseline gate honestly reported as unclean.** `test-results.md` documents 4 excluded failures claimed pre-existing on `origin/main`; I cannot run tests to confirm. The author correctly states deployment stays held under conditional auth. Driver must independently confirm those 4 reproduce on main and are unrelated before regenerating slots.

**Fixture harness safety:** credible. `run_isolated_smoke.py` uses a loopback `_OperatorStub`, records `client_mutation_requests=0` / `external_outbound_sent=0`, and the new `--fixture-file` path validates each line is an object with `messageId` before replay. Reports show narrowly-scoped persisted `source_refs` excluding the unrelated same-turn message in both runs. Nothing outbound.

Net: implementation is correct and matches the mandated design; the deploy-blocking questions are model-behavior confidence (risk 1) and the refs-empty context (risk 2), both of which the driver should close before lifting the held gate.