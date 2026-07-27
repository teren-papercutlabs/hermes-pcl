# External Review — claude

**Prompt:** Independent cold code review. Review the WhatsApp bridge media download hardening against this contract: diagnose showed 403 expired signed URLs bypass Baileys 404/410 reupload and other transient fai...

---

## Verdict: BLOCKED

One blocker, plus concrete defects below. Scope note: this is a read-only source review; I did not execute the test suite and cannot verify the `deploy/source-integrity.json` sha256 values or any host state — `structural.test.mjs` enforces those hashes, so that check is only as good as an actual test run.

The core contract is otherwise met: 403/404/410 → forced reupload (`media-download.js:139-141`, and Baileys' own path is confirmed dead here — `getHttpStream` throws a Boom with `output.statusCode`, while `Utils/messages.js:836` gates on `error.status`, which Boom never sets, so the wrapper is genuinely the only reupload path); transient set is 408/429/5xx + network codes + timeout; permanent fails on attempt 1; all four media routes go through `downloadInboundMedia` with types preserved; evidence carries no URL or error message and is proven by test.

### Blocker

**1. `sock.updateMediaMessage()` is an un-guarded, un-audited outbound stanza, added on the inbound path, while the rung is `off` (`bridge.js:158`).**
Baileys `updateMediaMessage` is not a local operation: `messages-send.js:1011-1018` builds `encryptMediaRetryRequest(...)` and calls `sendNode(node)` — an encrypted media-retry request emitted to the sender's device. This change therefore adds a bridge-initiated outbound emission that:
- bypasses `trustPolicyResolver` / `releaseLeaseResolver` / `outboundAudit` entirely, so it produces no decision row in the ledger the post-flip detector reasons over;
- fires for any peer or group JID that sent media and passed the mention gate, including JIDs outside `effectiveJids` (which is empty in `off`);
- can fire **twice per message**, because `reuploadCompleted` is only set on success (`media-download.js:167`), so a failing reupload leaves the flag false and a second 403 re-triggers it.

`structural.test.mjs`'s `rawMutationPatterns` covers `sendMessage`/`sendPresenceUpdate`/`groupCreate`/`groupParticipantsUpdate` only, so the invariant test that exists precisely to catch "raw Baileys mutation outside `guarded-transport.js`" returns green on this. Either route the reupload through the guarded transport as its own mutation class (recommended: `mutationClass: 'inbound-media-retry'`, allowed in `off`, audited), or make it an explicit documented exception in `SECURITY-REACHABILITY.md` — and in both cases extend `rawMutationPatterns` with `updateMediaMessage` so the exception is enforced rather than incidental. Shipping an unrepresented outbound path in a system whose whole design premise is "every outbound Baileys call is policy-checked and audited" is not a call to make silently.

### Important

**2. Worst-case inbound stall grows to ~225s per failing media message, on the exact path the fix exists to protect.**
`downloadInboundMedia` is awaited inline in `enqueueMessages`, *before* `appendCaptureEvent` for that message and every later message in the batch. With defaults (45s timeout, 3 attempts) a 403 whose sender is offline costs 3 downloads + 2 reuploads ≈ 225s of blocked capture, versus one 45s stall pre-change. Bounded, but a ~5x regression in head-of-line latency for the incident class that motivated this. Add an overall per-message deadline budget (e.g. total ≤ 1.5 × `MEDIA_TIMEOUT_MS`, checked between attempts), or capture first and hydrate media afterwards.

**3. Media failure is invisible in the durable ledger.**
On failure the normalized event is written with `hasMedia: true`, `mediaType` set, and `mediaUrls: []`, with no failure marker (`bridge.js`, image/video/audio/document catch blocks). The only record is a stderr `media_unavailable` line, which has different retention than `events.jsonl`. A consumer cannot distinguish "media saved" from "media permanently lost" from "not attempted." Add `mediaState`/`mediaError` (category + statusCode + attempts) to the event before `appendCaptureEvent`.

**4. The bridge-level catch throws away the diagnostics the downloader computed.**
`code: err?.code || 'MEDIA_DOWNLOAD_FAILED'` is always the constant, since `MediaDownloadError.code` is fixed. `err.category`, `err.statusCode`, and `err.attempts` are populated and dropped — precisely the fields needed to tell "expired reference, reupload refused" from "CDN 5xx" in the journal.

**5. No bridge-level regression test for the incident class.**
`media-download.test.mjs` is good unit coverage, but nothing asserts the integration property that actually failed in production: a hung/failing download must still result in a capture-ledger append for that message and must not stall the batch. `structural.test.mjs` only greps for four call sites. `bridge-http.test.mjs` already injects a fake socket via `__setBridgeSocketForTests`; an enqueue-level test with a hanging `downloadMediaMessage` is cheap here.

### Minor

**6. Malformed env values fail hard in non-obvious ways.** `Math.max(1, parseInt('x', 10))` is `NaN`, so `WHATSAPP_MEDIA_MAX_ATTEMPTS=abc` makes `downloadMediaWithRecovery` throw `TypeError('maxAttempts must be a positive integer')` on *every* media message — all media silently unavailable forever. Likewise a non-numeric `WHATSAPP_MEDIA_TIMEOUT_MS` yields `setTimeout(..., NaN)` → instant timeout on every attempt. Use `Number.isFinite` guards falling back to the defaults.

**7. New knobs are undocumented.** `WHATSAPP_MEDIA_TIMEOUT_MS`, `WHATSAPP_MEDIA_MAX_ATTEMPTS`, `WHATSAPP_MEDIA_RETRY_BASE_MS` appear in neither `deploy/env.example` nor `README.md`.

**8. Abandoned reuploads leak listeners and can mutate the message late.** Baileys calls `waitForMsgMediaUpdate(check)` with no timeout (`promiseTimeout` returns an un-timed promise when `ms` is falsy), so after our 45s `withTimeout` gives up, the `messages.media-update` and `connection.update` listeners stay registered until the next disconnect, and a late success mutates `content.url`/`directPath` in place on the same `msg` object the ledger may already have serialized. No unhandled rejection (the promise is inside `Promise.race`), and disconnects are frequent enough to bound the leak — but worth a comment at minimum.