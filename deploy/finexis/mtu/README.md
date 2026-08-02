# hermes-mtu — Finexis / Melody Tan Unit (MTU) BOR assistant — Telegram pilot

Runtime source-of-truth for the **MTU BOR-generation** PA agent (Melody Tan Unit, Finexis financial advisory), deployed as a **Studio-local hermes pilot on Telegram**. Second Finexis PA deployment; reuses the `amelia-finexis` shared standard. Patterned on `deploy/tgg/christopher/`.

**Status (2026-07-13):** **DEPLOYED + synthetic-E2E tested** on Telegram bot **@pcl_mtu_bor_bot** (READY, not DEBUT). Bot-cap blocker resolved (deleted one retired throwaway, minted the MTU bot). Gateway running + telegram connected; ROP and non-ROP synthetic cases pass through the production stack. See `test-transcripts/2026-07-13-synthetic-e2e.md` + OPS-NOTE. Remaining before a real advisor: Amelia's DEBUT go + Melody's BOR-table confirm + persistence (launchd/VPS).

## What this agent does
Advisor DMs a rough case (existing plan, proposed plan, is-it-a-replacement) → the agent classifies the replacement path, runs the required BOR checks, asks for anything missing, then **drafts a copy-pasteable BOR (Basis of Recommendation)** from the template with the right compliance disclosures inserted. It DRAFTS for advisor review — never advises or signs off compliance. See `mtu_constitution.yaml` + `knowledge/`.

## Files (source-of-truth; deployed to `$HERMES_HOME`)
| File | Role |
|---|---|
| `rules/`, `compliance/`, `reference/`, `templates/`, `job-briefs/` | Typed constitution sources. These are the editable source of truth; each artifact starts with a `pa-source` provenance header. |
| `mtu_constitution.yaml` | Generated PA constitution. Do not hand-edit. `hermes pa compose` reproduces it from the typed sources before deploy. |
| `mtu_constitution.manifest.json` | Generated digest manifest binding every source artifact and recording whether the unverified-compliance escape was used. |
| `config.yaml` | Gateway config: model (openai-direct-primary/gpt-5.4-mini) + `pa.enabled/job_type/constitution_path` + `platforms.telegram`. |
| `SOUL.md` | Thin operating rule only. Reference tables are deliberately absent from the prompt. |
| `knowledge/`, `reference/` | Manifest-declared prose and exact-key structured references. `compose: false` reference artifacts are validated and digested but excluded from constitution prose. |
| `scripts/bootstrap_local.sh` | Builds `~/.hermes-mtu` (HERMES_HOME) from this dir + writes `.env` (token + allowlist + OPENAI key sourced from secrets). Idempotent. Secrets never committed. |
| `OPS-NOTE.md` | Deploy specifics + what remains + rollback. |

## Typed constitution compose

Each YAML artifact under the five typed directories begins with this comment header:

```yaml
# pa-source:
# approved_by: [amelia]
# approved_date: ['2026-07-23']
# ruling_ref: [R02]
# status: approved
# sequence: 10
# ---
```

`approved_by`, `approved_date`, `ruling_ref`, and `status` are mandatory. `status` is
`approved`, `pending`, or `unverified`. `sequence` is a unique non-negative integer and
defines deterministic assembly order across directories. The correction-record ruling IDs
are the provenance vocabulary; an approval that cannot be traced stays `unverified` rather
than being promoted by implication.

Compose is a deploy-time build step, not runtime YAML merging:

```bash
hermes pa compose \
  --source-dir deploy/finexis/mtu \
  --output deploy/finexis/mtu/mtu_constitution.yaml \
  --manifest deploy/finexis/mtu/mtu_constitution.manifest.json
```

The default command refuses when any `compliance/` artifact is `unverified` and writes
neither output. During this parity-only migration, the explicit escape is required:

```bash
hermes pa compose \
  --source-dir deploy/finexis/mtu \
  --output deploy/finexis/mtu/mtu_constitution.yaml \
  --manifest deploy/finexis/mtu/mtu_constitution.manifest.json \
  --allow-unverified
```

The manifest then records `allow_unverified: true` and enumerates the affected compliance
artifacts. Resolve provenance and remove the escape before a later deploy step. This command
only writes the two caller-selected output paths; it does not deploy, restart Hermes, or write
`~/.hermes-mtu`.

`hermes pa sync-knowledge` reads the composed job brief's plain `knowledge` list and copies
only those declared files beneath the configured knowledge root. Runtime tools refuse any
undeclared path. `pa_reference_lookup` performs exact key lookup and returns either the
declared row or `found=false` with the file's escalation cue; it never fuzzy-matches.

## Run (once a free bot token is available)
```bash
# 1. token -> ~/.hermes-mtu-token.tmp (chmod 600), then:
deploy/finexis/mtu/scripts/bootstrap_local.sh ~/.hermes-mtu-token.tmp "<advisor_tg_user_id>"
# 2. run (needs network egress):
HERMES_HOME=~/.hermes-mtu ~/pcl-dev/hermes-pcl/.venv/bin/python ~/pcl-dev/hermes-pcl/hermes gateway run
```
Then DM the bot from an allowlisted Telegram account. Full research/recipe: `~/pcl-biz/_agents/edna/specs/2026-07-05-fa-mtu-assistant/deploy-research.md`.

## Safety
- No client PII here. The constitution + knowledge are abstracted (checklist/template/disclosures) — no real case data. Melody's real PDFs stay out of any repo (`client-raw/`, gitignored).
- Secrets (`.env`) live only in `~/.hermes-mtu/`, never committed.
- READY, not DEBUT: first real advisor use is Amelia's gate (WB 16f2b3ae) + Melody's BOR-table confirm.
