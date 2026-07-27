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
prove. This is test data only. Live execution must take the backfill-end
boundary from WB `34186991`'s terminal output and pass that value;
the script contains no hardcoded cutoff.

## Mutation result on the copy

Command surface:

```text
cutoff_ingress_pending.py apply
  --inbox <copied-or-live-inbox>
  --cutoff-seq <fresh-execution-time-max-pending-seq>
  --run-id <unique-run-id>
  --provenance <authority-and-WB-reference>
  --before-image <new-path>
  --consumer-lock-file <consumer-singleton-lock>
  --expect-selected-count <plan-output-selected-count>
  --confirm-apply
```

`apply` acquires the same non-blocking singleton lock as the durable consumer.
It refuses while Christopher's consumer is running, and also refuses if any
row at/below the cutoff is still `processing`. For tonight, the consumer must
be stopped before the cutoff and remain stopped through the remaining
authorized steps; the final flag-flip restart starts it once. This closes the
read-batch/claim race and prevents an old processing row from reappearing below
the cutoff after the mutation.

The script does not need to be deployed. From a current `hermes-pcl` checkout,
the authorized operator can stream it to the host:

```bash
CUTOFF="<backfill-end seq from WB 34186991 terminal output>"
RUN_ID="tgg-d1-cutoff-$(date -u +%Y%m%dT%H%M%SZ)"
SCRIPT="deploy/tgg/christopher/scripts/cutoff_ingress_pending.py"
INBOX="/home/pclaw/.hermes-christopher-tgg/runtime/capture-inbox.db"
BEFORE="/home/pclaw/.hermes-christopher-tgg/runtime/backups/${RUN_ID}-before.json"
LOCK="/home/pclaw/.hermes-christopher-tgg/runtime/capture-consumer.lock"

# With the consumer stopped, run plan first and inspect selected_count.
ssh tgg-app-1 \
  "runuser -u pclaw -- python3 - plan --inbox '$INBOX' \
   --cutoff-seq '$CUTOFF'" < "$SCRIPT"
EXPECT="<selected_count from that plan output>"

ssh tgg-app-1 \
  "runuser -u pclaw -- python3 - apply --inbox '$INBOX' --cutoff-seq '$CUTOFF' \
   --run-id '$RUN_ID' \
   --provenance 'teren D1 2026-07-27; WB:227c5ed9-e874-4e14-ac66-b23cceef5ddc' \
   --before-image '$BEFORE' --consumer-lock-file '$LOCK' \
   --expect-selected-count '$EXPECT' --confirm-apply" < "$SCRIPT"
```

`CUTOFF` must come from the backfill's own terminal output, never from this
proof, the inbox's touch-time maximum, or the pre-flight baseline.

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
authorized held-row action remains required. Because the retention alarm only
counts held rows whose status is pending/processing, the cutoff temporarily
removes those three rows from that alarm. The explicit three-seq clear must
therefore run in the same stopped-consumer window before restart; it cannot be
left for the retention worker to rediscover. It must target seqs
`4575/4576/4577` directly and verify those rows' `retention_state` values
directly; retry-journal silence is not proof after the cutoff.

The script wrote a full 2,503-row before-image before changing the copied
database. Its SHA-256 was
`de044ae5356338f1332dba91ab5885ddfb57f0a68f97b2c1e2148805407e5d12`.
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

`restore-audit --run-id ... --consumer-lock-file ... --confirm-restore`
restored all 2,503 rows using the in-database row audit and a compare-and-swap
guard: a row restores only when it is still `skipped` with the exact mutation
timestamp. All columns of all 4,981 `ingress_events` rows then matched the
pristine copied database. The provenance/audit tables intentionally remain as
an immutable record of the applied-and-reverted run; reversal restores client
row state, not the pre-run schema.

The full before-image is the independent row-level reversal artifact. The
audit tables can restore the two changed fields even if that file is lost.
The authorized-touch runbook at
`/Users/pcloffice/pcl-biz/_agents/edna/specs/2026-07-27-tgg-authorized-touch/runbook.md`
still requires a whole-database backup first; that remains the strongest
rollback for tonight.
