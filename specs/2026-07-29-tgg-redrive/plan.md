# TGG citation-rejection re-drive plan

Status: **HOLD — read-only investigation complete; no client mutation executed.**

Snapshot: **2026-07-29T00:01:02Z**

## Decision summary

The incident population is **20 unique rejected case-create intents across 16
Hermes turns**. Those intents produced **24 rejected tool calls** because four
intents were attempted twice through the native and business-write tool paths.

As of the snapshot:

- **19 of 20 rejected case-create intents are still missing**
- **0 of 20 are already landed under their intended job number**
- **1 of 20 is superseded by a later case for the same address and work**

Only the **19 still-missing intents** belong in the re-drive.

The brief's estimate of “~10 turns / 12 items” was not used as a denominator.
The durable records resolve the incident to 16 turns / 20 unique intents / 24
rejected calls.

## Population boundary and derivation

This plan covers the isolated rejection cluster from
**2026-07-27T12:13:41Z through 2026-07-27T12:26:35Z**, immediately before the
engine fix deployed at 2026-07-27T16:30Z.

The all-time Hermes census also contains older `JOB_NO_NOT_IN_SOURCE` events:
1 call on 2026-07-21 and 66 calls across 55 turns on 2026-07-22. They are not
part of the incident population described in this brief and are not silently
included in this plan.

The 20-intent population was joined across three durable surfaces:

1. Hermes state:
   - `pa_tool_calls.result_json` contains the pre-fix response vocabulary
     `JOB_NO_NOT_IN_SOURCE`.
   - `pa_tool_calls.input_json` supplies the intended `tgg_case_create`
     operation and job number.
   - `pa_turns.message_refs_json` supplies the turn-level source references.
2. Capture inbox:
   - `ingress_events.raw_json` binds each job number to an exact source
     `message_id`.
   - The original 20 rows are durable, `status='completed'`, `attempts=1`, and
     retain their original `pa_turn_id`.
3. Systems:
   - `ps_audit_log.action='case.create_rejected_citation'` records all 24
     rejected attempts.
   - The selected 19 source refs are absent from `bridge_message_log`, against
     a positive control from the post-fix live turn.

Durable counts for the incident cluster:

- `pa_tool_calls`: 24 rows with `JOB_NO_NOT_IN_SOURCE`
- distinct `pa_tool_calls.turn_id`: 16
- distinct intended job numbers: 20
- `ps_audit_log`: 24 `case.create_rejected_citation` rows covering the same 20
  job numbers

## Item classification

All intended writes were case creates. “Expected write” below means one case
whose source-derived fields come from the named message, plus its creation
observation and `case.created` audit record. No raw WhatsApp bodies, names,
phone numbers, or addresses are copied into this repository.

| # | Intended job | Original message ID / inbox seq | Hermes turn | Rejected call IDs | Current classification |
|---:|---|---|---|---|---|
| 1 | `SK/JOB/2607/2487` | `3A454D0C7D564D14CA3B` / 4991 | `paturn_57e064e628d2467f8d1cf8278b70a526` | 2223 | still-missing |
| 2 | `AM/JOB/2607/1388` | `AC080C893E1DA683F8A25479EB4B5984` / 4990 | `paturn_bd6a57c4a5a749b695edaaeb846f6ccf` | 2217 | still-missing |
| 3 | `SK/JOB/2607/2528` | `AC90CD608DE0EA58F33B3BACD6752774` / 5162 | `paturn_a61ddca3b2854dcfba150474a3e42bbf` | 2229 | still-missing |
| 4 | `PG/JOB/2607/0965` | `A5523DC755AB73643E440CE01C51A542` / 5129 | `paturn_b4302595b178421ea251cdd1c5ea17ec` | 2235 | still-missing |
| 5 | `SK/JOB/2607/2511` | `3A5DCDFD6AC39180C903` / 5135 | `paturn_5566629a636f4b06bac213aea4bdccfb` | 2245 | still-missing |
| 6 | `SK/JOB/2607/2515` | `3AB067A10CACEF36FCBF` / 5136 | `paturn_5566629a636f4b06bac213aea4bdccfb` | 2246 | still-missing |
| 7 | `SK/JOB/2607/2539` | `3A74199BF60F47A4D9A6` / 5279 | `paturn_878b3b53aa5a498ca7b8a294d7917d2f` | 2240 | still-missing |
| 8 | `PG/JOB/2607/0970` | `A50A47EF92F12F33F6B543717ED0C401` / 5171 | `paturn_53a493721b4f49b3bf4a6f73b90311cd` | 2252 | still-missing |
| 9 | `SK/JOB/2607/2518` | `3A89CFF0CF0C814C953B` / 5138 | `paturn_73be2ab4f3824ce299cd67b7ea7d92cf` | 2256 | still-missing |
| 10 | `PG/JOB/2607/0967` | `A57936DCAB2D7A4C307F6E98EE71C841` / 5174 | `paturn_df6589bc5b294835aed4ef7f29868bbf` | 2260 | still-missing |
| 11 | `PG/JOB/2607/0974` | `A5F1080D47C8998C4A44F9CF452BC40E` / 5183 | `paturn_5c83a2092e934e9f8061f2bb750b5610` | 2264 | still-missing |
| 12 | `AM/JOB/2607/1413` | `3A5238E1891BAB452829` / 5137 | `paturn_f9758ddef9ae4d9d8501f9e5d19e51ca` | 2327, 2329 | still-missing |
| 13 | `AM/JOB/2607/1431` | `ACCEA0646AFCB097C865E141400135F5` / 5161 | `paturn_0de5a30eb5564088bf17f0e7db8010f8` | 2334 | still-missing |
| 14 | `AM/JOB/2607/1446` | `AC40F875A6565D15D2334F0D82B9B458` / 5249 | `paturn_f9339b44100145a78c5473c8e06b9bd2` | 2402, 2404 | **superseded — do not re-drive** |
| 15 | `AM/JOB/2607/1445` | `3A4A15536359D6DC2401` / 5250 | `paturn_f9339b44100145a78c5473c8e06b9bd2` | 2403, 2405 | still-missing |
| 16 | `AM/JOB/2607/1451` | `3AA91EEA5AE8F1BDFDFA` / 5281 | `paturn_8cf3f600dede4094bbc9f947e0c13042` | 2430, 2432 | still-missing |
| 17 | `AM/JOB/2607/1449` | `3A8C4C96B4CCEB70AA6C` / 5358 | `paturn_58dd7efbee184156bf39ac5038c54a03` | 2437 | still-missing |
| 18 | `AM/JOB/2607/1455` | `AC6F34A24B444347BDAF475BF451003C` / 5359 | `paturn_58dd7efbee184156bf39ac5038c54a03` | 2438 | still-missing |
| 19 | `AM/JOB/2607/1456` | `AC1CA501A52C6F58A693524CD6F2BB5D` / 5360 | `paturn_d8ae4c40fef54e32b5376fc8c18e19c2` | 2445 | still-missing |
| 20 | `AM/JOB/2607/1457` | `AC15C7BC7B299FF4A1F1F488D7065163` / 5362 | `paturn_d8ae4c40fef54e32b5376fc8c18e19c2` | 2446 | still-missing |

### Current-state evidence

At 2026-07-29T00:01:02Z, the exact current-state query:

```sql
SELECT id, normalized_job_no, state, created_at
FROM cases
WHERE normalized_job_no IN (
  'SK/JOB/2607/2487','AM/JOB/2607/1388','SK/JOB/2607/2528',
  'PG/JOB/2607/0965','SK/JOB/2607/2511','SK/JOB/2607/2515',
  'SK/JOB/2607/2539','PG/JOB/2607/0970','SK/JOB/2607/2518',
  'PG/JOB/2607/0967','PG/JOB/2607/0974','AM/JOB/2607/1413',
  'AM/JOB/2607/1431','AM/JOB/2607/1446','AM/JOB/2607/1445',
  'AM/JOB/2607/1451','AM/JOB/2607/1449','AM/JOB/2607/1455',
  'AM/JOB/2607/1456','AM/JOB/2607/1457'
);
```

returned zero rows. The instrument was positive-controlled against
`AM/JOB/2607/1500`, which returned case 7078 and a `case.created` audit row.

For every original and later duplicate message ID, `case_observations` also
returned zero matching source refs, and every intended job returned zero
non-rejection audit rows.

Three items had a later duplicate source message:

- `PG/JOB/2607/0974`: later message `AC029AFE8BE3F8C6C8A7C873882D3AAB`,
  inbox seq 5717, `skipped`; no case resulted.
- `AM/JOB/2607/1431`: later message `3AAB9C1B1E515289ED64`, inbox seq
  5701, `failed`; no case resulted.
- `AM/JOB/2607/1446`: later message `3A7D060FD3DF9F6EF9F3`, inbox seq
  5767, `skipped`.

`AM/JOB/2607/1446` is excluded because a separate adjacent message,
`3AA9D1F727A8981C51FB`, created case 7085 (`WA/JOB/2607/0003`) on
2026-07-28. Case 7085 has the same address and work scope as the rejected
intent, and its creation observation binds the adjacent source message.
Re-driving the official-job message now risks creating a duplicate business
case. This is **1 superseded intent**, not an exact-job “already landed”
result.

No other incident item has a post-rejection case at the same block/unit.
Older cases at some matching addresses predate the rejected messages and
cover different job numbers; they do not satisfy these intents.

## Re-drive set

Create the exact message-ID file with these **19 IDs and no others**:

```text
3A454D0C7D564D14CA3B
AC080C893E1DA683F8A25479EB4B5984
AC90CD608DE0EA58F33B3BACD6752774
A5523DC755AB73643E440CE01C51A542
3A5DCDFD6AC39180C903
3AB067A10CACEF36FCBF
3A74199BF60F47A4D9A6
A50A47EF92F12F33F6B543717ED0C401
3A89CFF0CF0C814C953B
A57936DCAB2D7A4C307F6E98EE71C841
A5F1080D47C8998C4A44F9CF452BC40E
3A5238E1891BAB452829
ACCEA0646AFCB097C865E141400135F5
3A4A15536359D6DC2401
3AA91EEA5AE8F1BDFDFA
3A8C4C96B4CCEB70AA6C
AC6F34A24B444347BDAF475BF451003C
AC1CA501A52C6F58A693524CD6F2BB5D
AC15C7BC7B299FF4A1F1F488D7065163
```

Preserve the 16 original turn boundaries with a JSONL message-group file:

```jsonl
{"chat_id":"120363422582425366@g.us","message_ids":["3A454D0C7D564D14CA3B"]}
{"chat_id":"120363421424519051@g.us","message_ids":["AC080C893E1DA683F8A25479EB4B5984"]}
{"chat_id":"120363403845802098@g.us","message_ids":["AC90CD608DE0EA58F33B3BACD6752774"]}
{"chat_id":"120363423568509280@g.us","message_ids":["A5523DC755AB73643E440CE01C51A542"]}
{"chat_id":"120363422582425366@g.us","message_ids":["3A5DCDFD6AC39180C903","3AB067A10CACEF36FCBF"]}
{"chat_id":"120363403845802098@g.us","message_ids":["3A74199BF60F47A4D9A6"]}
{"chat_id":"120363423568509280@g.us","message_ids":["A50A47EF92F12F33F6B543717ED0C401"]}
{"chat_id":"120363422582425366@g.us","message_ids":["3A89CFF0CF0C814C953B"]}
{"chat_id":"120363423568509280@g.us","message_ids":["A57936DCAB2D7A4C307F6E98EE71C841"]}
{"chat_id":"120363423568509280@g.us","message_ids":["A5F1080D47C8998C4A44F9CF452BC40E"]}
{"chat_id":"120363421424519051@g.us","message_ids":["3A5238E1891BAB452829"]}
{"chat_id":"120363421424519051@g.us","message_ids":["ACCEA0646AFCB097C865E141400135F5"]}
{"chat_id":"120363421424519051@g.us","message_ids":["3A4A15536359D6DC2401"]}
{"chat_id":"120363421424519051@g.us","message_ids":["3AA91EEA5AE8F1BDFDFA"]}
{"chat_id":"120363421424519051@g.us","message_ids":["3A8C4C96B4CCEB70AA6C","AC6F34A24B444347BDAF475BF451003C"]}
{"chat_id":"120363421424519051@g.us","message_ids":["AC1CA501A52C6F58A693524CD6F2BB5D","AC15C7BC7B299FF4A1F1F488D7065163"]}
```

## Mutation procedure for Edna's separate execution

This section is a plan only. None of it was run during this investigation.

1. Re-run the current-state classification immediately before mutation.
   Remove any item that has since landed or been superseded. Regenerate the
   message-ID and group files, and set `--expected-total` to that re-measured
   still-missing population.
2. Stop `christopher-tgg-hermes.service` and verify it is inactive. The
   one-shot command uses the same singleton lock as the ordinary consumer;
   a refused lock is a hard stop, not a reason to bypass it.
3. Create a protected run directory under
   `/home/pclaw/.hermes-christopher-tgg/runtime/` and persist:
   - the exact message-ID file;
   - the exact message-group file;
   - a read-only SQLite backup of current `tgg.db`;
   - the bounded command's inbox before-image;
   - the bounded command's source-evidence before-image;
   - the bounded audit output.
4. Before execution, independently validate all 19 identities:
   - each ID selects exactly one inbox row;
   - selected raw `messageId` equals the selected ID;
   - selected raw `chatId` equals the inbox `chat_id`;
   - the 19 IDs partition exactly into the 16 declared groups;
   - every row is still one of the intended job-bearing source documents.
5. Execute the production bounded-backplay contract from
   `/home/pclaw/apps/hermes-pcl`:

```bash
/home/pclaw/apps/hermes-pcl/.venv/bin/python -B \
  -m gateway.durable_jsonl_consumer bounded-backplay \
  --inbox /home/pclaw/.hermes-christopher-tgg/runtime/capture-inbox.db \
  --config /home/pclaw/.hermes-christopher-tgg/config.yaml \
  --state-db /home/pclaw/.hermes-christopher-tgg/state.db \
  --case-db /home/pclaw/.systems-pcl/data/tenants/tgg.db \
  --canonical-env /etc/systems-papercut-labs/tgg.env \
  --message-id-file "$RUN_DIR/message-ids.txt" \
  --message-group-file "$RUN_DIR/message-groups.jsonl" \
  --requeue-selected \
  --before-image "$RUN_DIR/inbox-before.json" \
  --inject-source-evidence \
  --source-before-image "$RUN_DIR/source-evidence-before.json" \
  --expected-total 19 \
  --batch-size 25 \
  --audit "$RUN_DIR/audit.json" \
  --lock-file /home/pclaw/.hermes-christopher-tgg/runtime/capture-consumer.lock \
  --run-id tgg-jobno-redrive-20260729
```

The required source-evidence injection is message-ID scoped and idempotent:
existing identical refs are preserved; divergent identities fail closed. It
closes the original gap because the Systems citation gate reads
`bridge_message_log`, while these durable source rows currently exist only in
the capture inbox.

The bounded command must report:

- `selection.total = 19` for the re-measured still-missing population;
- `zero_real_sends = true`;
- `outbound_sent = 0`;
- inbox row conservation preserved;
- exactly the selected message IDs processed;
- no failed or retryable group.

Do not use a cursor rewind or a broad time-window replay.

6. Keep the ordinary consumer stopped while the post-checks run. A partial
   run is not retried blindly: classify every item from current state, then
   form a new exact still-missing set.
7. Restart `christopher-tgg-hermes.service` only after all post-checks pass,
   then verify the service and its consumer status from the live runtime.

## Expected business writes and per-item post-verification

For each of the 19 rows below, the expected business result is:

1. exactly one `cases` row under the intended normalized job number;
2. a creation observation whose `fields.source_refs` contains the exact
   original message ID;
3. one `ps_audit_log.action='case.created'` record for the job number;
4. one `bridge_message_log` row bound to the exact source message ID.

| Job parameter | Message parameter |
|---|---|
| `SK/JOB/2607/2487` | `3A454D0C7D564D14CA3B` |
| `AM/JOB/2607/1388` | `AC080C893E1DA683F8A25479EB4B5984` |
| `SK/JOB/2607/2528` | `AC90CD608DE0EA58F33B3BACD6752774` |
| `PG/JOB/2607/0965` | `A5523DC755AB73643E440CE01C51A542` |
| `SK/JOB/2607/2511` | `3A5DCDFD6AC39180C903` |
| `SK/JOB/2607/2515` | `3AB067A10CACEF36FCBF` |
| `SK/JOB/2607/2539` | `3A74199BF60F47A4D9A6` |
| `PG/JOB/2607/0970` | `A50A47EF92F12F33F6B543717ED0C401` |
| `SK/JOB/2607/2518` | `3A89CFF0CF0C814C953B` |
| `PG/JOB/2607/0967` | `A57936DCAB2D7A4C307F6E98EE71C841` |
| `PG/JOB/2607/0974` | `A5F1080D47C8998C4A44F9CF452BC40E` |
| `AM/JOB/2607/1413` | `3A5238E1891BAB452829` |
| `AM/JOB/2607/1431` | `ACCEA0646AFCB097C865E141400135F5` |
| `AM/JOB/2607/1445` | `3A4A15536359D6DC2401` |
| `AM/JOB/2607/1451` | `3AA91EEA5AE8F1BDFDFA` |
| `AM/JOB/2607/1449` | `3A8C4C96B4CCEB70AA6C` |
| `AM/JOB/2607/1455` | `AC6F34A24B444347BDAF475BF451003C` |
| `AM/JOB/2607/1456` | `AC1CA501A52C6F58A693524CD6F2BB5D` |
| `AM/JOB/2607/1457` | `AC15C7BC7B299FF4A1F1F488D7065163` |

Run this query separately for every row, substituting that row's `:job` and
`:message_id`. It is the per-item acceptance check, not an aggregate proxy:

```sql
WITH selected_case AS (
  SELECT id, normalized_job_no, state, created_at
  FROM cases
  WHERE normalized_job_no = :job
)
SELECT
  (SELECT COUNT(*) FROM selected_case) AS exact_case_count,
  (SELECT COUNT(*)
   FROM case_observations o
   JOIN selected_case c ON c.id = o.case_id
   JOIN json_each(json_extract(o.fields, '$.source_refs')) r
     ON r.value = :message_id) AS source_bound_observation_count,
  (SELECT COUNT(*)
   FROM ps_audit_log
   WHERE action = 'case.created' AND target_id = :job) AS created_audit_count,
  (SELECT COUNT(*)
   FROM bridge_message_log
   WHERE source_ref = :message_id) AS source_evidence_count;
```

Accept an item only when all four counts are exactly `1`. If the agent emits a
clarification, attention item, rejection, or no case instead, hold that item
for specific review; do not broaden or repeat the replay.

Final population statement after a successful run must be phrased with its
denominator, for example: “19 re-driven still-missing case intents, of which
19 landed and 0 remain missing as of `<timestamp>`; the twentieth rejected
intent remained excluded as superseded by case 7085.”

## Rollback boundary

The bounded run is reversible only inside its captured blast radius:

- inbox status restoration comes from `inbox-before.json`;
- injected source evidence restoration comes from
  `source-evidence-before.json`;
- business-state restoration comes from the timestamped SQLite backup and the
  run-scoped `ps_audit_log` delta in `audit.json`.

Any rollback is a separately reviewed client-data mutation. Do not improvise
deletes from counts or restore the entire database while the ordinary consumer
is running.
