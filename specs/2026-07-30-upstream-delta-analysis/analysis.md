# Hermes upstream delta analysis

**Snapshot:** 2026-07-30 10:09 SGT. Read-only comparison of PcL fork `origin/main` at `1ef233b738987fd253374d0df79ad568002674c5` and Nous upstream `upstream/main` at `0bd82a8a84595720ea1f14b103aeb81ca3cc50ef`. The pending TGG persistence branch `origin/worker/a9ab2ff5-9abdeced` was also read through commits `50ee3e8aa1` and `09547d5bfd`; it is not part of the `origin/main` divergence counts below.

## Executive call

Do **not** merge upstream wholesale and do **not** cherry-pick the compressor fixes individually. Upstream's compressor is now a different subsystem: 134 upstream-only commits changed it, versus 2 fork-only commits, and its current file is 5,619 lines versus the fork's 1,731. The valuable move is a controlled compressor subsystem re-sync that preserves PcL's PA guidance/policy hooks and proves Christopher's `mode: none` path end to end.

The session subsystem is also worth re-syncing later, but only behind a reconciliation plan. Upstream adds durable routing, concurrency, resume, pruning and recovery fixes, while still using naive `datetime.now()` in `gateway/session.py`. A direct replacement would regress PcL's timezone-aware fix (`50ee3e8aa1`) and would delete PcL's replay namespace isolation (`3227042d6c`, `651649a1e9`).

## Merge base and divergence

Merge base: `55c9f32060bbe7eb48bee2b702c157408b468eb2` (`fix(tui): width-aware markdown table rendering with vertical fallback (#26195)`, committed 2026-05-16).

| Population measured from that merge base | Count |
|---|---:|
| Commits reachable only from `upstream/main` | 11,001 total; 10,201 non-merge |
| Commits reachable only from fork `origin/main` | 334 total; 327 non-merge |
| Paths changed on the upstream side (`merge-base..upstream/main`) | 7,085: 4,730 added, 2,108 modified, 116 deleted, 131 renamed |
| Paths changed on the fork side (`merge-base..origin/main`) | 321: 244 added, 72 modified, 1 deleted, 4 renamed |
| Upstream-only commits touching `agent/context_compressor.py` | 134, all non-merge |
| Fork-only commits touching `agent/context_compressor.py` | 2, all non-merge |
| Upstream-only commits touching `gateway/session.py` | 74, all non-merge |
| Fork-only commits touching `gateway/session.py` | 2, all non-merge |

These counts describe commit/path populations, not independent capabilities. Many upstream commits form one interdependent subsystem.

## 1. Context compressor

### What upstream gained

#### A. Failure safety and recovery — **PULL NOW**

Upstream no longer treats a failed summarizer as permission to lose history:

- Summary LLM failure aborts rather than dropping messages (`1634397ddb`); the later deterministic fallback preserves bounded context when summary generation fails (`e785c0ad70`).
- Transient network failure preserves the original context (`ac822e4d36`), and an empty summary response is an explicit failure rather than a valid empty handoff (`b6a4638b6d`).
- Quota/access errors preserve messages and retain retry behavior (`c72f4576b9`, `202ad1b8c9`). Repeated timeout candidates get independent budgets and escalating cooldowns (`bd7e480236`).
- Interrupted preflight accounting is rolled back (`17a81ac89e`), and compression state is revalidated under its lock (`727392b5cb`).
- Anti-thrash state persists across restarts (`ec5835ab8b`), fallback compactions count as ineffective (`5ce827cac9`), and a tripped automatic-compression block now has a recovery path (`62bec4b3f8`).

**Why we want it:** `SessionResetPolicy.mode = none` makes compression Christopher's only automatic context boundary. The fork's static fallback proof in `09547d5bfd` covers the current implementation, but upstream has addressed more failure classes and persisted the breaker state across restarts.

**Pull shape:** subsystem re-sync, not cherry-picks. These commits share new session-bound state, telemetry, cooldown, and helper APIs (`bind_session_state`, persisted ineffective/fallback counters). Cherry-picking the visible failure commit without its state machinery would recreate partial behavior.

**Effort/risk:** medium-high effort, high semantic value. Risk is silent loss of PcL's custom PA summary guidance and policy branches unless explicitly ported.

#### B. Token and threshold accuracy — **PULL NOW**

- Compression threshold reserves output tokens instead of treating the whole model window as input budget (`623b21bf24`).
- Tail budgeting counts tool-call envelopes that the fork's visible-text estimate misses (`72f75f8456`) and accounts for Codex replay payloads (`78ee0aa367`).
- CJK budgeting is corrected (`3f33a1c5aa`), with an ASCII fast path to avoid imposing that cost on normal English turns (`ea0fd393db`).
- Anti-thrash decisions use real token usage rather than an estimated floor (`d172445629`); the threshold floor and ineffective-compaction scoring were repaired in `76381e2a8e`, `32f30d2a4f`, and `7f9485707d`.
- Operators can cap by absolute tokens (`e5078e3152`) and override per model (`5f2fdf66bf`); model switches reset stale calibration (`1e0b3a2bcc`) and the context-length setter now re-floors coherently (`49f50c68a0`).

**Why we want it:** Christopher uses large OpenAI windows and tool-heavy replay/capture context. Under-counting tool envelopes or replay material delays compaction until the provider rejects the request; over-counting causes needless summarization and quality loss.

**Pull shape:** same compressor subsystem re-sync. The threshold logic is now spread across model metadata, config parsing, and compressor state.

**Effort/risk:** medium-high. Validate against PcL's OpenAI-direct models and the exact 40→6 / repeated-cycle tests added by `09547d5bfd`.

#### C. Handoff/checkpoint quality — **PULL NOW**

- Recent turn preservation became explicit (`aec38855b5`), including keeping the last user turn (`24add1db74`) and a configurable recent-N user tail (`a9c868225e`).
- Turn pairing prevents an orphan user turn (`fc2fac73bd`); orphan tool calls are removed rather than replaced with fabricated stubs (`32b23bfb08`).
- Unanswered user questions are carried as active work (`56b8dccf25`), summary focus is anchored on recent user turns (`434c684bfa`), and summaries gain temporal anchors (`d87f293972`).
- Restart/rehydration no longer recursively summarizes stale handoff prefixes (`42bbd221e8`); merged-handoff lineage is deduplicated and recovered (`2b84ed921c`, `0ee8d41878`).
- The task snapshot is grounded before summary (`761a0b124e`) and the latest actionable user turn survives blank/echo cleanup (`bc4824167d`).
- Skill bodies that are pruned leave deterministic markers and protected recent skills cannot be ghosted (`5faef80a43`, `44c67fca91`).

**Why we want it:** these changes directly improve the checkpoint that becomes Christopher's durable conversational memory after compaction. The important outcome is not “a summary exists”; it is that the next turn retains the manager's unresolved ask, source grounding, and valid tool alternation.

**Pull shape:** subsystem re-sync. Do not cherry-pick prompt-only commits independently of the frozen-prefix and rehydration compatibility fixes (`8204b27618`, `835de6f764`).

**Effort/risk:** medium-high. Add golden replay cases from real TGG conversational shapes, but do not feed client data into the repo.

#### D. Security and privacy at compaction boundaries — **PULL NOW**

Strict redaction is applied at each compaction text boundary (`0acdf1d8c8`), including deterministic/fallback text. This is materially safer than treating the summarizer prompt and persisted handoff as ordinary internal strings.

**Pull shape:** include in the compressor re-sync. A standalone cherry-pick is possible in principle but would miss newly introduced text boundaries.

**Effort/risk:** low incremental effort inside the re-sync; low behavior risk, with a quality risk only if redaction over-matches TGG identifiers. Test positive and negative examples.

#### E. Performance and observability — **PULL LATER, with the re-sync if cheap**

- Large-window models proactively prune old tool results before full summarization (`cb481e2f2b`); prompt-cache reclaim is gated (`fa4800414c`).
- Summary input is bounded (`80ece3867b`, re-anchored in `b7a05b6b6f`) and in-memory-only guard refresh avoids durable I/O (`8ddc05b801`).
- Compression attempt telemetry records protected head/tail, thresholds and fallback state (`356ff99030`, adapted to the current aux-call contract by `cbc1054e23`).

These are valuable but secondary to correctness. Bring them with the subsystem if their dependencies are already present; do not delay the correctness pull to perfect telemetry.

### Fork-specific compressor behavior that must survive

The fork exposes PA-specific APIs absent from upstream: `set_pa_compaction_guidance`, `set_pa_compression_policy`, and `_compress_with_pa_policy` (`a56f6e5910`; earlier PA overlay in `2f6e2f847f`). Christopher's standing compaction guidance is injected through that surface. A straight file replacement removes it.

**Required preservation contract for a re-sync:**

1. Port the standing guidance as a narrow extension point on upstream's current summary builder, not as a second compressor branch.
2. Decide explicitly whether the legacy PA preserve/recent-only policy remains live. If no deployed agent reads it, retire it rather than carrying two compression algorithms.
3. Re-run `tests/gateway/test_mode_none_compaction.py` from `09547d5bfd`, plus summary-failure, multi-cycle, cross-restart breaker persistence, redaction, tool-pair, and recent-user-tail tests.

## 2. Gateway session and lifecycle

### Valuable upstream lifecycle work

| Capability | Upstream evidence | Recommendation | Pull shape / risk |
|---|---|---|---|
| Honor `mode: none` across reset/recovery boundaries and preserve adapter-aware resume guidance | `9fc0074bac`; self-healing reset path also checks policy in `4b12b7a359` | **PULL NOW as behavior**, but reconcile rather than cherry-pick | Overlaps `09547d5bfd` positively. Preserve PcL's tests and config contract. |
| Durable routing in `state.db`, with `sessions.json` only a legacy mirror | `747386ecfa`, `94205a1139` | **PULL LATER** | Subsystem re-sync. High migration risk because PcL replay/capture state also uses `state.db`. |
| SessionStore concurrency/I/O boundaries | `08e9dcf182`, `9d38a2309e`, `b3f77f5c82` | **PULL NOW in the session re-sync** | Do not cherry-pick one lock fix; the commits form one refactor. |
| Recovery survives restart without cross-profile adoption; stale routes self-heal | `86e64900b9`, `f1fde49e45`, `3a83b6bc5d`, `d17daf0b12` | **PULL LATER** | Valuable, but profile/multiplex semantics need de-fusion review. |
| Reset finalization and expiry reasons stay durable/auditable | `e701cdc86e`, `3305dcedbb`, `3c7bab9c65` | **PULL LATER** | Reconcile with PcL's replay end reasons and capture-only runs. |
| Active processes fail closed during prune/expiry; recent sessions are preserved | `ca559a7852`, `f5b6112226`, `a228b81501` | **PULL NOW** | Small capability cluster, but land against the current SessionStore shape. |
| Resume-pending freshness avoids zombie auto-resume | `a1f62f4777`; policy-none treatment is unified in `9fc0074bac` | **PULL NOW** | Must keep `mode: none` persistent sessions reusable while preventing stale interrupted work from auto-running. |
| Flush pending messages/memory before teardown | `58f6678e6d`, `72024950cf`, `5cc5c58e01` | **PULL NOW** | Cross-file cluster; test at the consumer layer. |
| Path-traversal rejection on persisted session fields | `3a6a43cb81`, tightened by `4d4ba0831e` | **PULL NOW independently** | The fork's current `gateway/session.py` has no `_is_path_unsafe` guard. This security fix can be backported before the full session re-sync. |

### Direct conflict with PcL's recent timezone fix

Upstream `gateway/session.py` at `0bd82a8a84` still defines `_now()` as naive `datetime.now()` and compares naive stored timestamps directly in reset, expiry, prune and resume paths. PcL commit `50ee3e8aa1` replaces this with configured, aware Hermes wall time and normalizes legacy naive timestamps before comparison.

**Conflict:** taking upstream's session file verbatim regresses the exact cross-timezone persistence bug PcL just fixed. The session re-sync must port `_hermes_now`, `_in_clock_timezone`, and every comparison site from `50ee3e8aa1`, then retain the cross-04:00-SGT and multi-day-idle proof in `09547d5bfd`.

### Direct conflict with replay/capture architecture

The fork namespaces session keys through `gateway.replay.current_replay_context()` and refuses to fall back to the live session key if namespacing fails (`3227042d6c`, hardened by `651649a1e9`). Upstream's session code does not contain that replay context; its new namespace is profile-oriented (`747386ecfa`, `e8b7ce8c19`).

A straight session subsystem replacement would therefore erase the replay envelope's isolation and could let a bounded replay attach to a live chat session. Preserve PcL's replay namespace as an orthogonal dimension to upstream's profile namespace, with tests proving:

- ordinary live consumer traffic uses the stable per-chat session;
- bounded backplay uses an isolated namespace (`50ee3e8aa1` makes this distinction explicit in `gateway/durable_jsonl_consumer.py`);
- capture-only runs never open business-write behavior (`5a5d4da4c9`, `def2628fa3`);
- provider failure preserves captured inputs without converting the run into a live business turn (`042228afa7`).

## 3. Other materially valuable upstream deltas

### Pull now

1. **Auxiliary fallback correctness for Gemini/openai-direct topology.** A fallback chain no longer skips a sibling model merely because it shares a provider (`e8f8b34b0c`), and an auxiliary failure can reach the main model even when the failed sibling shares that provider (`0a2c245cd6`). This maps directly to PcL's Gemini auxiliary + OpenAI-direct primary/fallback use. Prefer the coherent fallback cluster over isolated cherry-picks because backend identity was later centralized in `39b5965569`.
2. **Gemini tool/schema compatibility.** Preserve bridged tool response names (`01cb38e8ec`) and typed enum constraints as strings (`a751924c04`). These are narrow backports with low conceptual conflict.
3. **Sandbox/child-process security.** Host-bound Docker commands require approval (`9860d93f2a`); host/relative cwd overrides are sanitized before `docker run -w` (`c15945655f`); Hermes secrets are scrubbed from command and voice subprocess environments (`24a6fb6448`, `3ae25e0fbd`). Backport after checking PcL's Python sandbox wrapper and deployment tests.
4. **WhatsApp reliability used by the fleet.** Normalize bare phone targets before bridge send (`a4b1554c73`), resolve modern phone↔LID aliases (`263ffec1b0`, `caf4dcc7ad`), and restart stale bridge processes instead of silently reusing them (`3edd09a46f`). Pull as a WhatsApp adapter slice, not as the June plugin-bundling refactor (`5600105478`).

### Pull later

- WhatsApp native polls/rich metadata (`11627fdcb9`) and inbound read receipts (`35afa8ce06`, ordering corrected by `652d858f2e`) are useful product capabilities but not reliability prerequisites for Christopher's current capture-only path.
- Provider catalogs/defaults are highly volatile. Upstream moved Gemini aux defaults and model catalogs (`e25d516c8c`, `63fc810b95`); PcL should take capability fixes, not inherit upstream's model choices automatically.
- Gateway's 19 session-keyed dictionaries are consolidated into scoped `SessionState` (`ab08e8fc76`). This can eliminate reset races, but it is a broad July 29 refactor and should follow, not precede, the session/replay reconciliation.

### Skip

- Upstream profile/multiplex features as a bundle (`d82f9fa7f7`, `f35abb122a`, `e8b7ce8c19`). PcL's direction is tenant-neutral core plus deployment-owned tenancy. Pull only generic fixes after stripping profile-as-tenant policy.
- Upstream relay/Slack parity waves. They do not serve TGG's current capture-only consumer and would multiply the reconciliation surface.
- Desktop, voice, TTS, billing, wake-word and UI waves. They account for substantial upstream churn but are outside the deployed PA runtime's needs.
- Automatic adoption of upstream model defaults/catalog churn. PcL's engine slots and provider choices are controlled deployment artifacts.

## Ranked recommendation

| Rank | Disposition | Item | Why / pull shape |
|---:|---|---|---|
| 1 | **PULL-NOW** | Context compressor subsystem re-sync | Compression is now load-bearing for Christopher; upstream carries failure preservation, accurate budgets, anti-thrash persistence, checkpoint quality and redaction. Re-sync the subsystem; preserve PA guidance; prove with `09547d5bfd` tests. |
| 2 | **PULL-NOW** | Session path-traversal guard | Security defect is present in the fork and the fix is comparatively bounded (`3a6a43cb81`, `4d4ba0831e`). Backport independently. |
| 3 | **PULL-NOW** | Aux fallback + Gemini compatibility slice | Directly improves PcL's OpenAI-direct/Gemini aux chain (`e8f8b34b0c`, `0a2c245cd6`, `01cb38e8ec`, `a751924c04`). Pull as one tested provider slice. |
| 4 | **PULL-NOW** | Session concurrency, prune fail-closed, resume freshness, teardown flush | High reliability value, but implement as the first phase of a session subsystem re-sync; do not cherry-pick lock/state commits ad hoc. |
| 5 | **PULL-NOW** | WhatsApp bridge reliability slice | Phone/JID normalization, LID aliases and stale bridge restart address live delivery risks without taking unrelated platform refactors. |
| 6 | **PULL-LATER** | Durable `state.db` routing + full recovery lifecycle | Valuable but high collision risk with replay, capture-only state, pending timezone work and PcL's persistent namespace. Requires a written migration/reconciliation plan. |
| 7 | **PULL-LATER** | Scoped `SessionState` consolidation | Likely removes reset races, but it landed after most lifecycle work and expands the integration surface. Take after the session contract is reconciled. |
| 8 | **PULL-LATER** | Compressor performance/telemetry extras | Useful once correctness is stable; do not let observability/performance delay failure-safety and checkpoint correctness. |
| 9 | **SKIP** | Profile/multiplex tenancy bundle | Conflicts with tenant-neutral de-fusion. Extract generic fixes only. |
| 10 | **SKIP** | Relay/desktop/voice/UI waves and upstream model defaults | No current TGG value proportional to integration cost; model policy belongs to PcL engine slots. |

## Required conflict gates before any pull

1. **Tenant-neutral de-fusion:** no client/tenant policy enters shared core merely because upstream calls it a profile (`d82f9fa7f7`, `e8b7ce8c19`).
2. **Capture-only consumer:** replay/session changes must not enable business writes, advance production cursors, or turn recovery backplay into ordinary live processing (`5a5d4da4c9`, `def2628fa3`).
3. **Replay envelope:** preserve replay attempt provenance and namespace isolation (`3227042d6c`, `651649a1e9`); a missing/failed namespace must remain fail-closed.
4. **Persistent chat design:** `mode: none` remains one ongoing session per chat, with context managed by compression (`09547d5bfd`). No upstream expiry or auto-archive default may silently reintroduce daily/idle resets.
5. **Timezone:** preserve aware configured wall time and legacy timestamp normalization from `50ee3e8aa1`; upstream's naive clock is not acceptable.

## Proposed execution sequence

1. Backport the bounded session security guard and the provider/Gemini/WhatsApp reliability slices independently.
2. Build a compressor re-sync branch from upstream's current file plus a single PcL extension layer for PA guidance. Port the fork tests first, then add upstream failure/budget/anti-thrash cases.
3. Only after compressor acceptance, write the session-state reconciliation map: upstream durable routing/concurrency/resume fields versus PcL replay namespace, capture-only consumer, timezone clock, and `mode: none` contract.
4. Re-sync session lifecycle in phases, with no deploy until replay isolation, capture-only behavior, cross-timezone reuse, restart recovery and compaction all pass through the real gateway consumer path.
