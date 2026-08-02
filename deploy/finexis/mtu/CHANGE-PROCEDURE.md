# MTU correction-to-test procedure

This is the operating procedure for `edna-mtu`. A correction is not done when
text changes. It is done when the corrected behavior is represented in the eval
corpus and the affected-tag replay subset passes.

## Procedure

1. Capture the accepted correction in `rulings/<id>.yaml` using
   `rulings/README.md`. Preserve the exact words.
2. Classify the knowledge type (A–F) and name the affected source artifacts and
   eval tags in the ruling's `scope`.
3. Add or update a case in the PA eval corpus before treating the source edit as
   complete. The case's provenance points to the ruling id. Multi-turn defects
   remain multi-turn cases.
4. Land the source change and put the ruling id in every affected typed source's
   `ruling_ref` header.
5. Compose the non-live runtime copy. Never run this gate against or write to
   `~/.hermes-mtu`. Evaluate the deploy-tree candidate while borrowing only
   secrets from the installed runtime:

   ```bash
   .venv/bin/python deploy/finexis/mtu/scripts/run_eval_corpus.py \
     --corpus deploy/finexis/mtu/evals/mtu-eval-corpus-v1.json \
     --report /tmp/mtu-candidate-report.json \
     --runtime-source ~/.hermes-mtu \
     --candidate-deploy-dir deploy/finexis/mtu \
     --baseline-report deploy/finexis/mtu/evidence/mtu-eval-replay-2026-08-02.json
   ```
6. Run the corpus adapter for the union of the ruling's affected tags. The
   adapter executes each case turn through native Hermes replay under one shared
   replay namespace, records the response to every turn, and checks
   `exact_present`/`exact_absent` assertions after whitespace normalization.
   `must`/`must_not` records stay visibly `pending_judge` until the judge layer
   scores them.
7. A correction reaches DONE only when:
   - its ruling record exists;
   - its eval case exists;
   - every deterministic assertion in the affected-tag subset passes;
   - the judge layer passes every `must`/`must_not` expectation; and
   - the generated report records the corpus digest, runtime-copy identity,
     case count, turn count, and per-turn results.

If any condition is red or pending, the correction remains open. Do not deploy
around the gate and do not represent a source edit as corrected behavior.

## Deploy gate table

`scripts/deploy_guarded.py` is the only MTU deploy entry. The underlying
`bootstrap_local.sh` refuses direct use without a receipt produced by that
command. The gate classes are source-enforced from `eval-policy.yaml`:

- rule, compliance, wording, template, or job-brief edit: infer the affected
  tags from every changed file, then require that union plus the smoke tags;
- reference-data edit: affected tags plus smoke, semantic judge, and structured
  reference validation;
- document upload: document registration validation;
- model or provider swap: the full corpus, at least four draws per case, all
  semantic judgments passed, and canaries clean;
- channel change or DEBUT: the full corpus through a report explicitly produced
  by the live-channel battery, with semantic judgments and canaries clean.

Every class also re-reads the current nightly verdict. Red blocks deployment.
Only a JSON waiver preserving Teren's or Amelia's exact recorded word, its
timestamp, and `waives: [nightly_red]` can bypass that one red-state predicate;
it does not bypass the change-class battery.

For a model/provider battery, add `--honor-draws --minimum-draws 4` to the
runner command. This raises one-draw cases to the required floor rather than
silently treating their corpus default as sufficient.

The report is bound to the current corpus, candidate `config.yaml`, and
candidate `mtu_constitution.yaml` by digest. A report generated from the
installed runtime cannot authorize a different deploy-tree candidate.

## Judge and deterministic split

`exact_present` and `exact_absent` stay runner-scored after whitespace
normalization. `must` and `must_not` alone go to
`scripts/judge_eval_report.py`, pinned by policy to `gpt-5.6-sol`, medium
reasoning, and the schema in `evals/mtu-judge.schema.json`. The judge must copy
each label and kind exactly; omission, reordering, or identity drift refuses the
result. Until Amelia completes calibration batch 1, semantic status is
`calibration_pending`, so a behavior-changing deploy cannot pass by pretending
the deterministic checks cover judgment.

Calibration batch 1 is generated with:

```bash
.venv/bin/python deploy/finexis/mtu/scripts/judge_eval_report.py \
  --report <replay-report.json> --output <scored-report.json> \
  --limit-turns 10 \
  --calibration-output deploy/finexis/mtu/evidence/mtu-judge-calibration-batch-1.json
```

The packet is handed to Amelia through `edna-mtu`; this worker does not contact
her directly.

## Nightly regression

The 03:15 SGT launchd definition is
`launchd/com.pcl.mtu-eval-nightly.plist`. It runs `scripts/run_nightly.py`
against a disposable copy of `~/.hermes-mtu`; the live home is read-only. The
runner compares exact failures against the accepted S4 baseline by assertion
identity. Green means all 43 cases ran and there are zero new deterministic
failures; it does not relabel the ten accepted failures as passes. The durable
state is `~/.marshal/pa-eval/mtu/latest.json`, and every run posts a one-line
summary to WB `97f2c123` for `edna-mtu`. The deploy gate consumes the state file,
not the post acknowledgement.

## Non-live run boundary

The evaluation home must be a disposable copy outside `~/.hermes-mtu`. Copy the
runtime inputs, rewrite `pa.constitution_path` to the copied constitution, and
set `HERMES_HOME` to that directory before starting Hermes. The committed report
must identify that copy without including credentials. Delete the disposable
home after the report is written.

## Rollback

Revert the source, ruling, eval-case, and report commits together. This procedure
does not deploy or mutate the live MTU runtime; data loss is none.
