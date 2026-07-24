# Christopher create-contract verification battery

This is the verifier battery adapted to the fixed worker tree. Run from this
repository root; all fixture data is synthetic, HTTP writes terminate at a
loopback stateful stub, replay delivery is capture-only, and WhatsApp is
disabled.

```sh
SPEC=specs/2026-07-24-christopher-create-contract-verify
PYTHONPATH="$PWD" .venv/bin/python "$SPEC/run_gate_fixture.py" \
  --app-root "$PWD" --secrets-env ~/.marshal/secrets.env \
  --test-root /tmp/christopher-create-contract \
  --fixture-file "$SPEC/fixtures/core.jsonl" \
  --seed-file "$SPEC/fixtures/seeds.json" --repeat 2 \
  --report "$SPEC/evidence/core-repeat.json"

PYTHONPATH="$PWD" .venv/bin/python "$SPEC/run_gate_fixture.py" \
  --app-root "$PWD" --secrets-env ~/.marshal/secrets.env \
  --test-root /tmp/christopher-create-contract \
  --fixture-file "$SPEC/fixtures/label-turn-1.jsonl" \
  --fixture-file "$SPEC/fixtures/label-turn-2.jsonl" \
  --seed-file "$SPEC/fixtures/seeds.json" \
  --report "$SPEC/evidence/label-drift.json"

PYTHONPATH="$PWD" .venv/bin/python "$SPEC/run_gate_fixture.py" \
  --app-root "$PWD" --secrets-env ~/.marshal/secrets.env \
  --test-root /tmp/christopher-create-contract \
  --fixture-file "$SPEC/fixtures/replay-observation.jsonl" \
  --seed-file "$SPEC/fixtures/seeds.json" --repeat 2 \
  --report "$SPEC/evidence/replay-observation.json"

PYTHONPATH="$PWD" .venv/bin/python "$SPEC/run_gate_fixture.py" \
  --app-root "$PWD" --secrets-env ~/.marshal/secrets.env \
  --test-root /tmp/christopher-create-contract \
  --fixture-file "$SPEC/fixtures/concurrency.jsonl" \
  --seed-file "$SPEC/fixtures/seeds.json" --mode concurrency \
  --site-concurrency 3 --report "$SPEC/evidence/concurrency.json"

python3 "$SPEC/compare_reports.py"
```

The concurrency text-only run disables media retention because the production
media mount is intentionally absent from the isolated test environment.
