# Cross-provider review: b6240e2d integration conflict resolutions

Review merge commit e334ca80d8708aa78df359e1786b3df9d444ed2f, whose parents are current main 2f9a481ef and site-concurrency branch 98effd45f. The merge had seven conflicted files. The resolved tree intentionally equals current main because current main already independently contains the branch-side scheduler behavior plus later xlsx, retention, bounded replay, citations, and manager-turn changes; the merge records ancestry without reverting any later behavior.

Issue CLEAR only if inspection proves the resolved files retain both sides:

Main/later features:
- spreadsheet admission: `_event_spreadsheets`, `validate_tgg_spreadsheet`, `PermanentMediaRefusal`
- retained media gating and source citations / manager full-turn compatibility
- bounded replay provider-error/runtime-config behavior

Branch feature contract:
- `pending_chat_batches` per-chat scheduler
- reserved management lanes and site concurrency bound
- `TGG_DEMO_MANAGEMENT_ONLY`
- atomic chat batches, isolated replay context, source-native reply key
- deployment values `--site-concurrency 4 --chat-batch-size 25`

Conflict-resolution policy used:
- kept current-main additions whenever the branch alternative was an older omission
- preserved auto-merged branch scheduler code
- kept module-based systemd ExecStart from main, with branch concurrency flags
- kept all tests from both histories
- restored `hashlib` after detecting the older branch removed its import while newer media delivery still uses it

Check for subtle interaction bugs, especially shared runner concurrency, config binding, retention-before-claim ordering, task exception handling, and reply deduplication. Verdict must be exactly CLEAR or BLOCKED with concrete findings.
