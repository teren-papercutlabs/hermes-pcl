# Christopher inbox cutoff — copied-store proof

**WB:** `227c5ed9-e874-4e14-ac66-b23cceef5ddc`  
**Live host effect:** none. No database mutation, deploy, restart, or service-state
change was performed on `tgg-app-1`.

## Copy provenance

At `2026-07-27T04:23Z`, the live inbox was opened on `tgg-app-1` with SQLite
`mode=ro`, copied through `sqlite3.Connection.backup()` into an in-memory
database, serialized to stdout, and written under the worktree's ignored
`client-raw/` directory. The copied database passed `PRAGMA integrity_check`
with `ok`.

Copied population:

| population | count |
|---|---:|
| all ingress rows | 4,981 |
| pending | 2,513 |
| completed | 1,618 |
| skipped | 848 |
| failed | 2 |
| pending + retention complete | 2,062 |
| pending + retention bypassed | 448 |
| pending + retention held | 3 |
| fresh max pending seq | 4,990 |

The test cutoff was deliberately set to `4,980`, ten rows below the copied
maximum, so the real consumer query had a non-empty post-cutoff population to
prove. This is test data only. Live execution must take a fresh
`max(seq) WHERE status='pending'` and pass that value; the script contains no
hardcoded cutoff.

## Mutation result on the copy

Command surface:

```text
cutoff_ingress_pending.py apply
  --inbox <copied-or-live-inbox>
  --cutoff-seq <fresh-execution-time-max-pending-seq>
  --run-id <unique-run-id>
  --provenance <authority-and-WB-reference>
  --before-image <new-path>
  --confirm-apply
```

The script does not need to be deployed. From a current `hermes-pcl` checkout,
the authorized operator can stream it to the host:

```bash
CUTOFF="<fresh max(seq) where status='pending'>"
RUN_ID="tgg-d1-cutoff-$(date -u +%Y%m%dT%H%M%SZ)"
SCRIPT="deploy/tgg/christopher/scripts/cutoff_ingress_pending.py"
INBOX="/home/pclaw/.hermes-christopher-tgg/runtime/capture-inbox.db"
BEFORE="/home/pclaw/.hermes-christopher-tgg/runtime/backups/${RUN_ID}-before.json"

ssh tgg-app-1 \
  "python3 - apply --inbox '$INBOX' --cutoff-seq '$CUTOFF' \
   --run-id '$RUN_ID' \
   --provenance 'teren D1 2026-07-27; WB:227c5ed9-e874-4e14-ac66-b23cceef5ddc' \
   --before-image '$BEFORE' --confirm-apply" < "$SCRIPT"
```

`CUTOFF` must come from the runbook's fresh execution-time read, never from
this proof or its baseline.

Exact predicate:

```sql
status = 'pending' AND seq <= :cutoff_seq
```

Exact row mutation:

```sql
status = 'skipped', updated_at = :mutation_timestamp
```

It does **not** change `retention_state`, `raw_json`, `message_id`, `chat_id`,
`pa_turn_id`, or `last_error`.

Measured result:

| population | before | after |
|---|---:|---:|
| pending | 2,513 | 10 |
| skipped | 848 | 3,351 |
| consumer-selectable at/before cutoff | 2,500 | 0 |
| consumer-selectable after cutoff | 10 | 10 |
| total retention-held | 3 | 3 |

Selected/mutated rows: **2,503**. Of those, **3** were the already-known held
rows. Their `status` became `skipped`, but their `retention_state` remained
`held`; the cutoff does not clear or repair them. Tonight's separately
authorized held-row action remains required.

The script wrote a full 2,503-row before-image before changing the copied
database. Its SHA-256 was
`cc134db5bd7fc2b2a98b63892c63b9eee721fcd466b4e793ea82d43f424f1624`.
The before-image stays in ignored `client-raw/` because it contains client
message data.

Provenance is persisted separately from inbox state in:

- `ingress_cutoff_runs.provenance` — authority/reference for the run
- `ingress_cutoff_run_rows` — the exact affected seq set and prior status/time

The consumer's production selection method,
`DurableInbox.pending_chat_batches()`, returned exactly 10 records after the
copy mutation: seq `4,981` through `4,990`. Every returned seq was greater than
the test cutoff.

## Reversal proof

`restore --before-image ... --confirm-run-id ... --confirm-restore` restored
all 2,503 rows using a compare-and-swap guard: a row restores only when it is
still `skipped` with the exact mutation timestamp. All 4,981
`(seq,status,retention_state,updated_at)` tuples then matched the pristine
copied database.

The before-image is the row-level reversal mechanism. The authorized-touch
runbook's whole-database backup remains the stronger rollback for tonight.
