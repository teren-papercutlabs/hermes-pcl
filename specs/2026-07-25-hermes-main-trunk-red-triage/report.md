# hermes-pcl main trunk-red triage

**Investigation time:** 2026-07-25 09:38–09:47 SGT  
**Repository:** `teren-papercutlabs/hermes-pcl`  
**Fetched main:** `bcff03f4bacb0a3c63d7bddda03e27e164ec10ac`  
**Tests evidence:** workflow run `30132415288`, head `505c9e8df4`; comparison run `30069951359`, head `2f9a481eff`  
**Lint evidence:** workflow runs `30132415302` (`505c9e8df4`) and `30069951335` (`2f9a481eff`)  
**Bounds observed:** read-only Git/GitHub investigation. No source fix, push, PR mutation, client-host access, or live-client-state mutation.

## Verdict

Main is **genuinely red as a release gate, but the red is pre-existing and was not introduced by the overnight de-fusion landing**. Both complete Tests runs have the **same 57-node failure set**: 55 source/test-contract failures (`REAL` for triage purposes) and 2 CI-environment failures (`INFRA`). Set comparison is exact: 57 before, 57 after, zero added, zero removed.

The 55 REAL nodes do **not** mean 55 independent product regressions. They collapse to 17 REAL root-cause clusters, plus one INFRA cluster. Most are stale fixtures, obsolete assertions, or parallel-test isolation defects. Three clusters require an explicit behavior call rather than blind test editing: the TGG state-claim instruction, ops-ingest compaction contract, and bounded-dry-run physical-byte assertion. The lint workflow is independently red on the same single pre-existing Ruff violation.

**Sunday read:** green before Sunday is realistic if a focused fix pass starts today: roughly **4–6 engineering hours plus two full CI cycles**. It is not a 57-fix project. The work is mostly mechanical, but it should not be waved away as noise because (a) the release gate is actually red, (b) a few assertions protect PA behavior, and (c) the async/replay failures expose real test-isolation defects. A safe plan is one source branch, resolve the three behavior calls first, batch the mechanical repairs, run focused tests, then the 14–16 minute full suite and lint, followed by the required cross-provider review.

## Timeline and causation

### What the CI history proves

| Time (SGT) | Head | Tests result | Failure set |
|---|---|---:|---|
| 2026-07-24 13:37 | `2f9a481eff` | failed | 57 failures / 23,691 passed / 153 skipped |
| 2026-07-25 06:56 | `505c9e8df4` | failed | 57 failures / 23,698 passed / 153 skipped |

The two sets are node-id identical. Therefore every failure listed below was already present before the de-fusion window, and the tested de-fusion landing introduced **zero observed new failures**.

Between those heads, main received:

- `35196fc867` — plane-fence/manifest work
- `5531250691` — manifest cleanup
- `5a5a6bb2d9` — explicit replay tenant context (`[WB:e49bfc29]`)
- `93da88bd57` — docs
- `505c9e8df4` — docs/evidence (`[WB:e49bfc29]`)

Only `5a5a6bb2d9` changed runtime replay code/tests. It changed `gateway/replay.py`, `gateway/replay_orchestrator.py`, `hermes_cli/replay.py`, and their replay-runner/orchestrator tests; none is in the 57-node failure set. Git blame on the failing assertions likewise points to earlier commits, not these de-fusion commits. No main commit tagged `[WB:709f9a23]` or `[WB:ab3cd97c]` existed after the fetch, so those WBs cannot be assigned causation on the current main history.

The lint result is also identical before/after: one Ruff `unspecified-encoding` violation at `deploy/tgg/christopher/scripts/attention_digest_watcher.py:62`, introduced by `27163c425b` on 2026-07-20. De-fusion did not create it.

**Causation classification:** pre-existing red only; no de-fusion-introduced failures observed in either Tests or Lint.

## Root causes and fix shape

| Cluster | Nodes | Root cause | Obvious fix shape | Nature |
|---|---:|---|---|---|
| PA cron toolset name | 1 | The live constitution now enables `custom`; the test still expects retired `pa-business`. Actual result is `memory,file,web,custom`. | Update the assertion to the live toolset contract (and decide whether `web` should remain expected for this fixture). | Mechanical contract drift. |
| Bounded dry-run bytes | 1 | The test uses raw SQLite main-file bytes/mtime as the read-only oracle while the DB is WAL-backed. Opening/closing read connections can checkpoint/rewrite the physical main file without a logical row mutation; the diff is SQLite representation/schema pages, not changed selected-row state. | Replace byte identity with a stable logical-state assertion, or snapshot/checkpoint before taking the physical baseline. Preserve the real invariant: selected rows/counts/statuses unchanged and zero sends/writes. | Small design call; do not simply delete the gate. |
| Gateway `_set_session_env` signature | 11 | `gateway/run.py` now calls `_set_session_env(context, event=event)`; 11 test runners monkeypatch one-argument lambdas. | Update shared fixtures/lambdas to accept `event=None`/`**kwargs`, then keep their original assertions. | Mechanical fixture drift. |
| Streaming TTS/media fixtures | 3 | `_deliver_media_from_response` now invokes `adapter.extract_attachment_images`; the `SimpleNamespace` test adapter omits it. The broad extraction guard catches the `AttributeError`, so no media send occurs. | Add `extract_attachment_images` (and the current adapter surface) to the fake, ideally use a shared spec-backed fake to stop repeat drift. | Mechanical fixture drift. |
| Vision fixture | 3 | `_enrich_message_with_vision` calls `_vision_preanalysis_max_concurrency`; the `_Stub` bypasses `GatewayRunner` construction and omits the method. | Seed/bind the helper on the stub or construct a minimal real runner. | Mechanical fixture drift. |
| WhatsApp replay isolation | 11 | Replay messages are all filtered before capture (`processed == 0`). `WhatsAppAdapter` reads `WHATSAPP_GROUP_POLICY`, but the autouse hermetic fixture clears many WhatsApp variables and omits group policy/allowlist variables. Another config-loading test can leak `allowlist` into the xdist worker, making this file order-dependent. | Clear `WHATSAPP_GROUP_POLICY` and group-allowlist variables in the hermetic fixture and/or explicitly set `group_policy: open` in this file's adapter factory. Add a control asserting the effective group policy. | Mechanical test-isolation defect. |
| System-service home tests | 2 | Tests simulate `Path.home() == /root`; `generate_systemd_unit()` probes `/root/.hermes/...` with `Path.is_dir()` before remapping to the target user, and the hosted runner cannot traverse it. | **INFRA classification.** Make the path probe tolerate `PermissionError` or patch `get_hermes_home`/the path builder in the fixture. The target-home behavior is not what failed. | CI/env; no runtime regression shown. |
| WhatsApp setup ordering | 1 | Production code writes `WHATSAPP_ENABLED=true` on the existing-pairing early return. The test patches `Path.exists` and config/env helpers incompletely across the newer profile-aware home/config path, so its assertion reads a different env surface. | Patch the canonical `get_hermes_home`/`save_env_value` surface or assert through `get_env_value` under the isolated profile. | Mechanical fixture/path drift. |
| Auxiliary client cache key | 1 | `_client_cache_key` gained `pool_hint`; the test manually inserts an old 8-field key, while `_get_cached_client` looks up the current 9-field key. It then leaves the manually inserted old tuple untouched, so the test reads `old-model`. | Build the key with `_client_cache_key()` instead of hard-coding tuple shape. | Mechanical fixture drift. |
| PA source-ref binding | 5 | `_current_turn_source_refs()` moved from process env to task-local `gateway.session_context.get_session_env` for concurrent-turn safety (`61b137951e`). Tests still use `monkeypatch.setenv`, so no refs bind; generic payloads lack `sourceRefs`, and direct writes return an error instead of `ok`. | Set/reset the session contextvar in fixtures; do not restore process-env reads and reintroduce cross-turn leakage. | Mechanical fixture drift protecting a real concurrency fix. |
| Spreadsheet size gate fake | 1 | `validate_tgg_spreadsheet()` now checks `Path.is_file()` before size. The monkeypatched `Path.stat()` result has only `st_size`; `is_file()` needs `st_mode`. | Give `Oversized` a regular-file `st_mode`, or patch only the size read with a spec-complete stat result. | Mechanical fixture drift. |
| TGG state-claim gate | 1 | The test requires the “THIS turn's tool result” instruction in both job briefs. The recovered production constitution (`94b62b29cc`) dropped it from `tgg_ops_ingest`; management still carries equivalent state discipline elsewhere. | Decide deliberately: restore the state-claim sentence to ops ingest (safer default) or narrow/retire the assertion with evidence that ops ingest cannot assert state. | Judgment-bearing PA behavior. |
| TGG ops-ingest compaction | 1 | Test says ops ingest compression is guidance-only and must not declare a `strategy`; current constitution declares `strategy: preserve-case-state`, added by recovered spec `94b62b29cc`. | Reconcile the intended contract: remove `strategy` if runtime policy must not be client-authored, or update test/runtime loader together if the strategy is now legitimate. | Judgment-bearing PA/runtime boundary. |
| TGG management operation name | 1 | Test expects obsolete text `operation case_search`; constitution correctly instructs `operation tgg_case_search` and the bridge exposes the tgg-prefixed operation. | Update assertion to `operation tgg_case_search`. | Mechanical obsolete assertion. |
| Christopher replay preflight args | 2 | `_validate_replay_args` reads `args.prod_pilot_run_id`; the `_args()` test factory predates that parser field. | Add `prod_pilot_run_id=None` to the factory (prefer deriving defaults from the parser). | Mechanical fixture drift. |
| TUI toolset defaults | 2 | `inter_session` became a non-configurable base toolset; fallback now returns `inter_session,kanban,memory`, while tests expect only `kanban,memory`. | Update expected set if inter-session is intentionally universal; otherwise change the base-toolset policy. Current history (`2bf1ef2b39`) indicates intentional addition. | Mechanical contract drift. |
| Async delegation shared queue | 8 | The async worker completes, but a live TUI completion watcher in the same pytest process drains the global `process_registry.completion_queue` first. CI stdout shows the stolen event immediately re-entering a TUI session and failing on its fake agent. Tests then time out on an empty queue. | Stop/join the watcher in TUI test teardown or inject a per-test queue/registry; do not lengthen sleeps. Add ownership isolation around the global consumer. | Real parallel-test isolation defect; mechanical fix, moderate verification. |
| xAI dotenv fallback | 2 | xAI STT credential lookup moved to `tools.xai_http.resolve_xai_http_credentials`; tests still patch `hermes_cli.config.load_env`, which no longer intercepts the resolver's imported `get_env_value`. | Patch `tools.xai_http.get_env_value` or exercise a real temporary Hermes `.env` through the canonical resolver. | Mechanical fixture drift. |
| Ruff lint | 1 lint finding | `Path.read_text()` at `attention_digest_watcher.py:62` lacks explicit encoding. Same finding at both heads. | Add `encoding="utf-8"`; run Ruff. | Mechanical, minutes. |

## Fix scope and sequencing

Recommended fix pass (separate WB/branch, with cross-provider review before merge):

1. **Lock the three behavior calls first:** state-claim sentence, ops-ingest compaction ownership, and the bounded-dry-run oracle. This prevents a mechanical “make tests green” pass from deleting real protection.
2. **Repair shared fixtures, not 57 leaves:** gateway runner signature; WhatsApp hermetic env; TTS/vision fakes; session-context source refs; parser/default factories; TUI watcher teardown.
3. **Update plainly obsolete assertions:** `custom`, `tgg_case_search`, `inter_session`.
4. **Fix the two CI path probes and the one Ruff encoding finding.**
5. Run focused clusters, then full `scripts/run_tests.sh`, then `ruff check .`; require a clean main Tests + Lint run, not only local green.

Estimated implementation/verification:

- Mechanical changes: 2.5–4 hours.
- Three behavior calls and targeted validation: 0.5–1.5 hours.
- Full-suite/lint cycles and one correction allowance: 1–1.5 hours elapsed.
- Total: **4–6 engineering hours** if kept as one focused pass.

The uncertainty is not code volume; it is cross-test cleanup around the global async queue and the two PA behavior decisions. Sunday remains realistic. Waiting until Sunday morning is not: the full suite costs ~14–16 minutes per signal, and cross-provider review is mandatory.

## Freeze / live-state note

Every identified repair is **repository source or test work**. None requires touching `tgg-app-1`, Christopher's live database, WhatsApp state, credentials, or any other client-host state. Therefore the fix branch itself is not blocked by the TGG go-live deploy freeze.

No failure in this inventory requires a live-client mutation to fix. Deploying the eventual merged runtime is a separate production action and remains subject to the freeze/deploy decision. The bounded-dry-run and constitution clusters should be verified with fixtures/replay locally; they do not justify probing or mutating live client state.

## Complete failure inventory

Classification here is deliberately strict:

- `INFRA`: the hosted runner cannot traverse the simulated `/root/.hermes` path; the behavior under test did not execute.
- `REAL`: a reproducible source/test contract mismatch or test-isolation defect. `REAL` does not automatically mean a live production regression.

| # | Class | Pytest node | Exact CI summary |
|---:|---|---|---|
| 1 | REAL | `tests/cron/test_pa_job_brief.py::test_run_job_selects_pa_brief_and_restricts_toolsets` | AssertionError: assert ['memory', 'f...eb', 'custom'] == ['memory', 'f...'pa-business'] |
| 2 | REAL | `tests/gateway/test_durable_jsonl_consumer.py::test_bounded_dry_run_is_read_only_and_predicts_reconciliation` | assert b'SQLite form...ndingbypassed' == b'SQLite form...0\x00\x00\x00' |
| 3 | REAL | `tests/gateway/test_status_command.py::test_handle_message_persists_agent_token_counts` | TypeError: _make_runner.<locals>.<lambda>() got an unexpected keyword argument 'event' |
| 4 | REAL | `tests/gateway/test_status_command.py::test_first_run_slack_home_channel_onboarding_uses_parent_command` | TypeError: _make_runner.<locals>.<lambda>() got an unexpected keyword argument 'event' |
| 5 | REAL | `tests/gateway/test_status_command.py::test_first_run_non_slack_home_channel_onboarding_keeps_direct_command` | TypeError: _make_runner.<locals>.<lambda>() got an unexpected keyword argument 'event' |
| 6 | REAL | `tests/gateway/test_status_command.py::test_handle_message_discards_stale_result_after_session_invalidation` | TypeError: _make_runner.<locals>.<lambda>() got an unexpected keyword argument 'event' |
| 7 | REAL | `tests/gateway/test_status_command.py::test_handle_message_stale_result_keeps_newer_generation_callback` | TypeError: _make_runner.<locals>.<lambda>() got an unexpected keyword argument 'event' |
| 8 | REAL | `tests/gateway/test_telegram_topic_mode.py::test_managed_topic_binding_reuses_restored_session_over_static_lane_session` | TypeError: _make_runner.<locals>.<lambda>() got an unexpected keyword argument 'event' |
| 9 | REAL | `tests/gateway/test_tts_media_routing.py::test_streaming_delivery_routes_telegram_flac_media_tag_to_document_sender` | AssertionError: Expected mock to have been awaited once. Awaited 0 times. |
| 10 | REAL | `tests/gateway/test_tts_media_routing.py::test_streaming_delivery_routes_non_voice_telegram_ogg_media_tag_to_document_sender` | AssertionError: Expected mock to have been awaited once. Awaited 0 times. |
| 11 | REAL | `tests/gateway/test_tts_media_routing.py::test_streaming_delivery_routes_telegram_mp3_media_tag_to_voice_sender` | AssertionError: Expected mock to have been awaited once. Awaited 0 times. |
| 12 | REAL | `tests/gateway/test_vision_memory_leak.py::TestEnrichMessageWithVision::test_clean_description_passes_through` | AttributeError: '_Stub' object has no attribute '_vision_preanalysis_max_concurrency' |
| 13 | REAL | `tests/gateway/test_vision_memory_leak.py::TestEnrichMessageWithVision::test_memory_context_fence_stripped` | AttributeError: '_Stub' object has no attribute '_vision_preanalysis_max_concurrency' |
| 14 | REAL | `tests/gateway/test_vision_memory_leak.py::TestEnrichMessageWithVision::test_fenced_leak_stripped_plugin_header_preserved` | AttributeError: '_Stub' object has no attribute '_vision_preanalysis_max_concurrency' |
| 15 | REAL | `tests/gateway/test_whatsapp_replay_turn_policy.py::test_ingest_chat_bypasses_require_mention_for_ops_capture` | assert None is not None |
| 16 | REAL | `tests/gateway/test_whatsapp_replay_turn_policy.py::test_replay_uses_timestamp_debounce_without_wall_sleep` | AssertionError: assert 0 == 3 |
| 17 | REAL | `tests/gateway/test_whatsapp_replay_turn_policy.py::test_direct_trigger_closes_replay_turn_immediately` | AssertionError: assert 0 == 3 |
| 18 | REAL | `tests/gateway/test_whatsapp_replay_turn_policy.py::test_replay_bundle_hard_capped_at_ten_messages` | AssertionError: assert 0 == 22 |
| 19 | REAL | `tests/gateway/test_whatsapp_replay_turn_policy.py::test_replay_bundle_cap_override_uncapped_matches_live_no_cap` | AssertionError: assert 0 == 22 |
| 20 | REAL | `tests/gateway/test_whatsapp_replay_turn_policy.py::test_replay_bundle_cap_override_custom_value` | AssertionError: assert 0 == 12 |
| 21 | REAL | `tests/gateway/test_whatsapp_replay_turn_policy.py::test_replay_without_turn_policy_uses_passive_window_not_legacy_default` | AssertionError: assert 0 == 4 |
| 22 | REAL | `tests/gateway/test_whatsapp_replay_turn_policy.py::test_replay_without_any_explicit_window_keeps_legacy_default` | AssertionError: assert 0 == 3 |
| 23 | REAL | `tests/gateway/test_whatsapp_replay_turn_policy.py::test_replay_bundle_still_splits_on_timestamp_gap` | AssertionError: assert 0 == 4 |
| 24 | REAL | `tests/gateway/test_whatsapp_replay_turn_policy.py::test_album_bundle_renders_shared_quote_once_with_case_refs` | AssertionError: assert 0 == 2 |
| 25 | REAL | `tests/gateway/test_whatsapp_replay_turn_policy.py::test_album_bundle_renders_distinct_quotes_separately` | AssertionError: assert 0 == 2 |
| 26 | REAL | `tests/gateway/test_session_hygiene.py::test_session_hygiene_messages_stay_in_originating_topic` | TypeError: test_session_hygiene_messages_stay_in_originating_topic.<locals>.<lambda>() got an unexpected keyword argument 'event' |
| 27 | REAL | `tests/gateway/test_session_hygiene.py::test_session_hygiene_warns_user_when_summary_generation_fails` | TypeError: test_session_hygiene_warns_user_when_summary_generation_fails.<locals>.<lambda>() got an unexpected keyword argument 'event' |
| 28 | REAL | `tests/gateway/test_session_hygiene.py::test_session_hygiene_informs_user_when_aux_model_fails_but_recovers` | TypeError: test_session_hygiene_informs_user_when_aux_model_fails_but_recovers.<locals>.<lambda>() got an unexpected keyword argument 'event' |
| 29 | REAL | `tests/gateway/test_session_hygiene.py::test_session_hygiene_honors_configurable_hard_message_limit` | TypeError: test_session_hygiene_honors_configurable_hard_message_limit.<locals>.<lambda>() got an unexpected keyword argument 'event' |
| 30 | REAL | `tests/gateway/test_session_hygiene.py::test_session_hygiene_default_hard_message_limit_does_not_fire_at_12_messages` | TypeError: test_session_hygiene_default_hard_message_limit_does_not_fire_at_12_messages.<locals>.<lambda>() got an unexpected keyword argument 'event' |
| 31 | INFRA | `tests/hermes_cli/test_gateway_service.py::TestSystemUnitHermesHome::test_system_unit_uses_target_user_home_not_calling_user` | PermissionError: [Errno 13] Permission denied: '/root/.hermes/node/bin' |
| 32 | INFRA | `tests/hermes_cli/test_gateway_service.py::TestSystemUnitHermesHome::test_system_unit_remaps_profile_to_target_user` | PermissionError: [Errno 13] Permission denied: '/root/.hermes/profiles/coder/node/bin' |
| 33 | REAL | `tests/hermes_cli/test_whatsapp_setup_ordering.py::test_existing_pairing_skip_branch_enables_whatsapp` | AssertionError: assert None == 'true' |
| 34 | REAL | `tests/run_agent/test_async_httpx_del_neuter.py::TestClientCacheBoundedGrowth::test_same_key_replaces_stale_loop_entry` | AssertionError: Should have the new model |
| 35 | REAL | `tests/test_pa_business_facts.py::test_generic_observation_injects_current_turn_source_refs` | KeyError: 'sourceRefs' |
| 36 | REAL | `tests/test_pa_business_facts.py::test_generic_observation_placeholder_source_refs_bind_real_turn_ids` | KeyError: 'sourceRefs' |
| 37 | REAL | `tests/test_pa_business_facts.py::test_generic_observation_placeholder_inside_fields_bind_real_turn_ids` | KeyError: 'sourceRefs' |
| 38 | REAL | `tests/test_pa_business_facts.py::test_direct_observation_placeholder_source_refs_bind_real_turn_ids` | KeyError: 'ok' |
| 39 | REAL | `tests/test_pa_business_facts.py::test_tgg_spreadsheet_gate_refuses_oversized_csv_without_full_read` | AttributeError: 'Oversized' object has no attribute 'st_mode' |
| 40 | REAL | `tests/test_pa_case_state_echo.py::test_observation_injects_current_turn_source_refs` | KeyError: 'ok' |
| 41 | REAL | `tests/test_pa_case_state_echo.py::test_state_claim_gate_in_both_job_briefs` | AssertionError: tgg_ops_ingest |
| 42 | REAL | `tests/test_pa_compaction_guidance.py::test_christopher_ops_ingest_declares_guidance_not_policy` | AssertionError: assert 'strategy' not in {'preserve_fields': ['block', 'unit', 'worker', 'photo', 'status'], 'strategy': 'preserve-case-state', 'window_size': 24} |
| 43 | REAL | `tests/test_pa_constitution.py::test_tgg_management_defaults_to_operator_db_before_ilinked` | AssertionError: assert 'operation case_search' in 'Separate facts from any interpretation; do not mix known data with a read.\nDefault management-chat answer shape: ans...gement_question_answered, escalated, clarification_requested. Use a new label only when none of these genuinely fit.\n' |
| 44 | REAL | `tests/test_tgg_christopher_replay_profile.py::test_replay_preflight_rejects_prod_business_url` | AttributeError: 'Namespace' object has no attribute 'prod_pilot_run_id' |
| 45 | REAL | `tests/test_tgg_christopher_replay_profile.py::test_replay_preflight_rejects_sqlite_sidecars` | AttributeError: 'Namespace' object has no attribute 'prod_pilot_run_id' |
| 46 | REAL | `tests/test_tui_gateway_server.py::test_load_enabled_toolsets_rejects_disabled_mcp_env` | AssertionError: assert ['inter_sessi...an', 'memory'] == ['kanban', 'memory'] |
| 47 | REAL | `tests/test_tui_gateway_server.py::test_load_enabled_toolsets_falls_back_when_tui_env_invalid` | AssertionError: assert ['inter_sessi...an', 'memory'] == ['kanban', 'memory'] |
| 48 | REAL | `tests/tools/test_async_delegation.py::test_async_executor_workers_are_daemon_threads` | assert None is not None |
| 49 | REAL | `tests/tools/test_async_delegation.py::test_completion_event_lands_on_shared_queue_with_session_key` | assert None is not None |
| 50 | REAL | `tests/tools/test_async_delegation.py::test_rich_reinjection_block_is_self_contained` | assert None is not None |
| 51 | REAL | `tests/tools/test_async_delegation.py::test_crashed_runner_produces_error_completion` | assert None is not None |
| 52 | REAL | `tests/tools/test_async_delegation.py::test_interrupt_all_signals_running_children` | assert None is not None |
| 53 | REAL | `tests/tools/test_async_delegation.py::test_delegate_task_background_routes_async_and_does_not_block` | assert None is not None |
| 54 | REAL | `tests/tools/test_async_delegation.py::test_delegate_task_background_detaches_child_from_parent` | assert None is not None |
| 55 | REAL | `tests/tools/test_async_delegation.py::test_pa_gateway_async_completion_reenters_and_records_pa_turn` | assert not True |
| 56 | REAL | `tests/tools/test_transcription_dotenv_fallback.py::TestProviderSelectionGate::test_explicit_xai_sees_dotenv` | AssertionError: assert 'none' == 'xai' |
| 57 | REAL | `tests/tools/test_transcription_dotenv_fallback.py::TestEndToEndRegressionGuard::test_xai_key_only_in_dotenv_before_fix` | assert False is True |

## Evidence checks performed

- Fetched `origin` before making any repo-state claim.
- Pulled failed logs directly with `gh run view 30132415288 --log-failed` and the earlier run `30069951359`.
- Parsed pytest's short summaries and compared node-id sets mechanically: `57 == 57`, symmetric difference empty.
- Compared commit range `2f9a481eff..505c9e8df4` and inspected the de-fusion code commit/file set.
- Ran Git blame/log searches on failing assertions and implicated source contracts.
- Pulled and compared both failed Lint workflow logs (`30132415302`, `30069951335`); exact same Ruff finding.
- Did not run or mutate any live client surface.
