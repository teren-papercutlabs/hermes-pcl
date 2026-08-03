# MTU P0 — BOR Draft Template (recommendation-rationale narrative)

This is the concise narrative structure used per recommended product / need-bucket. It is grounded in files 01 and 17 and confirmed across the ten text-readable OneDrive cases. Variable case facts are never invented. Corpus-standard alternatives wording is selected by product category rather than collected from the advisor each time.

## Interaction before drafting

- Use every fact already supplied. Do not repeat it back as an intake recap.
- Derive coverage and premium movement from the category-appropriate facts provided. For sustainability, use only the advisor's yes/no answer to whether total annual premiums exceed 50% of annual income; never request or print income or surplus figures.
- Insert the category-specific alternatives sentence below. Do not ask which alternatives were considered unless the path is novel, the advisor names an exception, or the standard wording conflicts with the case.
- Match intake and comparison fields to the product category. For Shield/hospitalisation, collect exact base-plan and exact/versioned rider names, material premium, ROP mapping/status, and the client's rationale. Never ask for or print rider benefit limits, deductible, or coinsurance. If a rider is missing or ambiguous, ask only for its exact/versioned name.
- If a known category has no approved required-field set in bor-required-checks.yaml—including CareShield/ElderShield, accident, or other A&H—do not draft from generic assumptions. Withhold that component and route it to Melody.
- Do not ask about comparison-list freshness or reference numbers by default. Reference numbers are outside the BOR narrative unless the advisor explicitly requests one or names a workflow that requires one.
- If irreducible facts are missing, ask for all of them once in one compact message instead of drafting. Irreducible facts are: category-appropriate material plan facts needed for a truthful comparison; the client's stated rationale; old→new mapping and ROP status; and ILP-only suitability facts when an ILP is involved. ROP acknowledgement wording is a template default, not an intake question. Do not output a partial BOR, placeholders, or an unresolved list in that turn.
- If the missing fact is non-blocking, draft with a clear [[MISSING: ...]] placeholder instead of conducting another question round.

## Category-specific standard alternatives sentences

Protection path — recommended Term, Whole Life, or protection ILP:

> After discussing the pros and cons of whole life, term life and investment-linked plans, the client preferred {{RECOMMENDED_CATEGORY}} because {{CLIENT_STATED_RATIONALE_OR_SUPPLIED_PRODUCT_FIT}}.

ILP / wealth path — recommended ILP, investment platform, endowment, annuity, or IUL:

> After discussing the pros and cons of endowment, investment and annuity solutions, the client preferred {{RECOMMENDED_CATEGORY}} because {{CLIENT_STATED_RATIONALE_OR_SUPPLIED_PRODUCT_FIT}}.

Shield path — recommended Integrated Shield Plan / medical plan:

> After comparing Shield plans from other insurers, the client preferred {{RECOMMENDED_PLAN}} because {{CLIENT_STATED_RATIONALE_OR_SUPPLIED_PRODUCT_FIT}}.

The reason slot may use only the client's stated rationale or an objective product fit directly supported by supplied figures/features. Do not invent alternatives outside the approved template, a product preference not supplied by the advisor, or a rationale. For an unknown category, ask one short clarification instead of forcing a template.

## Narrative order per recommended product

1. Need and existing position

> To address the client's {{NEED}}, the client currently has {{EXISTING_PLANS_OR_NO_EXISTING_COVER}}.

2. Client's reason

> After a recent review, the client stated {{CLIENT_STATED_RATIONALE}}.

3. Standard alternatives sentence

Insert exactly one category-specific sentence from the section above.

4. Recommended product and material facts

Protection / life path:

> The client has chosen {{PROPOSED_PLAN_NAME_AND_RIDERS}}. The plan provides {{SA_BY_BENEFIT}}, costs {{PREMIUM_FIRST_YEAR_AND_SUBSEQUENT}}, and covers the client {{POLICY_TERM_OR_TO_AGE}}. {{ILP_ONLY: Minimum Investment Period {{MIP}}.}}

Shield / hospitalisation path:

> The client has chosen {{EXACT_PROPOSED_BASE_PLAN_NAME}} with {{EXACT_VERSIONED_RIDER_NAME}}, at a material premium of {{MATERIAL_PREMIUM}}.

For Shield, the plan and rider names identify the arrangement, not numeric benefits. Do not ask for or print rider benefit limits, deductible, or coinsurance. If an advisor volunteers an exceptional or nonstandard fact material to the rationale, preserve it accurately in case context without printing the prohibited value in the BOR.

Do not claim the plan matches affordability unless the supplied figures support that conclusion.

5. Derived comparison

> {{COVERAGE_DELTA_STATEMENT}} {{PREMIUM_MOVEMENT_STATEMENT}} {{SUSTAINABILITY_STATEMENT_FROM_50_PERCENT_BOOLEAN}}

For protection/life, state supplied before/after sums assured and durations directly. For Shield/hospitalisation, state a qualitative before/after arrangement using the exact existing and proposed plan/rider names plus the supplied rationale; do not infer numeric benefits. If the premium is higher, use only supplied product differences or the client's stated reason as justification. Copy the exact sentence for the supplied boolean: NO → "The client's total annual premiums do not exceed 50% of annual income." YES → "The client's total annual premiums exceed 50% of annual income, and the client was advised to consider the sustainability of the premium commitment." Never invert, recalculate, infer, or soften this mapping. Never request or expose the client's income or surplus.

6. Conditional blocks

- ROP only: insert "The client was advised of the replacement disadvantages and wishes to proceed." as standard template text without asking the advisor to reconfirm it. If the advisor explicitly contradicts this or expresses uncertainty, suppress it and return only a concise Melody-escalation note—no holding draft, partial BOR, disclosures, or placeholders. The replacement-options declaration is also standard template text; insert it without asking.
- Non-ROP: exclude every ROP acknowledgement and replacement disclosure.
- ILP only: include CKA, risk profile, fund/objective alignment, and the ILP disclosure block. Fill product-specific disclosure values from supplied product documents only.
- Non-ILP: exclude every ILP-specific question and disclosure.

7. General disclosures

Insert the approved general disclosure block from standard-disclosures.md without paraphrasing it.

## Output contract

- Return the BOR draft directly. Keep it copy-pasteable, short, and in plain text.
- Add a short "Check before use:" note only for unresolved placeholders or a Melody escalation. Do not list every field used.
- Do not add a reference-number placeholder unless the advisor requested it or the named workflow requires it.
- Do not end with an offer to shorten, reformat, or produce another version.
- Do not emit Markdown asterisks anywhere in the Telegram response. Use plain headings and hyphens only when needed.
- Before sending, remove every Markdown marker (`*`, `#`, `---`), intake recap, and closing offer such as "if you want". If any remain, rewrite once in concise plain text.

## Hard rules

- Do not invent SA, premiums, reference numbers, client rationale, CKA/risk profile, fund alignment, or product-specific disclosure values. The approved ROP acknowledgement and alternatives declarations are template defaults; suppress them on explicit contradiction or uncertainty.
- Do not make the product recommendation. The advisor chooses the product; the agent drafts the justification.
- The draft is for advisor review, not compliance sign-off. Escalate sensitive or unclear cases to Melody.
