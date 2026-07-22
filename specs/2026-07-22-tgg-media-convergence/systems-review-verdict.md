# Cross-provider review verdict: Systems TGG media convergence

- Review WB: 1f6249fe-69d8-4b8c-8022-2b27ec66c1d8 (parent d17d42d8, builder 765a1eb5)
- Target: systems-papercut-labs `origin/worker/a5a36f41` @ `a19fccb415e69c6e50fc5664023e2401975d3f66`
- Reviewer: edna clone on claude-fable-5 (cross-provider vs codex maker)
- Date: 2026-07-22

## Verdict: CLEAR — no blocking defects

## Independently verified (fresh worktree at the commit, own runs)

- Focused set (`tgg-domain`, `media-backfill-compat`, `bobby-api`, `tgg-media-convergence`): **80/80 pass** — matches maker claim exactly.
- Full suite: **373/373 pass**; `pnpm run build` exit 0.
- Falsifiers from the brief, each checked against source:
  - **No schema/migration change**: diff touches only README.md, package.json (one script line), scripts/tgg-media-reconcile.ts, routes.ts, store.ts, new test file. schema.ts untouched.
  - **Exact-id/hash only**: route recomputes `source_key = whatsapp-capture-v1:sha256(chat_jid+NUL+message_id)` and refuses mismatch; reconcile matches by exact capture-map basename (ambiguous>1 held) and exact export-token ledger lookup. `timestamp` is stored metadata only, never a matching input. No LLM anywhere.
  - **Manifest transaction-bound**: apply opens manifest `wx`, writes it inside the `beforeCommit` callback of the single `BEGIN IMMEDIATE` batch; write failure → ROLLBACK + manifest unlinked (tested); occupied manifest path refused with DB untouched (tested).
  - **CAS reversal holds changed rows**: reversal compares `sha256(ledger row)` vs `appliedAfterHash` and observation `afterFields` verbatim; conflicts held. Reversal UPDATE columns gated by an allowlist that matches the live `message_ledger` schema 1:1 (28 columns) — injection-safe and fail-closed on manifest drift.
  - **Identity conflict refuses**: both natural keys resolved; different rows → 409 `IDENTITY_CONFLICT`. Changed digest/mime/ref at same ordinal → 409 `PROVENANCE_DIVERGENCE`.
  - **Refs opaque + path-contained**: route regex + decode rejects `.` `..` and separators; store requires `/media/tgg/hermes/` prefix and resolves by basename set-membership from `readdir` (no path resolution → no traversal). `getCaseMedia` never emits filesystem paths.
  - **README is the only guidance co-deliverable**; documents both routes, exact-id contract, dry-run/apply/reverse, integrity/rollback.
  - **No production mutation**: tests run under per-process `mkdtemp` PS_DATA_DIR (tests/setup.ts); reconcile subprocess inherits it.
- Adversarial probes beyond maker tests (throwaway test, run then deleted): (1) observation edited after apply → reversal holds it, later edit survives; (2) inserted ledger row newly referenced by a post-apply observation → deletion held; (3) encoded traversal `%2e%2e%2f` refused at the store layer, not just the route. All passed.

## Non-blocking observations

1. `convergeMessageMediaBatch` scans all `case_observations` per input message — O(messages × observations). Fine at current scale (~11k msgs, ~50 obs) for the one-shot migration; would degrade if observations grow large.
2. Store-level `MEDIA_NOT_FOUND` thrown after the route's pre-validation (race: file deleted between check and converge) is not in the route's 400 map → surfaces as 500. Unreachable in normal operation; cosmetic.
3. Dry-run's `observationsChanged` is a predictor heuristic; apply output overrides it with actual counts. Labeled correctly in output.

Merge gate: cleared from this reviewer's side. No maker code was changed.

---

# Round 2: production-shape correction — re-review

- New target: `origin/worker/a5a36f41` @ `01c0a4ce957f2079563208bab30cf3915e0360df` (6 commits past `a19fccb`; net +271/-55 across README, reconcile script, store.ts, tests)
- Context: maker's live dry-run against production capture data found the reconcile parser did not match real capture shapes; a same-worktree commit race (ab1e61a/b84824a "unidentified concurrent" edits) was reverted and restored; final tree is what I reviewed.

## Verdict (round 2): CLEAR — no blocking defects

## Independently verified at 01c0a4c (fresh worktree, own runs)

- Focused convergence file **10/10**, full suite **374/374**, build exit 0 — all match maker claims.
- **Capture matching** now prefers production `normalized.messageId` / `normalized.chatId` / `normalized.mediaUrls`; media URLs join to exactly one basename in the media root (URL parse failure → unmatched; >1 candidate → held). Still exact-only.
- **Export matching rewritten**: `export_backfill_<zone>_<24hex>_<ordinal>-PHOTO` files join only to observation source refs `EXPORT_BACKFILL_<zone>_<ts>_<24hex>` on exact zone + trailing 24-hex hash. The timestamp inside the ref string is NOT a matching input — multiple distinct refs sharing zone+hash are held as ambiguous (tested), multiple ledger rows held, capture-vs-ledger source_key mismatch held, ordinal collisions held. Exact-only contract preserved.
- **CAS fix is a real correctness improvement**: the manifest now snapshots every observation that cited an inserted alias (including unchanged ones), so reversal can distinguish pre-existing citations (reversible) from post-apply citations (held). Round 1's semantics would have made an inserted row with any pre-existing citation permanently irreversible.
- Adversarial probes (throwaway test, run then deleted): (1) inserted row with pre-existing citation now reverses cleanly — ledger row deleted, observation restored; (2) referenced-but-unchanged observation edited after apply → whole mutation held, later edit survives. Maker's own new test covers the post-apply-citation hold.
- README documents the production shapes, export semantics, updated reversal contract, and corrected `pnpm tgg:media-reconcile` syntax (exercised via `pnpm --silent` in tests). No schema change; manifest still transaction-bound; dry-run remains pure-read (asserted: no ledger row created for ambiguous fixtures).

## Non-blocking observations (round 2)

1. Ledger inserts hardcode `source='whatsapp-capture-v1'` even for export-backfill-keyed rows (`export-backfill-v1:` source_key); source_key carries true provenance, but the `source` label is misleading for that subset.
2. Export-only inserts with no ledger/capture context use placeholder `chat_jid = export-backfill:<zone>:<hash>` — deterministic and greppable, but not explicitly documented in README.
3. Commit history on the branch is noisy (fix → revert → fix → restore from the worktree race); net tree is coherent. Squash-merge would keep main clean.

Merge gate: cleared from this reviewer's side at 01c0a4c. No maker code was changed.

---

# Round 3: one-pass fixed point + coupled reversal — re-review

- Target: `origin/worker/99130591` @ `84a660ad3939ac18750bb56c6de5f165f23545d5` (deploy branch; supersedes intermediate `cd0c669` which parent blocked on coupled reversal)
- Context: production apply (10,735 ledger rows, 25 observations) was not a one-pass fixed point — immediate dry-run predicted 3 more observation changes, because observations were refreshed per-message against intermediate ledger state. `cd0c669` restructured the batch into two phases (converge ALL ledger rows, then refresh each affected observation ONCE from final state, same `BEGIN IMMEDIATE`), with a virtual CAS chain in the manifest (first matching mutation owns before→final; later ones assert final→final). Parent caught the coupled-reversal blocker in that shape (edit one ledger post-apply → sibling mutation could still reverse, clobbering the shared observation); `84a660a` fixes it by grouping manifest mutations into connected components (union-find over shared observation ids), preflighting the whole component (ledger hash CAS + simulated observation-chain CAS + insert-reference guard) before mutating anything, and applying or holding each component atomically.

## Verdict (round 3): CLEAR at 84a660a — no blocking defects

## Independently verified (fresh worktree, own runs)

- Focused convergence file **10/10**, full suite **374/374**, build exit 0.
- Scope check: vs my cleared `01c0a4c` content, only `store.ts` + tests differ (plus an unrelated sender-key deployment doc); reconcile script, routes, README, schema byte-identical. No schema/config/exact-match regression.
- Two-phase batch is a fixed point by construction: second run finds identical ledger rows → identical derivation → 0/0.
- Legacy chained manifests (real before→intermediate→final chains, i.e. the manifest already produced by the production apply under prior code) remain reversible under the new component logic — maker test constructs one and it unwinds to the exact initial fields.
- Adversarial probes beyond maker tests (5, throwaway file, run then deleted — all passed):
  1. 3-message shared observation (one message pre-existing with legacy natural keys, cited only via stored `source_ref`; two inserted later in batch — the production topology): ONE pass lands all 3 media, immediate replay 0/0, reverse restores the observation exactly once, deletes only the 2 inserted rows, pre-existing row survives with its legacy source_key.
  2. **Coupled adversary (the parent's blocker case)**: two inserted messages feed one observation; post-apply edit to ONE ledger row → whole component held (`conflictsHeld: 2`), observation byte-identical, both ledger rows survive including the edit. No partial reversal, no clobber.
  3. Component isolation: an unrelated third mutation in the same manifest still reverses (its row deleted, its observation restored) while the coupled pair holds.
  4. Later observation edit holds ALL connected mutations; the edit survives; ledger rows survive.
  5. Audit: exactly one `message.media_converged` row per effective mutation; the shared observation's change attributed to exactly one mutation; replay writes zero audit rows; a held reverse writes zero reversal audit rows.

## Non-blocking observations (round 3)

1. `conflictsHeld` now counts mutations (component members), not components — operator reading the reverse report should know a single conflict can surface as N held counts. Consistent with tests; worth a line in README on next touch.
2. Round-2 non-blocking notes (ledger `source` label for export-keyed inserts, placeholder chat_jid documentation, noisy history → squash-merge) still stand.

Merge gate: cleared from this reviewer's side at 84a660ad3939ac18750bb56c6de5f165f23545d5. No maker code was changed.
