# Cross-provider review — TGG streamed MEDIA directive native delivery

- **Commit reviewed:** `2ef313701b67065edb332e3a495ef00f8557afaf` on `origin/worker/d17d42d8-media-capture` (hermes-pcl)
- **Maker:** Codex. **Reviewer:** CC/Fable (edna clone, WB 65b4eac4). Maker code untouched.
- **Verdict:** **CLEAR**

## What the commit does

`gateway/durable_jsonl_consumer.py` adds `_expand_captured_send` (durable_jsonl_consumer.py:1846) and one call-site change in `deliver_management_replies` (durable_jsonl_consumer.py:2152-2156): a captured streamed `send` whose body contains absolute image `MEDIA:` directives is expanded into `send_kind="media"` entries (path, first-image caption = cleaned text, ordinal), so delivery goes through the existing native media branch (`/send-media`, retained-root + signature + sha-identity durable keys) instead of leaking the local filesystem path as chat text via `/send`.

## Criteria walked

1. **Exact production failure, actual captured shape.** The expansion consumes the output of `_parse_captured_send` — the same parser that matched the leaking production entry (paturn_8a591ce1, provider msg 3EB08673C53B754077F1F2 went out via `/send` with `MEDIA:/home/pclaw/...jpg` in text). Capture shape verified against the real recorder (`gateway/replay.py:977` `record_outbound` wrapping `adapter.send` in `gateway/run.py:6247`), matching the test harness `_captured` shape. Adversarial reproduction of the 12-path production body → 12 `/send-media` calls, caption on ordinal 0 only, 12 distinct durable keys, **zero** `/send` text calls, path string absent from every payload.
2. **No widening.** Management-selector gate and anchor/freshness gates (durable_jsonl_consumer.py:2179-2200) run before any media handling. Media branch keeps retention-config-required, `resolve(strict=True)` + `is_relative_to(root)`, image-signature validation (durable_jsonl_consumer.py:2207-2220). Falsifiers pass: path traversal (`root/../secret.jpg`), symlink-inside-root→outside, wrong image signature inside root, non-management chat, missing file — all suppressed with zero bridge calls.
3. **Representation collapse.** Expanded streamed sends and `_parse_captured_media` native sends share the exact key shape `media::{chat}::{anchor}::{sha256}::{ordinal}` (durable_jsonl_consumer.py:2221-2223). Falsifier: same turn captured both as streamed directive send AND `send_multiple_images` → 2 delivered + 2 duplicate, 2 bridge calls total.
4. **No text leak on rejection.** Expansion replaces the text send entirely; a rejected directive suppresses the media entry and there is no residual text path to deliver. Falsifiers: outside-root (maker's test), `file://` form, nonexistent path, `.jpg.txt` prefix-trick, mixed valid+invalid (invalid path absent from the one surviving native payload) — no `/send`, no path text anywhere.
5. **Regex bounded to observed shape.** `_CAPTURED_IMAGE_MEDIA_RE` requires `MEDIA:` + absolute (`/` or `~/`) non-space path ending in an image extension. Retained filenames are `{24-hex}_{ordinal}.{ext}` (durable_jsonl_consumer.py:1364,1378) — no spaces, extensions from `_IMAGE_SIGNATURES` — so the production shape always matches. Prose containing `.jpg`, `/media`, etc. without a directive is untouched (falsifier passes: plain text delivered verbatim via `/send`).
6. **Tests.** Focused file at review commit: 15 passed; the 2 failures (typing-presence) fail identically on parent `20ad9e0bf` — pre-existing env class (pytest-asyncio not installed). Adjacent `test_durable_jsonl_consumer.py` + `test_media_extraction.py`: 37 passed / 14 failed on BOTH review commit and parent, identical counts, all async-mark env failures — none introduced. 11 reviewer-authored adversarial tests all pass (kept review-side, not committed to maker branch).

## Non-blocking observations (follow-up material, none gate this commit)

- **Non-image `MEDIA:` directives (.pdf, .mp4 …) still flow as text** — the image-only regex leaves them in the body, so the pre-existing path-leak class persists for non-image media. Out of scope (case photos are images; retained root only admits images), but the leak class isn't fully closed.
- `file://`-prefixed directives are matched with the scheme kept in the path group → always fail `resolve(strict=True)` → suppressed. Fail-closed (no leak), but inconsistent with `_parse_captured_media`, which strips `file://`. If a model ever emits the URI form, the message silently vanishes.
- Ordinal is part of the durable key, so streamed/native collapse relies on order alignment; both derive from the same response text so alignment holds in practice.
- Rejecting a directive also drops the accompanying caption text (fail-closed silence over partial leak) — consistent with the delivery contract.

## Evidence

- Focused suite (review commit): `15 passed, 2 pre-existing env failures` (identical on parent).
- Adjacent suites: `37 passed / 14 failed` on both review commit and parent — env class, not introduced.
- Adversarial suite (reviewer-side, 11 tests): production 12-path shape, double-representation collapse, traversal/symlink/signature/file-uri/prefix-trick/mixed-validity/no-leak, idempotent re-run, non-mgmt suppression — all pass.
