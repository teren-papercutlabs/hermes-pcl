# Christopher create-contract rerun

Tree under test: this worker branch. Synthetic replay only; business writes
terminated at the loopback fixture store. No client host, Box surface, channel,
or production database was touched.

| Class | Result | Evidence |
|---|---|---|
| 1 — foreign photos / turn lumping | **RESOLVED** | Create cites only `gate-a-report`; the unrelated 8802 observation cites its caption and two photos. `evidence/comparison.json → foreign_photos` |
| 3 — replay duplicates | **RESOLVED** | Two identical requests resolve to one stored observation keyed by the normalized source-ref set. `evidence/comparison.json → replay_dedupe`; Systems route test covers the real store. |
| 4 — `observed_at` | **RESOLVED** | Both replay attempts bind `1784545200`, the cited ingress epoch. `evidence/comparison.json → observed_at` |
| 5 — priority | **RESOLVED** | Create carries explicit top-level `priority: URGENT`. `evidence/comparison.json → priority` |
| 6 — due date | **RESOLVED** | Receipt-only date becomes midnight SGT; `dueAt` is receipt + 30 days. All four due fallbacks use one shared constant. `evidence/comparison.json → due_date`; `evidence/static-store.txt` |
| 7 — contact fields | **RESOLVED** | `contactName` and `contactPhone` are top-level create fields and reach the store route. `evidence/comparison.json → contact_fields` |
| 8 — supplied work items | **RESOLVED** | Opening items are top-level create fields and persist in the opening observation; existing-case labels remain canonical. `evidence/comparison.json → work_items`; Systems route test. |
| 11 — field-by-field hunt | **RESOLVED** | All targeted contract fields are explicit; cited refs are current-turn validated; observed time is middleware-bound; store replay is idempotent. `evidence/comparison.json → open_hunt` |

`evidence/comparison.json` is the mechanical verdict. All eight requested
classes report `pass: true`.

The remaining `7 * 86400` match in `store.ts` is a seven-day recent-activity
lookback, not a due-date fallback, and was deliberately left unchanged.

## Suite and review evidence

- Systems: typecheck clean; **378/378 tests passed**.
  `evidence/systems-typecheck.txt`, `evidence/systems-tests.txt`
- Hermes task-focused: **95 passed**; the sole failure is the existing
  constitution state-claim assertion reproduced on `origin/main` `2f9a481ef`.
  `evidence/hermes-focused.txt`, `evidence/hermes-origin-known-failure.txt`
- Hermes full suite: **23,626 passed**. Its 105 failures were grounded by
  rerunning the exact nodeids: 74 reproduced on `origin/main`; the other 31
  passed when rerun on this branch outside the contaminated full-order run.
  No persistent branch-only failure remains.
  `evidence/hermes-tests.txt`, `evidence/hermes-origin-subset.txt`,
  `evidence/hermes-transient-rerun.txt`
- Independent Claude review: **CLEAR** on the two-repository implementation
  and **CLEAR** on the timestamp/field-stripping follow-up.
  `evidence/cross-provider-review.md`,
  `evidence/cross-provider-review-incremental.md`
