# TGG media-download failure diagnosis

Measured 2026-07-27 against the live capture artifact and service journal on
`tgg-app-1`. Every client-host command was read-only. No service, file, config,
row, or process was mutated.

## Population correction

The prior 3,283 / 15,791 headline combined two different populations. In the
current 15,903-media-event capture population:

- 2,125 events are `source=export_backfill` placeholders. Their raw
  `imageMessage` contains only `caption` / `fileName`; there is no URL, media
  key, direct path, or byte path for the bridge to download. These are not
  capture-bridge download failures. Recovery from the 23-Jul exports is the
  separate historical-recovery workstream.
- 1,158 events are genuine bridge download failures: 1,153 history events and
  5 live notify events.

The populations travel separately below. Treating all 3,283 as one failure
class hid both the real bridge mechanism and the deliberately media-less export
backfill.

## Root cause

The bridge's inbound media path was a single `downloadMediaMessage` attempt.
On any exception it logged only `err.message`, discarded the HTTP status and
retryability metadata, emitted `hasMedia=true` with an empty `mediaUrls`, and
continued. There was no bridge-owned retry.

The Baileys 6.7.23 implementation retries through `reuploadRequest` only for
HTTP 404 and 410 (`REUPLOAD_REQUIRED_STATUS = [410, 404]`). The failure
population demonstrates both uncovered classes:

1. **Expired signed media URLs:** 678 / 1,158 genuine failures had an `oe`
   timestamp already in the past when capture attempted them (median 8.62 days
   expired; range 0.22–14.60 days). Ten read-only probes of this class returned
   HTTP 403. HTTP 403 does not enter Baileys' 404/410 reupload branch, so the
   stale URL is never refreshed.
2. **Transient fetch failures with no retry:** 480 / 1,158 genuine failures had
   an `oe` timestamp still in the future at capture. The four most recent
   failures (23–24 Jul) remain fetchable today and returned HTTP 206 from the
   exact stored URLs, proving the media was not permanently absent. The bridge
   made one attempt and abandoned it. The exact historical HTTP status for the
   remaining burst cannot be recovered because the deployed logger discarded
   it; throttling is therefore unproven, not asserted.

Journal evidence matches the path: 1,158 download errors total (1,119 image
stream failures, 31 document stream failures, 4 video stream failures, 3 image
transport `fetch failed`, 1 document decrypt error). The 3-Jul spike was 1,132
errors. The failure spans image/document/video and successful and failed file
sizes overlap, so size/type-specific rejection is not the mechanism.

## Current leakage

Population: every captured media event since 00:00 SGT on 27 Jul, measured at
12:25 SGT.

- total: 112
- missing local media path: 0
- live failure rate: **0.0%**

The same result holds for PcL's 05:00 operating-day boundary. The defect remains
latent because the deployed code is still single-attempt, but it was not
actively leaking in today's observed 112-event population.

## Mandatory-retention consequence

The three 24-Jul notify failures are the three current mandatory-media holds.
Read-only inbox inspection showed 93,994 attempts/failures per row (281,982
combined) by 12:31 SGT. Each full raw event remains in `raw_json` (529–600
bytes), but the uncapped selector retries all three every consumer cycle. This
is the reason the repo fix adds a cap and a durable quarantine/bypass outcome.
The rows themselves are not touched in this work item.

## Fix shape

- Bridge: bounded retry; request reupload on 403/404/410; retry transient
  network/408/429/5xx failures with bounded backoff; log safe structured
  status/attempt evidence; wire image/video/audio/document through one path.
- Consumer: after the configured/default retention failure cap, mirror the
  existing `PermanentMediaRefusal` durable bypass shape, preserve `raw_json`
  and failure history, record a queryable quarantine disposition, and remove
  the row from the retry candidate set.
- Deployment: none. Both changes remain source-only behind the active freeze.
