# External Review — claude

**Prompt:** Final cold re-review after the second BLOCK. Verify invalid explicit receiptDate values now fail closed even when receivedAt is also supplied; common job-sheet date-only formats normalize locale-indep...

---

## Verification against the second BLOCK

I re-read the worktree files, not the patch text.

**Prior blocker — unparseable `receiptDate` with `receivedAt` also supplied — resolved.** `_normalise_case_receipt_epoch` (`tools/pa_business_tools.py:1895-1903`) now raises on *any* explicit receipt source that fails to parse, instead of `continue`-ing to the model epoch. `_handle_tgg_case_create:2020-2023` and `_prepare_case_create_source_refs:2200-2203` both convert that to `tool_error` before any write. Pinned by `test_unparseable_explicit_receipt_date_fails_closed` (`tests/test_pa_business_facts.py:2019`), which supplies both fields and asserts `captured == {}` — the fail-open path is gone on both entry points.

**Formats.** `_date_only_sgt_epoch:1833-1870` is locale-independent: no `strptime`, explicit lowercase `_MONTH_NUMBERS` map, `datetime(..., tzinfo=_SGT)`. All four regexes are anchored `fullmatch`, so `evidence.messageText` prose can't match (`test_unrelated_evidence_text_does_not_override_epoch:1949`). Coverage now includes `D Mon YYYY`, `D-Mon-YYYY`, `D/M/YYYY`, `Mon D, YYYY`, `YYYY-MM-DD`; out-of-range components (`07/20/2026`, `2026-02-30`) hit `ValueError` → `None` → hard error, i.e. fail closed. Arithmetic verified independently: `1784476800` = 2026-07-20T00:00+08, `dueAt` = +2 592 000 = `1787068800`.

**Conflicts.** Two explicit sources parsing to different epochs raise (`:1904-1908`); `set()` means differently-spelled same dates don't false-trip. Test at `:2030`.

**Contract across both paths.** Direct handler (`:2031-2035`) and generic `pa_business_write` path (`:2221-2223`, reached via `_handle_business_call:2111-2115`) both set `receivedAt`, overwrite `dueAt` = receipt + 30d, and strip `receiptDate`/`receipt_date`/`received_at` before the HTTP payload. `test_generic_create_path_normalizes_and_strips_receipt_source:1963` covers the generic path. The `0`-epoch truthiness bug is gone (`in (None, "")` checks). Override is logged (`:1912`) and asserted over two non-midnight epochs.

**Constitution/schema/tests aligned.** Source constitution `:166-169`, the snippet patch, and all three `runtime-slots/*` copies carry the same wording; single-quoted YAML folds to the exact strings the three new `build_runtime_slots.py:594-596` asserts require. Repo-wide grep finds no surviving "stated receipt epoch"/"use midnight at the start of that date" outside review docs. Schema adds `receiptDate` (needed, since `additionalProperties: false`) with examples, drops `receivedAt` from `required`, and no existing test asserts the old shape.

## Non-blocking

- `_SEPARATED_DATE_ONLY_RE` accepts a numeric month, so `06/07/2026` is silently read DD/MM. Correct for HDB SG sheets and it fails closed on the unambiguous US case, but the assumption is undocumented — worth a one-line comment.
- `_case_receipt_source_values:1882` and the following `def` are separated by zero blank lines. Ruff config selects only `PLW1514`, so no gate fails; cosmetic.
- `receivedAt` is `"type": "integer"` while the corrective path expects omission; the description could say "omit" explicitly rather than implying null is fine.
- Dry-run `pa_business_write` (`:971`) skips `_prepare_case_create_source_refs`, so dry-run output shows un-normalized fields. Pre-existing, and no write occurs.

I could not verify `runtime-slots/SHA256SUMS` digests (read-only, no shell); `verify_runtime.sh` remains the gate. I also did not execute the test suite — findings above are from reading the code and tests.

VERDICT: CLEAR