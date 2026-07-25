# External Review — claude

**Prompt:** Cold review this Hermes date-normalization patch. Contract: date-only job-sheet receipt strings must mechanically normalize receivedAt to 00:00:00 Asia/Singapore, override any non-midnight model epoch...

---

## Findings

Verified against the worktree (`tools/pa_business_tools.py:1793-1861`, `:1958-1970`, `:2135-2156`, `tests/test_pa_business_facts.py:1873-1946`), not just the patch text. The core arithmetic is right — `1_784_476_800` is exactly 2026-07-20T00:00+08, `dueAt` = +2 592 000 on both paths, and `_coerce_observed_at_epoch` is untouched when no date-only source exists.

### Blockers

**1. The evidence free-text scan can silently overwrite a correct receipt with an unrelated date.**
`_case_receipt_source_values` (`:1828-1830`) iterates *every* top-level string value in `evidence` and treats the first one matching `\breceipt\s*date\b` as authoritative over the model epoch. Evidence routinely carries pasted WA text — the existing tests themselves use `evidence: {"messageText": "previous job SK/JOB/2603/1709 not attended"}` (`:1916`) — which can quote a *different* case's job sheet. There is no guard: no check that the extracted date is consistent with the supplied epoch's date, no refusal when two evidence values yield conflicting dates (first-wins, and "first" is model-controlled dict insertion order, so this is not deterministic across calls). This writes to the ledger and sets the 30-day due date. Explicit `receiptDate`/`evidence.receipt_date` keys are a fine authoritative source; the blob scan needs either removal or a conflict/consistency gate.

**2. The generic `pa_business_write` path is entirely unverified.**
`_prepare_case_create_source_refs` (`:2122`) got the same substantive change, and a repo-wide grep shows **zero** tests reference it — both new tests call `_handle_tgg_case_create` only. The contract names both paths. Also missing: a regression test that an epoch/ISO `receivedAt` with no date-only source is still passed through unchanged (the new function now sits in front of every create), a test that `receiptDate`/`receipt_date` are stripped before `_handle_tgg_write` (the backend body contract per `evidence/cross-provider-review.md:9` is explicit top-level fields), a test for the neither-supplied error, and a schema test that `receiptDate` exists and `receivedAt` is no longer required.

**3. `%d %B %Y` / `%d %b %Y` are LC_TIME-locale-dependent.**
`datetime.strptime` resolves month names through the process locale. If the runtime host's `LC_TIME` is not English, `"20 July 2026"` returns `None` from `_date_only_sgt_epoch` and the code silently falls back to the model's wrong epoch — the exact bug this patch exists to fix, failing open with no log. Either match month names explicitly or assert the locale; no test pins this.

### Important

- **Constitution not updated.** `deploy/tgg/christopher/christopher_tgg_constitution.yaml:166-167`, `patches/ops-ingest-judgment.snippet.yaml:100-101`, and all three `runtime-slots/*` copies still say "pass receivedAt as the job sheet's stated receipt epoch; when it states only a date, use midnight at the start of that date in Asia/Singapore" — i.e. they still instruct the model to do the arithmetic that this patch exists to take away, and never mention `receiptDate`. The schema description alone is competing with a direct prompt instruction.
- **`%d/%m/%Y` newly accepts previously-rejected input.** `_coerce_observed_at_epoch("05/06/2026")` used to return the string → `tool_error`. It now silently resolves to 5 June under a DD/MM assumption. Plausible for SG, but it is a silent semantic guess feeding a due date, and it is untested.
- **Regex phrasing coverage.** `_RECEIPT_DATE_RE` only matches "receipt date". Job sheets commonly say "Date Received" / "Received Date" / "Date of Receipt", none of which match.

### Minor

- Schema declares `receivedAt: {"type": "integer"}` while the new test passes `receivedAt: None`; a provider enforcing the declared type may reject an explicit null, so the "omit it" shape is the only safe one and should be what the description says.
- `receivedAt: ""` no longer falls through to `received_at` (the `is None` check at `:1845-1847` replaced the `or` chain). The `0`-value fix is an improvement; the empty-string case is a narrow regression.
- The test helper reorder (`:1881-1885`) makes caller args win over defaults. Correct for the new tests, and no existing test in the class supplies those keys — but it silently changes the helper's contract for future tests.

VERDICT: BLOCK