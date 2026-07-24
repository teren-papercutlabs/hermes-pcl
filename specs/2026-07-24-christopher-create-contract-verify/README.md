# Christopher gate verification battery

Run against a detached checkout of `origin/integration/b6240e2d-main`:

```sh
P=/path/to/hermes-pcl-integration
VENV=/path/to/hermes-pcl/.venv/bin/python
OUT=specs/2026-07-24-christopher-gate-verify/evidence

PYTHONPATH="$P" "$VENV" run_gate_fixture.py \
  --app-root "$P" --secrets-env ~/.marshal/secrets.env \
  --test-root /tmp/christopher-gate \
  --fixture-file fixtures/core.jsonl --seed-file fixtures/seeds.json \
  --repeat 2 --report "$OUT/core-repeat.json"

PYTHONPATH="$P" "$VENV" run_gate_fixture.py \
  --app-root "$P" --secrets-env ~/.marshal/secrets.env \
  --test-root /tmp/christopher-gate \
  --fixture-file fixtures/label-turn-1.jsonl \
  --fixture-file fixtures/label-turn-2.jsonl \
  --seed-file fixtures/seeds.json --report "$OUT/label-drift.json"

PYTHONPATH="$P" "$VENV" run_gate_fixture.py \
  --app-root "$P" --secrets-env ~/.marshal/secrets.env \
  --test-root /tmp/christopher-gate \
  --fixture-file fixtures/replay-observation.jsonl \
  --seed-file fixtures/seeds.json --repeat 2 \
  --report "$OUT/replay-observation.json"

PYTHONPATH="$P" "$VENV" run_gate_fixture.py \
  --app-root "$P" --secrets-env ~/.marshal/secrets.env \
  --test-root /tmp/christopher-gate \
  --fixture-file fixtures/concurrency.jsonl \
  --seed-file fixtures/seeds.json --mode concurrency \
  --site-concurrency 3 --report "$OUT/concurrency.json"

python3 compare_reports.py
```

All fixtures are synthetic. HTTP writes terminate at a loopback stub. Replay
delivery is capture-only; the copied config disables WhatsApp. The concurrency
text-only run also disables media retention because the integration config's
production media mount does not exist in the sandbox.
