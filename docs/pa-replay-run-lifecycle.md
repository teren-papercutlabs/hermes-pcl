# PA replay run lifecycle

`hermes replay-run` is the PA replay orchestrator of record. It owns the run state around the native Hermes replay primitive and the systems-pcl replay target provider.

Boundary:

- Hermes `replay` executes normal gateway turns under `agent:replay:<run-id>`.
- `hermes replay-run` mints the run id, persists the run manifest, enforces the mechanical verify gate, and is the only Hermes-side path that calls provider promote/rollback.
- The target provider owns target preparation, run-id write guards, provider-specific invariants, promote, and rollback.

## State machine

```text
initialized
  -> preparing_target
  -> prepared
  -> running_agent_replay
  -> replayed
  -> verifying
  -> verified
  -> promoting
  -> promoted
  -> rolling_back
  -> rolled_back
```

Failure/abandon paths move to `dirty` or `failed`. Dirty runs are terminal and cannot promote. A promoted run may still roll back through the provider promotion manifest.

## Start a replay run

The start command performs `prepare_target -> run_agent_replay -> verify` and stops before promote.

```bash
export PS_REPLAY_PROVIDER_URL="http://127.0.0.1:5192"
export PS_REPLAY_PROVIDER_ADMIN_TOKEN="..."

hermes replay-run start \
  --provider-url "$PS_REPLAY_PROVIDER_URL" \
  --provider-admin-token "$PS_REPLAY_PROVIDER_ADMIN_TOKEN" \
  --source-data-dir /path/to/source-ps-data \
  --target-data-dir /path/to/fresh-eval-ps-data \
  --target-base-url http://127.0.0.1:5192 \
  --plan /path/to/replay-plan.json \
  --out-dir /path/to/replay-runs \
  --tool-error-budget 0
```

The command writes:

- `run-manifest.json` — state machine, target descriptor/baseline digests, attempt digests, gate result.
- `target-prepare.json` — provider prepare result without token literals.
- `replay-plan.json` — the exact `ReplayPlan` handed to Hermes.
- `attempt-<attempt-id>.json` — replay result + attempt provenance.
- `verify-report.json` — mechanical gate checks.

When a client-side scorer must inspect the replay target before verification,
stop safely in `replayed` state with `--defer-verify`. Promotion still refuses
the run until the normal verify command succeeds.

If the provider process must restart on the prepared target data root, use the
same lifecycle as explicit commands: `replay-run prepare`, restart the provider
with the returned target data dir, then `replay-run run --manifest ...` and
`replay-run verify --manifest ...`. These are slices of the same orchestrator
state machine, not a parallel runner.

## Model qualification and trust-rung graduation

`hermes replay-eval` is the shared measurement layer around native replay. Its
config schema is `hermes-replay-eval/v1`; client identity, corpus, isolated
tenant, model arms, probes, and score definitions are data. The execution path
remains `GatewayRunner.replay` + `PAReplayOrchestrator`.

The instrument fail-closes on four judgment-layer assertions:

1. tool-call sequence variance across cases,
2. configured paired probes taking different decision paths,
3. model output preceding tool results in every judgment turn, and
4. process provenance matching the pinned deployment artifact and the runtime
   config/constitution files actually loaded through `HERMES_HOME`.

Deterministic tools invoked by the model can be excluded from the judgment
layer in config. That preserves the intended boundary: mechanical work belongs
in code; a fixed pipeline masquerading as judgment does not.

Capture stores can be consumed natively without a projection. A corpus source
may set `record_path` (for example `normalized`) and `media_root`; ReplayCorpus
unwraps each immutable capture envelope, remaps missing media paths by basename,
then applies its normal ordering, reaction, and dedup policies.

```bash
# Raw client paths stay outside git and are injected through config env refs.
hermes replay-eval validate --config /path/to/eval-instrument.json

hermes replay-eval materialize \
  --config /path/to/eval-instrument.json \
  --arm candidate \
  --output-dir /isolated/run/candidate/hermes-home \
  --business-base-url http://127.0.0.1:5192/api/operator

hermes replay-eval plan \
  --config /path/to/eval-instrument.json \
  --arm candidate \
  --runtime-manifest /isolated/run/candidate/hermes-home/eval-runtime-manifest.json \
  --output /isolated/run/candidate/replay-plan.json
```

After replay and client-side scoring, bind both into one immutable receipt:

```bash
hermes replay-run verify \
  --manifest /isolated/run/candidate/replay-runs/<run-id>/run-manifest.json \
  --session-db /isolated/run/candidate/hermes-home/state.db \
  --eval-config /path/to/eval-instrument.json \
  --eval-arm candidate \
  --eval-mode eval \
  --eval-invocation-id <run-id> \
  --eval-receipt-index /isolated/run/eval-receipt-index.jsonl \
  --score-manifest /isolated/run/candidate/score.json
```

The same receipt gate runs with `--eval-mode graduation` at trust-rung
boundaries. Audit the operating-loop index, then compare distinct model arms:

```bash
hermes replay-eval audit \
  --index /isolated/run/eval-receipt-index.jsonl \
  --instrument-id <instrument-id> \
  --mode eval \
  --expect-invocation <run-id>

hermes replay-eval compare \
  --config /path/to/eval-instrument.json \
  --receipt /isolated/run/candidate/receipt.json \
  --receipt /isolated/run/baseline/receipt.json \
  --output-dir /isolated/run/comparison
```

The comparison surface never changes the deployed engine. It emits evidence
and an explicit `driver_verdict_required` decision state.

## Mechanical verify gate

Promote is refused unless all checks pass:

- corpus parity: processed message count equals the deterministic corpus count.
- processed-turn coverage: PA turn records exist and cover source message ids.
- zero failed PA turns.
- zero escaped outbound sends: captured replay outbounds
  (`delivery_mode=capture`) are reported for review but do not fail the gate;
  any non-capture outbound remains a hard-fail capture-lock leak.
- tool-error budget not exceeded.
- provider descriptor/baseline digests match their manifests.
- attempt/code/replay-policy digests match persisted manifests.
- provider invariants pass through `POST /api/operator/replay-target/verify`.

Expected business misses are not runtime failures. When a corpus legitimately
probes an absent entity, name the provider code explicitly with repeatable
`--allow-tool-error-code` (for example `CASE_NOT_FOUND`). Unnamed errors and
unknown operations still consume the strict error budget.

Re-run the gate:

```bash
hermes replay-run verify \
  --manifest /path/to/replay-runs/<run-id>/run-manifest.json \
  --session-db ~/.hermes/state.db \
  --tool-error-budget 0
```

A failed verify marks the target dirty locally and through the provider dirty endpoint. Dirty targets cannot promote; rebuild from a fresh baseline and rerun.

## Promote / rollback

Promotion must be called through the orchestrator manifest. Do not call the provider promote endpoint directly.

For this phase, use only non-prod target directories. Real TGG production promote is held for the gated validation phase.

```bash
hermes replay-run promote \
  --manifest /path/to/replay-runs/<run-id>/run-manifest.json \
  --prod-data-dir /path/to/non-prod-prod-dir \
  --confirm ORCHESTRATOR_PROMOTE
```

Rollback uses the provider promotion manifest recorded in `run-manifest.json`:

```bash
hermes replay-run rollback \
  --manifest /path/to/replay-runs/<run-id>/run-manifest.json
```

If the provider manifest path must be supplied manually:

```bash
hermes replay-run rollback \
  --manifest /path/to/replay-runs/<run-id>/run-manifest.json \
  --promotion-manifest-path /path/to/provider/promotions/<promotion>.json
```

## Recovery commands

Inspect current state:

```bash
hermes replay-run status --manifest /path/to/replay-runs/<run-id>/run-manifest.json
```

Mark an abandoned or suspect run dirty:

```bash
hermes replay-run dirty \
  --manifest /path/to/replay-runs/<run-id>/run-manifest.json \
  --reason "abandoned after partial replay"
```

Fresh-baseline-only rule: never promote an in-place resumed target. If a run dies after target preparation or replay starts, mark it dirty, prepare a new target data dir, and rerun from the original corpus/plan.
