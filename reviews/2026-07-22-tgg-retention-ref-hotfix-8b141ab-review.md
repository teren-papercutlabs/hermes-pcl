# Cross-provider review — TGG Hermes retention root/ref contract hotfix

- **WB:** d9c1ba0f-ca43-48d7-b474-1a1b7068661e
- **Reviewer:** edna clone (Fable provider), session e5ef8017
- **Maker commit:** `8b141ab56d95532724b11ced9bcf581194591250` on `origin/worker/c6357030-media-root`, repo `hermes-pcl`
- **Base:** `origin/main d0b0efbec0eef8e8a372437d4ed650e59e958987` — verified exact parent of the maker commit.
- **Date:** 2026-07-22

## VERDICT: CLEAR for merge/deploy

## Live falsifier being fixed

After deploying d0b0efb, the consumer retained files under `/home/pclaw/.systems-pcl/data/media` and emitted `/media/<basename>`; the live Systems `POST /api/operator/messages/media-retention` route only accepts `/media/tgg/hermes/<basename>`. ~500 rows held on `media retention convergence returned an invalid Systems envelope`.

## Checks

### 1. Configs — PASS
Canonical `deploy/tgg/christopher/config.yaml` + all three runtime slots (`gpt-5.4-mini`, `gpt-5.6-luna`, `gpt-5.6-luna-low`) set:
- `media_root: /home/pclaw/.systems-pcl/data/media/tgg/hermes`
- `media_ref_prefix: /media/tgg/hermes`

Parsed all four YAMLs at the exact commit; `shasum -a 256 -c SHA256SUMS` passed byte-exact (constitution sums unchanged).

### 2. Generic retention contract — PASS
- Single ref emission site: `gateway/durable_jsonl_consumer.py` `_retain_record_media_impl` emits `f"{config['ref_prefix']}/{target.name}"`. No other hardcoded `/media/` emission remains.
- Default `/media` preserved when `media_ref_prefix` absent — generic (non-TGG) contract intact.
- Prefix validated fail-closed in `_retention_config`: must be `/media` or start `/media/` with no empty/`.`/`..` segments; internal `//`, relative, `/mediax`, `/other/...` all rejected (exercised — see §5).
- Basename is `{24-hex}_{ordinal}.{ext}` with ext from magic-byte sniffing (`_IMAGE_SIGNATURES`) — no slash/query/fragment surface.
- Path escape guards intact: `_contained_existing_file` (source containment), `target.is_relative_to(root)` (target containment).

### 3. Systems-side contract match — PASS
Route source (`src/tenants/tgg/routes.ts:1290`): `ref.match(/^\/media\/tgg\/hermes\/([^/?#]+)$/)`, decoded basename must not be empty/`.`/`..`/contain `[\\/]`, and `mediaRefResolves` gates on basename existence in `tggHermesMediaDir()` = `<psDataDir>/media/tgg/hermes` (`media-index.ts:44-46`). With psDataDir `/home/pclaw/.systems-pcl/data`, the new `media_root` is exactly the directory Systems serves and indexes — root and ref aligned end-to-end.

### 4. No drift — PASS
Diff vs base is exactly 9 files, all retention-scoped (4 configs, SHA256SUMS, slot builder, spec validator, consumer, consumer test). `pa.enabled: false` everywhere; systemd units, deployment spec, constitution, pause/allowlist/token/bridge/processing gates untouched. Validator reports `processing_enabled: false`.

### 5. Tests + adversarial fixture — PASS
- `build_runtime_slots.py` re-run at the commit: byte-identical regeneration (`git status` clean), internal `_validate` asserts (incl. new retention dict) passed.
- `validate_deployment_spec.py --app-root . --spec client-agent-deployment.yaml`: `ok: true`, slot hashes match SHA256SUMS.
- `tests/gateway/test_durable_jsonl_consumer.py`: **34/34 passed** under the repo venv. (An initial run showed 7 failures under sandbox system Python 3.14 lacking `pytest-asyncio` — environmental, not code.)
- Reviewer adversarial fixture (12/12, written against the exact live Systems regex, then removed — not maker code):
  - retained ref matches `^/media/tgg/hermes/([^/?#]+)$`; decoded basename has no `\\/`, not `.`/`..`; retained file provably under configured root.
  - default-prefix (no `media_ref_prefix`) emits `/media/<name>` which the Systems regex rejects — reproduces the live falsifier, proving the fix is load-bearing.
  - 8 malformed prefixes (`/media/../etc`, `/media/tgg/../hermes`, `/media//tgg`, `media/tgg/hermes`, `/mediax/tgg`, `/other/tgg/hermes`, `/media/.`, `/media/./tgg`) all raise `media retention ref prefix is invalid`.
  - trailing-slash prefix normalises (no `//` in ref); empty root fails closed.

## Ops note for deploy

Replaying the ~500 held rows re-retains from `/var/lib/tgg-capture/whatsapp/media` into the new root (idempotency/provenance keys are scoped to the new root, so replay is clean). Files previously retained flat under `/home/pclaw/.systems-pcl/data/media/` become harmless orphans — optional cleanup after convergence confirms.
