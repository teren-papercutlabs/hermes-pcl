# TGG demo-pause preservation in secret materialization — cross-provider review verdict

Reviewed: `bfc31e0759dd8eb839460fb22631adc7bc4420bd` ("fix(tgg): preserve demo pause during secret materialization") against its parent (repo: hermes-pcl). 1 file, +4/-0: `deploy/tgg/christopher/scripts/prepare_host_secrets.sh`.

Context: approved deployment boundary is deploy with `TGG_DEMO_MANAGEMENT_ONLY=true`, no live flip. Live process carried the flag but the regenerated `.env` did not — `prepare_host_secrets.sh` rewrites the whole env file with only the secret keys, so a routine deploy restart would have silently dropped the pause. This commit adds the pause literal to the generated file.

## Verdict: CLEAR

### 1. Generated env necessarily includes `TGG_DEMO_MANAGEMENT_ONLY=true`, no secret exposure — CLEAR
`lines.append("TGG_DEMO_MANAGEMENT_ONLY=true")` (`prepare_host_secrets.sh:40`) is an unconditional Python string literal appended after the secret-key loop, unguarded by any branch — it executes on every invocation regardless of which secrets are present. It is not sourced from `os.environ` or any secrets file, so it carries no risk of leaking `OPENAI_API_KEY`/`GEMINI_API_KEY` values; the adjacent lines that do carry secrets already wrap them in `json.dumps(value)` and are untouched by this diff.

### 2. Script remains in the bundle manifest; syntax/manifest checks pass — CLEAR
`prepare_host_secrets.sh` was already present in `pa-agent.hermes.manifest.json`'s file list prior to this commit (added independently) and this diff does not touch the manifest. Verified live at `bfc31e075`: `bash -n deploy/tgg/christopher/scripts/prepare_host_secrets.sh` clean; the embedded Python heredoc compiles clean (`py_compile`); `build_pa_agent_manifest.py --check` returns `{"check": true, "file_count": 567, "ok": true}`.

### 3. Cannot enable the site lane or alter processing-gate/rung/scope semantics — CLEAR
`TGG_DEMO_MANAGEMENT_ONLY` has exactly one read site (`gateway/durable_jsonl_consumer.py:1503-1505`), where a truthy value skips site-batch concurrency acquisition entirely (`if not demo_management_only: ... site_batches[:available_site]`) — the flag can only *suppress* site-lane activity, never grant it, and this commit hardcodes the suppressing value. It is structurally disjoint from `processing-gate.json` (read by `activate_processing.py`, `effectiveEnableRule: "pa.enabled AND processing-gate.enabled"`) and from the rung/graduation ceremony, which lives entirely in a different repo (`tgg-agent/runtime/tgg-capture-whatsapp-bridge/activation-orchestrator.js` — per `deploy/tgg/christopher/scripts/WHERE-THE-CEREMONY-LIVES.md`). This commit touches none of those files or mechanisms.
`EnvironmentFile=/home/pclaw/.hermes-christopher-tgg/.env` is the systemd unit's sole source for this variable (`christopher-tgg-hermes.service`) — no other `Environment=` line or override path sets it, so the generated file is authoritative and this fix closes the actual gap.

### 4. No safer or required source contract is missing — CLEAR
The hardcoded literal, gated behind a code review (this one) to change, is the single-homed, explicit, deterministic form for a boundary the comment states is intentionally held "until Teren explicitly releases the site drain" — an implicit/default-derived value would be the weaker contract here, not this one. No companion file (deploy spec, systemd unit, other generator) sets or overrides this variable, so nothing is left unsynchronized by this change.

No correctness issues found in the reviewed diff.
