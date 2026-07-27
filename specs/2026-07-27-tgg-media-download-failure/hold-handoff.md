# TGG media-download fix — source-only hold handoff

## Verdict

Root cause is established and both source fixes are built. Deployment remains
intentionally held behind the active TGG freeze.

## Cause

The original 3,283 headline combined 2,125 intentionally media-less export
backfill placeholders with 1,158 genuine bridge failures.

Of the genuine failures:

- 678 had already-expired signed WhatsApp media URLs. Read-only probes returned
  HTTP 403, but Baileys 6.7.23 requests reupload only for 404/410.
- 480 were unexpired at capture. The four latest stored URLs returned HTTP 206
  during diagnosis, proving a transient-failure class. The bridge performed one
  attempt and discarded retry/status evidence.

The exact historical status for the transient burst is unrecoverable because
the deployed logger dropped it. Throttling is not claimed.

Detailed evidence: `diagnosis.md`.

## Source fixes

### Capture bridge (`tgg-agent`)

Branch `worker/5e839bbe-media`, head
`9ad0eb7209d7fa47b5c012b0f425f6d9765398a3`.

- 403/404/410 requests one guarded, durably audited media reupload.
- Transient network/408/429/5xx failures retry within a single overall deadline.
- Image, video, audio/PTT, and document paths use the same bounded downloader.
- Capture events preserve safe failure category/status/attempt evidence.
- Direct raw `updateMediaMessage` calls are structurally refused; malformed
  trust policy fails closed.
- Source-integrity manifest updated.

Verification: 81/81 Node tests passed independently.

### Mandatory-media give-up (`hermes-pcl`)

Branch `worker/a5a5efd2`, head
`b5bc5627d0849a20536dff0fecf2b7d556a88450`.

- Retry cap defaults to and is deployment-pinned at 5.
- At cap, the row atomically moves to quarantine/bypass so FIFO work continues.
- Quarantine retains the exact source capture envelope plus complete failure
  history without relying on the source JSONL remaining available.
- Quarantine count/status is emitted and checked by the runtime contract.
- `PermanentMediaRefusal` remains the existing non-quarantine bypass.

Verification: 71 focused tests passed independently; Ruff clean; deployment
spec validator passed.

## Independent review

Cold Claude reviews initially blocked both changes and caused corrective
commits. Both corrected sources then received `CLEAR` re-reviews. All four
review artifacts are attached beside this handoff.

## Live/freeze proof

Final read-only host verification:

- Captured `normalized.hasMedia` events since 00:00 SGT: 126.
- Empty `mediaUrls`: 0 (**0.0%**).
- `tgg-capture-whatsapp-bridge.service`: active since
  `2026-07-23 03:13:02 UTC`, `NRestarts=0`.
- Deployed bridge SHA-256 remains
  `5dfec9ee0a05facbb6fd9aa42753da2d4d0a9616ae792de04b8aae3a4ab845db`.
- `christopher-tgg-hermes.service`: active since
  `2026-07-23 23:41:50 UTC`, `NRestarts=0`.

No service, client-host file, config, row, or process was mutated. The three
current held rows were not touched. Historical media recovery remains separate.

## Release condition

Merge/deploy only through the new authorized deploy path or a later explicit
TGG touch. This WB terminals `hold`; it does not authorize deployment.
