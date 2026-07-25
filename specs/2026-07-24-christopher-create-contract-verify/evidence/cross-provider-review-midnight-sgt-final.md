# External Review — claude

**Prompt:** Re-review the final Hermes midnight-SGT patch after the prior BLOCK. Verify every prior blocker is resolved: no free-text evidence scan or first-wins ambiguity; locale-independent explicit date parsin...

---

## Verification of the prior blockers

All five prior blockers are resolved in the worktree (not just in the patch text):

1. **Free-text evidence scan / first-wins nondeterminism — fixed.** `_case_receipt_source_values` (`tools/pa_business_tools.py:1858-1872`) now yields only explicit keys in a fixed code-defined order: top-level `receiptDate`/`receipt_date`, then `evidence.receiptDate`/`receipt_date`/`job_receipt_date`, then a string-valued `receivedAt`. No iteration over arbitrary evidence values, so dict insertion order is no longer load-bearing. `test_unrelated_evidence_text_does_not_override_epoch` (`tests/test_pa_business_facts.py:1949`) pins that `evidence.messageText` containing "receipt date: 1 June 2026" does **not** override — and the anchored `fullmatch` (`:1828`) genuinely rejects it.
2. **Generic write path — covered.** `_prepare_case_create_source_refs:2172` uses the shared helper; `test_generic_create_path_normalizes_and_strips_receipt_source:1963` asserts normalized `receivedAt`, `dueAt`, and `receiptDate` stripping on that path. It is the path `pa_business_write` actually takes (`:2083-2087`).
3. **Locale dependence — fixed.** `strptime` is gone; `_MONTH_NUMBERS` (`:1794-1814`) is an explicit lowercase map and `datetime(...)` is constructed directly with `tzinfo=_SGT`. The `%d/%m/%Y` silent DD/MM guess was dropped entirely, which also closes that prior "important".
4. **Missing tests — added and correct.** Epoch pass-through (`:1949`), neither-supplied error plus no-write (`:1984`), stripping before `_handle_tgg_write` (`:1931`), schema shape (`:1994`), override logging parametrized over two non-midnight epochs (`:1933`). Arithmetic checks out: `1_784_476_800` = 2026-07-20T00:00+08, `dueAt` = +2 592 000 on both paths. The `0`-epoch bug from the old `or` chain is fixed by the `in (None, "")` checks (`:1861`, `:1869`, `:1883`).
5. **Constitution and slots — updated.** Source (`christopher_tgg_constitution.yaml:166-169`), the snippet patch, and all three `runtime-slots/*` copies now say pass `receiptDate` and "Do not calculate midnight or dueAt". A repo-wide grep finds no surviving "stated receipt epoch" / "use midnight at the start of that date" outside the review docs. The single-quoted YAML scalars fold newlines to spaces, so the three new `build_runtime_slots.py:594-596` asserts match the rendered `joined`.

I cannot verify the `runtime-slots/SHA256SUMS` digests without hashing (read-only, no shell); `verify_runtime.sh:110` is the gate for that.

## Remaining blocker

**A present-but-unparseable `receiptDate` silently falls back to the model's non-midnight epoch.** `_normalise_case_receipt_epoch:1886-1889` does `if normalised is None: continue` and then returns `supplied_epoch` — no error, no warning. Only two formats parse: `D Month YYYY`/`D Mon YYYY` and `YYYY-MM-DD`. HDB job sheets commonly write `20/07/2026`, `20-07-2026`, `20-Jul-2026`, or `Jul 20, 2026`; none match (`\s+`-separated only). In that case, if the model supplied both `receiptDate` and a computed `receivedAt` — which the constitution's own "When the source supplies an actual timestamp, pass it as receivedAt" makes a realistic partial-compliance shape — the wrong non-midnight epoch reaches the ledger and sets the 30-day `dueAt`, which is exactly the defect this patch exists to remove, now failing open and unlogged. When `receiptDate` is the *only* source it fails closed on the corrective error, so the gap is specifically the both-supplied case.

Minimal fix: when `receiptDate`/`receipt_date` (or `evidence.receipt_date`) is non-empty and `_date_only_sgt_epoch` returns `None`, return a `tool_error` naming the accepted formats instead of falling through — plus a test. Widening the accepted separators to `[\s/-]` and adding `Mon D, YYYY` is worth doing alongside, but the refusal is the load-bearing half.

## Important (non-blocking)

- Conflicting explicit sources (top-level `receiptDate` = one date, `evidence.receipt_date` = another) resolve to top-level silently, with no warning. Deterministic now, so not a blocker, but a conflict log would be cheap.
- `receiptDate`'s schema description (`:2766-2773`) never states the accepted formats. Given the blocker above, spelling out "e.g. `20 July 2026` or `2026-07-20`" materially reduces the failure mode.
- The code reads `evidence.receipt_date` while the same constitution instruction says "operational fields never belong under free-form evidence" — the helper tolerates a shape the prompt forbids. Harmless, but the tolerance is what the override test exercises (`:1942`), so it's now load-bearing in tests.
- `receivedAt` is declared `"type": "integer"` while `test_create_requires_epoch_or_date_source` passes `None`; the omit-it shape is the only provider-safe one, so the description should say "omit", not imply null is acceptable.
- The `_create` helper reorder (`:1881-1885`) flips it to caller-args-win. Correct for these tests and no existing test in the class supplies those keys, but it silently changes the helper contract for future tests.

VERDICT: BLOCK