# Cross-provider review: TGG deploy hook transaction classifications

- **Verdict: CLEAR**
- Commit under review: `3137651dd3fa40971ccb2d0c9fcbb8ef841c1421` (`origin/worker/d17d42d8-hook-class`)
- Reviewer: edna review clone (claude), session `e80b3b59`, 2026-07-22 18:55 SGT
- WB: `b25c7e29-9eab-4e61-a035-3fbcabe5f564`

## Scope

The deployed pa-agent transaction gate refused the TGG bundle with
`PA_AGENT_TRANSACTION_HOOK_UNCLASSIFIED` because the two
`christopher-tgg-hermes.service` preRestartHooks lacked transaction
classifications. The commit under review declares both
`{"behavior": "idempotent"}` in `pa-agent.manifest.json`. Review verifies the
classifications are truthful, the manifest passes the deployed gate, and no
hook command or runtime behavior changed.

## Findings

### 1. Diff is classification-only — no behavior change

`git show 3137651dd -- pa-agent.manifest.json`: 4 insertions, 2 deletions,
single file. Byte-compared each hook `command` string between parent
(`3137651dd^`) and review commit via sha256 — identical for both hooks
(`configure-outbound-policy` `8df69b49ae42295f…`, `daemon-reload`
`46979156d9adffee…`). Only additive `transaction` objects. Manifest JSON
parses clean at the review commit.

### 2. `configure-outbound-policy` idempotency — CONFIRMED from source

Read `scripts/deploy/configure_christopher_outbound.py` at the review commit.
The manifest invocation runs the mutating branch (no `--check-only`). All four
mutation paths converge on re-run:

- `_set_env_file`: rewrites matched keys in place, appends only keys not seen —
  second run rewrites identical lines, no duplication.
- `_write_policy_env`: whole-file overwrite with content derived solely from
  CLI args — trivially convergent.
- `_patch_config`: YAML load → set `outbound_allowed_chats` to same list,
  `pop(key, None)` on already-removed pins — convergent. The
  `safe_dump(...).replace("null\n", "\n")` transform is deterministic and
  round-trips (a literal string value `"null"` is quoted by safe_dump, so only
  genuine nulls are stripped).
- `_patch_constitution`: sets `never_send_replies` to fixed booleans; rebuilds
  selectors by filtering out exactly the whatsapp selectors for
  `tgg_ops_ingest`/`tgg_management` it itself emits, then re-emitting them in
  deterministic order (ops, mgmt, preserved) — second run reaches a fixed point.
- Arg constraints satisfied by the manifest command: 6 `--management-chat` +
  9 `--ops-chat`, both non-empty; `_normalize_many` dedups deterministically.

Only non-convergent side effect: `_backup` writes a fresh timestamped `.bak`
per run. Benign accumulation, no behavioral divergence — does not defeat the
idempotent classification (gate semantics = safe to re-run during transaction
recovery, which holds).

### 3. `daemon-reload` idempotency — CONFIRMED

`systemctl daemon-reload` re-parses unit files into systemd manager state and
changes no unit activation state. Canonically idempotent; safe to re-run any
number of times.

### 4. Deployed gate accepts the manifest — CONFIRMED by dry-run

- Gate source (deployed marshal release `2026-07-22T08-23-47-189Z-168a0db726db`,
  `dist/lib/pa-agent/manifest.js:13-16` + `transaction.js:88`): schema is a
  discriminated union on `behavior` — `read-only`/`idempotent` plain, or
  `compensated` requiring non-empty `compensationCommand`. The declared shape
  `{"behavior": "idempotent"}` is valid; `compensated` is not required since
  re-run converges (finding 2).
- `pcl pa-agent bundle --client tgg --agent christopher --repo <worktree@3137651dd>
  --manifest pa-agent.manifest.json --dry-run` → ok, `gapCount: 0`.
- Real bundle `tgg-christopher-20260722-105238-ba7546fdc9` +
  `pcl pa-agent deploy … --dry-run` → `ok:true`, target `tgg-app-1` resolved
  from `pa_runtime_hosts.ssh_target_alias`, registry `gapCount: 0`, both hooks
  carried through with classifications. No target mutation (dry-run).

## Notes (non-blocking)

- `.bak-<timestamp>` backup accumulation in `~/.hermes-christopher-tgg/` grows
  by ~4 files per deploy; candidate for a retention sweep someday, out of scope
  here.

## Verdict

**CLEAR.** Classifications are truthful, gate accepts, zero runtime/command
delta. No maker-code edits made.
