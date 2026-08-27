# MTU BOR rebuild plan

Status: source-only implementation; no deployment authorized.

## Outcome

Rebuild the BOR capability as a deterministic assembler. The model extracts facts from supplied screenshots and resolves genuine ambiguity. Code and structured client configuration own required inputs, defaults, policy-to-policy mappings, disclosure selection, exact clauses, clause order, and output validation.

The existing MTU deployment tree is the starting artifact. This is an adaptation of the existing case record, compliance blocks, reference tables, output assembly, and eval harness—not a parallel agent.

## Locked input contract

One case consists of:

1. eKYC page 6: existing-policy rows and the `To Be Replaced` flag for each row.
2. eKYC pages 9 and 10: the client's recorded needs and shortfalls.
3. An adviser-supplied policy category for every relevant page-6 policy because page 6 does not state the category.
4. One PIPS/BI summary screenshot for each proposed product.

Advisers may send several screenshots in one burst. The runtime groups them into one case after ten seconds of quiet and generates directly; it does not add a confirmation turn.

For every ILP, the selected fund defaults to `Fundsmith Equity`. The adviser is not asked for fund allocation, fund details, or a separate fund screenshot.

## Canonical case shape

The present configuration stores `existing_plans` and `proposed_plan` as scalar text. That cannot represent page-6 rows, multiple recommendations, or exact replacement links without asking the model to reconstruct structure later. Replace the scalar-only client contract with structured per-policy records while retaining a compatibility projection for the shared runtime during migration.

```yaml
case:
  needs: []
  existing_policies:
    - source_row: page_6_row_1
      insurer: null
      product_name: null
      adviser_category: whole_life
      benefits: []
      premium: null
      to_be_replaced: true
  proposed_policies:
    - source_document: pips_bi_1
      insurer: null
      product_name: null
      product_category: investment_linked
      benefits: []
      premium: null
      minimum_investment_period: null
      fund: Fundsmith Equity
  replacement_links:
    - existing_policy_refs: [page_6_row_1]
      proposed_policy_refs: [pips_bi_1]
```

Policy category is adviser authority for existing policies. Product documents remain authority for proposed-product facts. Page 6 remains authority for whether an existing policy is marked for replacement. The assembler must never infer a Shield-to-life or whole-life-to-Shield replacement link merely because both appear in the same case.

## Deterministic/model boundary

Deterministic:

- required page set and attachment grouping;
- page-6 row identity and replacement flag;
- adviser category capture per existing policy;
- proposed-product record per PIPS/BI summary;
- ILP Fundsmith Equity default;
- replacement-link validation;
- disclosure applicability, exact approved text, ordering, and deduplication;
- completed-output structure and golden comparisons.

Model-owned:

- OCR and extraction into the declared record;
- recognising the need described on pages 9 and 10;
- concise variable narrative inside explicitly generative slots;
- one targeted clarification only when a required fact cannot be extracted or linked safely.

## Source findings that drive the rebuild

- `case-field-sets.yaml` currently makes `fund_selection` required and askable. This directly conflicts with the new Fundsmith Equity default.
- The same file stores plans as scalar text and derives replacement status from prose. Page 6 now provides a direct per-row flag, so replacement must enter as structured source data instead.
- `reference/066-funds.yaml` recognises Fundsmith aliases but marks its allocation and risk classification unverified. The default fund name can be locked now; any risk-alignment sentence that needs unverified allocation remains provisional.
- The MTU turn profile currently waits four seconds for passive input. The corrected multi-screenshot workflow requires ten seconds of quiet.
- `knowledge/standard-disclosures.md` explicitly says its wording is a starter library pending Melody. Recent approved BORs will replace or ratify those clauses; they do not block the input and assembly work.

## Build slices

### 1. Input and record contract

- Add structured existing-policy, proposed-policy, need, and replacement-link records.
- Add source/provenance markers for page 6, pages 9–10, adviser category, and PIPS/BI.
- Preserve a compatibility view for current case-record consumers while assembly migrates.
- Add schema and fixture tests using synthetic/redacted data only.

### 2. Intake behaviour

- Set the MTU screenshot quiet window to ten seconds.
- Require pages 6, 9, and 10 plus at least one PIPS/BI summary.
- Require one adviser category per relevant page-6 policy.
- Generate directly when the record is sufficient; do not ask for confirmation.
- Default every ILP proposed-policy record to Fundsmith Equity.

### 3. Deterministic assembly

- Assemble one narrative block per proposed product/need bucket.
- Select exact disclosure blocks once, deduplicate them, and validate order.
- Keep any clause whose wording or applicability depends on Melody's recent BORs behind an explicit provisional gate.

### 4. Full-output proof

Add synthetic full-output golden cases for:

- first-purchase ILP;
- replacement of a cash-value plan by an ILP;
- multi-plan replacement with explicit links;
- Shield plus unrelated life recommendation, proving no cross-category replacement inference;
- missing adviser category, proving one compact clarification;
- ILP input with no fund details, proving Fundsmith Equity is inserted and no fund question is asked;
- repeated/overlapping triggers, proving no duplicate disclaimers.

Run the existing MTU case-record, output-assembly, replay, and full-output tests alongside the new battery.

## Deferred source enrichment

Melody will provide recent approved BORs containing the real input combinations and disclosure choices. When received, they become the wording and golden-output authority. They may change exact clauses and conditional applicability, but they do not reopen the locked page contract, structured policy mapping, Fundsmith default, or direct-generation flow unless Melody explicitly changes those requirements.

## Rollback and activation

This branch changes source and tests only. The live MTU runtime is untouched. Rollback is discarding or reverting the branch. Deployment, production replay, and client testing require a separate current-state check and explicit activation decision after the source is reviewed and Melody's recent cases are incorporated.
