# S3 review attempt 98a0d9d5 — finding 3 reconciliation (out-of-diff, not actioned)

- Review attempt: `98a0d9d5-4e50-4f51-af45-dd5f3ae2398a` (fresh non-authoring session, textual verdict BLOCK)
- Reviewed surface: commit `28f5816edfd9bc567a8ca009b44fd3abd0b92b58` (branch `worker/9057826a`)
- Merge base with main at review time: `e430b7341327deece30ac19eb6865e6695933a69`
- Written: 2026-08-03, while closing the review fixes at WIP `82556a36ad26f6a6e49d2bbb5e9857f180898bc7`

## The three findings and their disposition

1. **Provenance gate accepts null `approved_date`** — VALID, fixed. `compliance/196-protection-alternatives.yaml`
   (the only artifact carrying `approved_date: [null]`) and the whole model-supplied-slot mechanism are removed;
   `hermes_cli/pa_compose.py` now refuses any required provenance field that is null, blank, or a null-bearing
   list, with fixture-based fail-closed tests plus a strict non-null assertion over the live MTU source set.
2. **Protected wording remains model-readable** — VALID, fixed. The compliance YAMLs are out of
   `reference/020-knowledge-manifest.yaml`, so they are no longer reachable through `pa_knowledge_fetch`;
   `sync_pa_knowledge` now derives them from `response_policy.output_assembly.artifacts` and labels them
   `visibility: runtime-only` in the sync manifest, with tests asserting the model-visible set stays disjoint.
3. **"Patch creates a live runtime cutover path"** — FALSE, out-of-diff. No code change; reconciled below.

## Why finding 3 is out-of-diff

The finding names three things: `scripts/bootstrap_local.sh` being an active runtime-home writer, the README
documenting `HERMES_HOME=~/.hermes-mtu` execution, and `scripts/deploy_guarded.py` being removed. None of the
three is a change made by the reviewed commit.

- **`bootstrap_local.sh` and `README.md` are untouched by this branch.**
  `git diff --name-only e430b734..HEAD -- deploy/finexis/mtu/scripts/bootstrap_local.sh deploy/finexis/mtu/README.md`
  returns nothing. Both files last changed in `ec8d6a061511324e2c6dde11e0eed6cfeb05e995`
  ("fix pa knowledge runtime context and profile isolation", 2026-08-02 21:38), which is an ancestor of the
  merge base — i.e. pre-existing main state that the S3 work inherited, not state it created.

- **`deploy_guarded.py` was never on this branch, so it cannot have been removed by it.**
  The only deletion in `e430b734..HEAD` is `compliance/196-protection-alternatives.yaml` (the finding-1 fix).
  `deploy_guarded.py` (with `judge_eval_report.py`, `mtu_eval_policy.py`, `run_nightly.py`) was added to main
  by parallel MTU work AFTER the merge base — `3d6baa2c0c` → `b0d277adf8` → `3060d76330`, none of which is an
  ancestor of `e430b7341`. The file is present at `origin/main` and absent at the merge base; the branch simply
  predates it. The reviewer compared the worktree against the moving `origin/main` tip rather than the reviewed
  commit's base, so a parallel ADD on main read as a DELETE on the branch.

- **The no-live-cutover scope actually held.** Nothing in this branch writes `~/.hermes-mtu` or
  `~/pcl-run/hermes-mtu`. The A/B harness (`scripts/run_eval_corpus.py`) reads a runtime source and refuses a
  target equal to or under `~/.hermes-mtu`, staging every run in a disposable copy; all four A/B reports record
  `"live_home_written": false`. The live gateway home was read-only input throughout, and no deploy step ran.

## Consequence

No code change for finding 3. Findings 1 and 2 are closed in code and tests. When this branch merges, the
parallel-line deploy scripts (`deploy_guarded.py` and siblings) arrive from main untouched by the merge.

## Recorder defect noted by the reviewer (carried forward, not fixed here)

The reviewer's response began with a bare `BLOCK`, but `dev.external_review_attempts` persisted
`verdict = unknown` because the parser only recognises `Verdict: BLOCK` forms. Attempt `98a0d9d5` must not be
read as a clearing review from its stored verdict field. This is a review-recorder defect outside the MTU
scope and is left for the owning surface.
