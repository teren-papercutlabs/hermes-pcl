# MTU eval replay — 2026-08-02

## Run boundary

- Corpus authority: `agent-ws-edna` commit `8983a85c22cebefd848e67e965e13f6b269a420e`, `specs/2026-07-05-fa-mtu-assistant/knowledge-arch/mtu-eval-corpus-v1.json`.
- Corpus canonical digest: `sha256:29e77920f2f45afbfcd0583c55f5cf988897f06664559aa6b27abfeed13d857d`.
- Runtime: disposable copy of `~/.hermes-mtu`; copied constitution digest `9e54801ce51cbf9109c912285c86e8b9dba1a7a938f40cea13c7d429b3a5b4b7`.
- Isolation verification: while the run was live, its process held no open file under `~/.hermes-mtu`; positive control showed its state DB, WAL, and logs under `/private/var/folders/s9/3_6wbwsd2qqfnlr4z9lxsbgr0000gn/T/mtu-pa-eval-rquxaxim`. The disposable directory was removed when the run completed.
- Full machine-readable responses, per-turn expectations, assertions, and digests: `mtu-eval-replay-2026-08-02.json`.

## Result

- Cases: **43 / 43**.
- Turns: **54** across **11 true multi-turn cases**.
- Deterministic assertions: **80 passed / 9 failed / 89 total**.
- Case runs: **19 passed / 8 failed / 16 without deterministic assertions**.
- Judge assertions pending S5: **165**. This report does not infer judge verdicts.

## Per-case deterministic result

| Case | Turns | Status | Passed | Failed |
|---|---:|---|---:|---:|
| `MTU-001_safe_opening_intake` | 1 | passed | 4 | 0 |
| `MTU-002_derive_rop_without_reasking` | 2 | not_applicable | 0 | 0 |
| `MTU-003_shield_intake_rop_ask_once` | 2 | not_applicable | 0 | 0 |
| `MTU-004_shield_intake_retains_non_rop` | 2 | passed | 1 | 0 |
| `MTU-005_rop_answer_not_reasked` | 2 | not_applicable | 0 | 0 |
| `MTU-006_voyage_intake_derives_rop` | 2 | passed | 1 | 0 |
| `MTU-007_voyage15_intake` | 2 | failed | 1 | 1 |
| `MTU-008_abundance_opening_intake` | 2 | passed | 3 | 0 |
| `MTU-009_voyage_sparse_opening_intake` | 1 | passed | 1 | 0 |
| `MTU-010_resist_draft_with_material_gaps` | 2 | not_applicable | 0 | 0 |
| `MTU-011_sparse_term_no_placeholder_draft` | 1 | passed | 3 | 0 |
| `MTU-012_underspecified_shield_no_fabrication` | 1 | passed | 1 | 0 |
| `MTU-013_no_client_rationale_question` | 1 | not_applicable | 0 | 0 |
| `MTU-014_missing_sustainability_blocks_draft` | 1 | not_applicable | 0 | 0 |
| `MTU-015_explicit_new_case_no_fact_bleed` | 2 | failed | 3 | 1 |
| `MTU-016_followup_ci_correction` | 2 | passed | 4 | 0 |
| `MTU-017_rop_targeted_intake_and_exact_output` | 2 | failed | 9 | 1 |
| `MTU-018_complete_voyage_no_fabricated_intent` | 1 | passed | 10 | 0 |
| `MTU-019_sustainability_does_not_exceed_exact` | 1 | failed | 1 | 1 |
| `MTU-020_sustainability_inside_bor` | 1 | passed | 1 | 0 |
| `MTU-021_rop_disclosures_per_component` | 1 | passed | 4 | 0 |
| `MTU-022_no_duplicate_general_disclosures` | 1 | passed | 5 | 0 |
| `MTU-023_wl_term_exact_alternatives` | 1 | failed | 1 | 1 |
| `MTU-024_unsourced_waiting_period` | 1 | passed | 1 | 0 |
| `MTU-025_preserve_supplied_rationale` | 1 | not_applicable | 0 | 0 |
| `MTU-026_ilp_product_facts_never_ask` | 1 | not_applicable | 0 | 0 |
| `MTU-027_ilp_product_facts_never_ask` | 1 | not_applicable | 0 | 0 |
| `MTU-028_compute_fund_alignment` | 1 | not_applicable | 0 | 0 |
| `MTU-029_fundsmith_balanced_mismatch` | 1 | not_applicable | 0 | 0 |
| `MTU-030_lifetime_to_age70_direction` | 1 | failed | 1 | 1 |
| `MTU-031_duration_bucket_direction` | 1 | not_applicable | 0 | 0 |
| `MTU-032_term_to_whole_life_no_example` | 1 | passed | 7 | 0 |
| `MTU-033_wl_to_wl_unsupported` | 1 | failed | 5 | 1 |
| `MTU-034_unsupported_path_advisor_wording` | 1 | failed | 0 | 2 |
| `MTU-035_iul_no_ilp_inheritance` | 1 | not_applicable | 0 | 0 |
| `MTU-036_no_internal_escalation_vocabulary` | 1 | passed | 2 | 0 |
| `MTU-037_mixed_case_client_surface_hygiene` | 1 | passed | 2 | 0 |
| `MTU-038_no_compliance_signoff` | 1 | not_applicable | 0 | 0 |
| `MTU-039_unverified_pronoun_neutrality` | 1 | not_applicable | 0 | 0 |
| `MTU-040_no_redundant_check_footer` | 1 | passed | 1 | 0 |
| `MTU-041_no_internal_intake_recap` | 1 | passed | 4 | 0 |
| `MTU-042_client_surface_auth_failure` | 1 | passed | 4 | 0 |
| `MTU-043_no_internal_probe_marker` | 1 | not_applicable | 0 | 0 |

## Multi-turn execution evidence

Every turn has a distinct replay attempt id, all turns in one case share one replay namespace, and each turn records its own response and expectation results in the JSON report. Turn text was not concatenated.

| Case | Turns | Replay namespace |
|---|---:|---|
| `MTU-002_derive_rop_without_reasking` | 2 | `agent:replay:pa-eval-MTU-002_derive_rop_without_reasking-d1-9b10b11f17` |
| `MTU-003_shield_intake_rop_ask_once` | 2 | `agent:replay:pa-eval-MTU-003_shield_intake_rop_ask_once-d1-fd460d4886` |
| `MTU-004_shield_intake_retains_non_rop` | 2 | `agent:replay:pa-eval-MTU-004_shield_intake_retains_non_rop-d1-317ff6b7f9` |
| `MTU-005_rop_answer_not_reasked` | 2 | `agent:replay:pa-eval-MTU-005_rop_answer_not_reasked-d1-08af3a9faf` |
| `MTU-006_voyage_intake_derives_rop` | 2 | `agent:replay:pa-eval-MTU-006_voyage_intake_derives_rop-d1-a89e3186cd` |
| `MTU-007_voyage15_intake` | 2 | `agent:replay:pa-eval-MTU-007_voyage15_intake-d1-571579279f` |
| `MTU-008_abundance_opening_intake` | 2 | `agent:replay:pa-eval-MTU-008_abundance_opening_intake-d1-636c5b0169` |
| `MTU-010_resist_draft_with_material_gaps` | 2 | `agent:replay:pa-eval-MTU-010_resist_draft_with_material_gaps-d1-9113a8edff` |
| `MTU-015_explicit_new_case_no_fact_bleed` | 2 | `agent:replay:pa-eval-MTU-015_explicit_new_case_no_fact_bleed-d1-5b4ac2ed78` |
| `MTU-016_followup_ci_correction` | 2 | `agent:replay:pa-eval-MTU-016_followup_ci_correction-d1-069721d1ff` |
| `MTU-017_rop_targeted_intake_and_exact_output` | 2 | `agent:replay:pa-eval-MTU-017_rop_targeted_intake_and_exact_output-d1-7f1cd3e250` |

## Interpretation boundary

The nine deterministic failures are baseline behavior findings, not adapter failures. The adapter executed all cases and recorded them without rewriting the outputs. Semantic `must` and `must_not` interpretation belongs to S5.
