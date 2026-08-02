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
   `~/.hermes-mtu`.
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

## Non-live run boundary

The evaluation home must be a disposable copy outside `~/.hermes-mtu`. Copy the
runtime inputs, rewrite `pa.constitution_path` to the copied constitution, and
set `HERMES_HOME` to that directory before starting Hermes. The committed report
must identify that copy without including credentials. Delete the disposable
home after the report is written.

## Rollback

Revert the source, ruling, eval-case, and report commits together. This procedure
does not deploy or mutate the live MTU runtime; data loss is none.
