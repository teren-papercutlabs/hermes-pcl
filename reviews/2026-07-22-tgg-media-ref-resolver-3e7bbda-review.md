# Cross-provider review — TGG streamed case-media ref resolver

- **Commit under review:** `3e7bbdadaac94dcf47ea69f014c7143c3e5dd49b` ("fix(tgg): resolve streamed media refs") on `origin/worker/d17d42d8-media-ref-send`
- **Parent WB:** d17d42d8 · **Review WB:** bed9f83f
- **Reviewer:** edna clone (CC / claude-fable-5), independent of maker
- **Date:** 2026-07-22

## Verdict: CLEAR

## What the commit does

1. Broadens `_CAPTURED_IMAGE_MEDIA_RE` from extension-gated (`.png|.jpe?g|.gif|.webp`) to any `MEDIA:` + `file://`/`~/`/`/`-rooted token.
2. Adds `_resolve_captured_media_path(raw_path, retention)`: maps an exact `<media_ref_prefix>/<single-basename>` opaque reference onto `media_root/<basename>`; keeps absolute local paths and safe `file://` URIs as before; both forms terminate at `resolve(strict=True)` + `is_file()` + `is_relative_to(media_root)`.
3. Delivery site in `deliver_management_replies` swaps the inline resolve for the helper. Everything downstream (content-signature image validation, sha256 identity, durable claim, bridge `/send-media` POST) unchanged.

## Contract verification

- **ref_prefix always defined and shape-validated.** `_retention_config` (untouched, line 1135) defaults `ref_prefix` to `/media`, strips trailing `/`, and refuses any non-`/media`-rooted or traversal-bearing configured prefix. No KeyError path; the config surface itself constrains prefixes.
- **Containment is symlink-consistent.** `retention["root"]` is `.resolve()`d at config load; candidate is `resolve(strict=True)`d before `is_relative_to(root)` — a symlink inside media_root pointing outside is refused (probe-verified), a symlink resolving inside root is allowed.
- **Opaque-ref strictness.** Basename must be non-empty, not `.`/`..`, contain no `/`, `\`, or `%`, and equal its own `Path(...).name`. Query/fragment/CR/LF markers refuse the whole reference before any parsing. `file://` percent-decoding happens before the basename checks, so encoded traversal (`%2e%2e%2f`, `..%2F`, double-encoded `%252e`) decodes into characters the checks refuse.
- **Absolute retained-root paths + safe file:// still supported.** Probe-verified for bare path, `file://<abs>`, `file://localhost<abs>`, and `file://` addressing the ref prefix. Foreign netloc (`file://evil.example/...`) refused.
- **Mismatched/sibling prefixes cannot send or leak.** `/media/tgg/other/...`, `/media/tgg/hermesX/...`, case variants fall to the absolute-path branch and die at `resolve(strict=True)` or root containment → suppressed. Because `_expand_captured_send` replaces the original text send with media entries, a suppressed ref never re-enters as chat text — end-to-end probe confirms zero bridge calls on a hostile ref embedded in prose.
- **Production shape.** Maker test `test_streamed_case_media_ref_resolves_to_retained_file_and_native_send` asserts exactly one `POST /send-media` with `filePath` under media_root, `mediaType: image`, caption preserved without the MEDIA ref, scalar `replyTo`, and no `/send`. Re-run and passing.
- **No other surface changed.** Diff touches only `gateway/durable_jsonl_consumer.py` (regex + helper + one call-site swap) and the test file. No config, allowlist, token, pause, destination, or bridge-transport change. Management-selector gating, gate-epoch checks, durable claim, and strict-success recording untouched.

## Test evidence

- Focused maker suite `tests/gateway/test_consumer_reply_delivery.py`: **21 passed, 2 failed** — both failures are `pytest.mark.asyncio` typing-presence tests failing for lack of `pytest_asyncio` in this review env; **confirmed identically failing on parent commit `2f68e8359`** → pre-existing, not attributable.
- Independent adversarial probe suite (30 tests, written and run by reviewer, then deleted — not maker code): **30 passed.** Coverage: happy ref, absolute-path and file:// support, 21 hostile refs (traversal, encoded/double-encoded traversal, backslash, nested, double-slash, empty basename, bare `.`/`..`, query, fragment, foreign netloc, prefix-sibling, prefix-exact, mismatched prefix, case variant, nonexistent, empty string), `~/` outside root, symlink escape refused / contained symlink allowed, end-to-end hostile-ref zero-bridge-calls no-text-leak, non-image file under root suppressed, retention-disabled suppressed.

## Non-blocking observations

1. **Regex broadening is fail-closed but coarser.** Any `MEDIA:/…` token in assistant prose is now stripped from text and, if unresolvable, suppresses the entire reply (caption included) rather than delivering the text without it. Trailing prose punctuation glued to a ref (e.g. `…x.jpg.`) is captured into the path and will suppress. Live emitter (`tgg_case_media`) emits the ref on its own line, so this is theoretical; the fail-closed direction is correct for a private-path surface.
2. The extension gate moved from the regex to `_validated_image_type`'s content-signature check — strictly stronger (content-based, not name-based).
