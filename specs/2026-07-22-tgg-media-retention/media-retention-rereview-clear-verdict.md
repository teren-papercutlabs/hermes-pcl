CROSS-PROVIDER RE-REVIEW VERDICT: CLEAR — a0e0fe245 (origin/worker/7d8c92cd, hermes-pcl)

Scope: fix commits f00629b39 + a0e0fe245 on top of blocked b5bf7e9d7. Reviewer independently inspected the full fix diff, reran the focused suite, generator check, and deployment validator at a0e0fe245. No maker code changed by reviewer.

ALL FOUR BLOCKERS RESOLVED, verified at code + test level:

B1 (non-image wedge) FIXED. `_event_media` (durable_jsonl_consumer.py:891-907) now derives coarse kind via split("/")[0] — handles both live coarse types ("video") and MIME ("video/mp4") — and returns [] for non-image events; per-item mimes are filtered the same way. `_retain_record_media_impl` returns zero-retention for non-image events BEFORE the hasMedia raise, so the 4 pending production videos bypass retention and process normally. Proven by `test_production_normalized_video_is_not_retained` and `test_pending_production_video_bypasses_retention_and_completes` — both using the real envelope ({type: whatsapp_capture_event, normalized: {...}}, coarse mediaType).

B2 (whole-consumer crash loop) FIXED. run_consumer's done-task await (:1891-1897) now catches MediaRetentionError, logs HELD/PENDING, and keeps the daemon and other chat lanes alive; the --once gather uses return_exceptions and re-raises only non-retention exceptions (fail-loud preserved for genuine bugs). Status surfaces `state: held-pending` + `retention_hold` (newest unresolved media-retention-retry last_error), and a successful retry clears last_error (record_retention sets last_error=NULL). Proven by `test_one_chat_retention_hold_does_not_kill_other_chat` (MISSING stays pending, HEALTHY completes, daemon exits 0).

B3 (OSError → terminal failed) FIXED. `_contained_existing_file` wraps resolve(strict=True) into MediaRetentionError; the whole retention path is wrapped by `retain_record_media` normalizing OSError → MediaRetentionError (:1082-1089), so missing/racing capture files requeue pending per the retry-pending contract instead of terminally failing the event. Proven by `test_media_retention_normalizes_source_read_oserror` and `test_production_normalized_missing_image_stays_pending_and_once_survives` (pending + retention_failures=1 + held-pending status).

B4 (vacuous preflight) FIXED. `media_backlog_preflight` now reads the `normalized` envelope first and treats is_image as coarse "image" OR "image/..." — parity with the runtime filter. Non-image backlog is skipped; an image event with no capture path holds activation. Fixtures rewritten to live shape. Proven by `test_media_backlog_preflight_proves_all_pending_images_resolvable` (normalized + coarse type → events=1/images=1, non-vacuous), `..._skips_normalized_video_event`, `..._holds_normalized_image_without_cache_path`.

INDEPENDENT VERIFICATION AT a0e0fe245
- 149/149 focused tests pass (142 prior + 7 new live-shape tests).
- build_runtime_slots.py --check: ok, slot hashes identical to the reviewed commit's SHA256SUMS (fix commits touch only consumer/preflight code + tests — zero config/constitution/slot drift).
- validate_deployment_spec.py: passes; `failureDisposition: retry-pending` is now actually true in code.
- Changed files across b5bf..a0e0: exactly 4 (activation script, consumer, 2 test files). inter_session_ops.md still untouched; no deploy occurred.

NON-BLOCKING NOTES (for tier-2 activation, no code change required before merge)
- N1: preflight reads cursor-forward only, but the un-retained backlog largely sits in the already-staged INBOX (1,269 pending media events today, incl. the 4 videos and any future evicted-file images). With per-chat containment the consequence is bounded and visible, but the orchestrator should sweep pending inbox media paths as an operational pre-activation step, since the preflight will not see them.
- N2: an image event whose mediaType is absent ("" coarse kind) silently skips retention rather than holding. Live data always carries mediaType (20,625/20,625), so this is theoretical; noting for completeness.
- N3: a held management-chat image blocks that chat's FIFO until the file is restored or the event is manually addressed — by design ("held, never guessed"), surfaced via held-pending status; worth watching in the 24h measurement window.

VERDICT: CLEAR for merge. Tier-2 (deploy, controlled live proof hard-bound to 120363426509183563@g.us, reconciliation, 24h window) remains with the orchestrator per parent plan d17d42d8.
