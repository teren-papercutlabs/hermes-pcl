# Review — TGG/Christopher attachment usability + memory injection + xhigh slot

**Date:** 2026-08-03
**Reviewer:** edna review clone (fresh, non-authoring), session af6e809e
**Reviewed target (terminal head):** hermes-pcl `7d4965a4398cc80aabfc93db7f2987cc20b7f647`
**Canonical external-review attempt:** `4c7c8ec7-a714-4c35-8b9e-c15d09c2e991` (model=claude, verdict=fail), reviewed SHA `cabd97352b7eea9c5c908db40d0433dbb8206a72`
**Full model output:** `/Users/pcloffice/pcl/_review-artifacts/85aae025/claude-review.md`

## Verdict: BLOCK

Not PR-ready. One hard blocker (production `NameError` on every fresh session) plus a verification gap that lets it pass CI.

### Head movement (context)
Parent redirected the exact head 5x during review: `55f9d91068 → 4c7ef34736 → 2d1b0984e1 → b48d3ab9ec → cabd97352b → 7d4965a4`. `gateway/run.py` is **byte-identical `cabd97352b → 7d4965a4`** (delta is ONLY `deploy/tgg/christopher/scripts/run_isolated_smoke.py` +36, a test fixture) — git-grep-verified at `7d4965a4`. So the claude attempt at `cabd` applies unchanged to the terminal head.

## HARD BLOCKER — `agent_holder` out of scope → NameError on every fresh session
`gateway/run.py:9729`
```python
"system_prompt": _session_meta_system_prompt(agent_holder[0]),
```
`agent_holder` is a **local of `_run_agent`** (def `16778`, bound at `17290` as `[None]`). Line 9729 is inside `_handle_message_with_agent` (def `8777`), a **sibling method** — not lexically nested, no module global, no `global`/`nonlocal`, no closure. `git grep agent_holder gateway/run.py`: appears at `9729`, then nowhere until `16995+`.

Runtime consequence (independently ground-truthed at the exact head, not relayed blind):
- 9729 executes on the fresh-session branch (`elif not history:` ~9717) — first turn of every new chat session.
- Raises `NameError: name 'agent_holder' is not defined`, swallowed by the broad handler ~9880 → the turn aborts → `session_meta`, user msg, and assistant response are never appended → user gets "Sorry, I encountered an error (NameError)…".
- Nothing persists → `history` stays empty → next message re-enters the same branch: **permanent first-turn loop**. With `session_reset: mode: none`, blast radius = every new Christopher chat (and every chat after any reset).

**Fix:** plumb `system_prompt` into `_run_agent`'s result dict (return sites ~`18091`/`18217`/`18550`; default `""` on proxy paths), then at `9729` read `agent_result.get("system_prompt","")`.

## VERIFICATION GAP — new consumer fixture asserts on the exact crashing write
`deploy/tgg/christopher/scripts/run_isolated_smoke.py` — `_verify_memory_in_session_jsonl` (the +36 that IS `7d4965a4`) asserts exactly one `session_meta` JSONL row containing the MEMORY probe. But that `session_meta` row is written at **line 9729** — the crashing line. Its fresh home → fresh session → NameError → row never appended → the fixture raises `"fresh gateway session JSONL omitted live MEMORY.md content"`. So running `run_isolated_smoke` against this code should **FAIL** — a symptom of 9729, not of memory logic. `tests/gateway/test_document_sandbox_context.py:78-96` only tests the helper in isolation (helper is correct); nothing exercises the call site.

## Important (non-blocking — fix or explicitly accept)
- **MEMORY.md enable is a no-op for existing sessions:** `run_agent.py:12455-12468` reuses stored `system_prompt` verbatim when history exists; with `session_reset mode:none`, memory reaches only sessions created after the switch. Christopher's live chats are long-lived → needs a documented per-chat `/reset` rollout.
- **Single shared `HERMES_HOME/MEMORY.md` across every chat** (`tools/memory_tool.py:55,128,180-183`) — cross-chat leakage risk vs constitution isolation; 2200 chars bounds size not leakage. If accepted, make it an explicit ruling + forbid tenant/contact detail in memory writes.
- **`CHRISTOPHER_ENGINE_SLOT=gpt-5.6-luna-xhigh` hardcoded in the pre-restart hook** (`pa-agent.hermes.manifest.json`); `apply_engine_slot.py:70-79` lets explicit `--slot` override the persisted engine-slot file, so manual `switch_engine_slot.sh` is silently reverted on next deploy/restart. Intended, but make the switch script say so or seed-only-when-absent.
- **6th+ hardcoded copy of the slot table** (`apply_engine_slot.py:20-30`, `build_runtime_slots.py:42-47`, `validate_deployment_spec.py:36-45`, `verify_runtime.sh:227-237` AND `515-527`, `run_isolated_smoke.py:259-265`, + test literals) — adding slot #5 needs ~8 edits; drift risk.
- **Mislabelled filename:** `gateway/run.py:8637-8652` labels `basename` as "Original filename:" even when the on-disk name is prefixed `<ts>_<id>_<original>` (`display_name` exists for that reason). Sandbox path stays correct; only the label lies. Use "Saved filename:" or emit both.

## Verified clean
`host_path_to_python_sandbox_path` (`tools/python_sandbox_paths.py:23-56`) lexical, rejects `..`; config read hoisted out of loop, fails closed to `{}`; `python_sandbox_paths` shipped by both manifests; loud `[TRUNCATED]` marker + test in lockstep; `memory_char_limit` consumed (default 2200); xhigh slot wiring consistent across all 5 copies.

**Not verifiable read-only:** the 4 regenerated `SHA256SUMS` digests, and existence/readability of `/var/lib/tgg-capture/whatsapp/media/documents` on the box — covered by `validate_deployment_spec.py` / live smoke, not runnable here — treat as unconfirmed, not passed.

## Notes
- No code edits made (read-only review, as briefed).
- **Freeze deviation:** teren-ordered fleet freeze landed ~18:34 SGT before a FRESH external-review attempt against `7d4965a4` was dispatched; per the freeze (no new dispatches) it was not started. Verdict unaffected — the blocker is in code byte-identical `cabd→7d4965a4`. Re-run a fresh claude review against the fixed head after the all-clear, once 9729 is fixed.
