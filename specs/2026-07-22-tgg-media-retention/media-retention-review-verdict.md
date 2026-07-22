CROSS-PROVIDER REVIEW VERDICT: BLOCK — commit b5bf7e9d7 (origin/worker/7d8c92cd, hermes-pcl)

Reviewer: edna clone (CC/Fable, cross-provider vs codex maker), WB 113bed88. Independently reran 142 focused tests (all pass), read full diff + final files, and ground-truthed against the LIVE tgg-app-1 capture stream and consumer inbox.

ROOT CAUSE OF ALL BLOCKERS: the retention/preflight code was written against a guessed capture-event schema; the live schema differs. Live events nest under `normalized.*` and `normalized.mediaType` is a COARSE type ("image", "video", "document", "audio") — NOT a MIME string. The 142 tests pass because fixtures encode the same wrong guesses (top-level fields, "image/jpeg" mediaTypes, files always present).

BLOCKING DEFECTS

B1. Non-image media wedges the entire consumer — LIVE DATA HIT.
gateway/durable_jsonl_consumer.py:988-990 (`retain_record_media`): no media-type filter; every media item goes to `_validated_image_type`, which raises MediaRetentionError on video/document/audio bytes (:867). Live proof: production inbox has 4 PENDING video events with capture paths right now (first seq 2306, chat 120363421424519051@g.us); live stream carries 104 video + 47 document + 3 audio events. Same wedge for image events with hasMedia and no mediaUrls (:966-969) — 3,222 such events exist in the historical stream shape.

B2. MediaRetentionError escapes run_consumer → crash-restart loop, whole-fleet outage.
:1801-1803 re-raises after requeue; :1851 (`await task`) re-raises in the main loop, which catches only CancelledError (:2099). One failing chat batch (a video, a missing file, OR any transient Systems outage during convergence) kills the process; systemd restarts; the same batch re-claims; permanent crash loop across ALL chats. Parent plan says retention failure "returns the claimed row to pending" — the row, not the process. deployment.yaml's own failureDisposition is retry-pending.

B3. Missing/unreadable source file → terminal `failed`, silent event loss.
:885 `resolve(strict=True)` raises FileNotFoundError (not MediaRetentionError) → generic handler :1804-1806 → `inbox.finish(status="failed")`. The event is never retried AND never model-processed. Violates the explicit plan invariant "never generic terminal failed" for retention failures. Zero such files in today's pending set, but the capture-download race makes it live post-activation.

B4. Activation media preflight is vacuous on production data — safety gate is a no-op.
deploy/tgg/christopher/scripts/processing_activation_transaction.py:~675-706 (`media_backlog_preflight`): (a) reads `event.get("item") or event.get("payload") or event` — live events carry media under `normalized` (the consumer's own `_bridge_item` docstring records this exact 2026-07-21 first-light lesson); (b) skips events where mediaType doesn't startswith("image/") — live value is "image", so even with the right envelope EVERY image is skipped. Result: preflight scans 0 events and passes; "activationBacklogPreflight: all-image-paths-from-current-cursor-must-resolve" (client-agent-deployment.yaml) is asserted but not delivered.

REQUIRED FIX SHAPE (maker's choice on detail): image-only filter on declared type in retain_record_media (non-image events process WITHOUT retention); catch OSError in the retention path → MediaRetentionError; contain retention failure per chat (held/pending with loud status+health) instead of process death; fix preflight envelope (`normalized`) + coarse-type semantics; add live-shape fixtures (normalized envelope, mediaType "image", hasMedia-without-urls, video event) so this class cannot regress green.

CLEARED / VERIFIED
- 142 focused tests independently rerun: pass.
- inter_session_ops.md untouched (full name-only diff checked).
- No deploy occurred: deployed /home/pclaw/apps/hermes-pcl has zero occurrences of retain_record_media; services untouched.
- Demo JID 120363426509183563@g.us used in tests; real client group 120363407903158826@g.us appears ONLY in pre-existing selectors/allowlists (identical counts at parent a071a6cdc — not introduced here).
- Management scope flip in tests is NOT a smuggled widening: scope tests were ALREADY failing 8/22 at the parent commit; the constitution granted management the full 23-op registry in a prior ratified commit. This commit reconciles stale tests + adds tgg_case_media (mgmt-only) and denies tgg_case_media/tgg_media_retention to ingest. Correct.
- tgg_case_photos: job-no regex gate, opaque-ref-only resolution, containment + byte-signature + MIME-consistency checks, dedupe, graceful no-media. Sound. (Minor: resolve(strict=True) FileNotFoundError via tool_error can leak an under-root internal path for missing refs — suggest mapping OSError → generic INVALID_MEDIA_REF.)
- Media delivery: mgmt-selector + anchor-in-this-result + gate-epoch guards shared with text path; retained-root containment + image validation pre-claim; per-media durable key (chat::anchor::content-hash::ordinal); strict 200+success; unknown outcome durable-undelivered, never retried. Native send signatures (send_image_file/send_multiple_images) verified against gateway/platforms/base.py — parser positional assumptions correct. Capture guard list includes both kinds (gateway/run.py:6256-6261).
- Retention saga: atomic tmp+fsync+rename, O_EXCL, ordinal/digest/MIME provenance refusal, idempotent replay, convergence envelope strictly validated (ok:true + ledgerChanged/observationsChanged), Systems payload matches the parent-plan contract (source_key=whatsapp-capture-v1:sha256(chat NUL msg), media items source_key/media_ordinal/digest/mime/ref).
- pa.enabled=false: zero file/DB mutation (test verified); disabled status carries retention fields with null free-percent.
- Generator/validator/slots: all three slots regenerated from canonical sources, SHA256SUMS consistent, mgmt max_output_tokens 8192 has a real read-site (gateway/run.py:1344), pause/allowlists/scope invariants asserted; deployment.yaml contract landed same-commit.

NON-BLOCKING NOTES
- N1: live mediaType "image" (no slash) means the declared-MIME divergence branch never engages in production; provenance is byte-signature-only in practice. Acceptable, but intent drift worth knowing.
- N2: `_retention_status(inspect_media=True)` os.walks the full media tree (~11k files) every 2s poll iteration. Works; wasteful.
- N3: text sends are delivered before media regardless of capture order — caption/photo ordering may read oddly in chat. Cosmetic.
