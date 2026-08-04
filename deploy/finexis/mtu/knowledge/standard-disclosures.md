# MTU P0 — Standard Disclosures Library (boilerplate)

The compliance boilerplate that recurs near-verbatim across the real BOR cases (files 01, 17; confirmed by the bor-scraper's 70%+-frequency boilerplate detection). These are **generic regulatory/compliance statements** (not client-specific) that the agent **inserts** into the draft rather than writing fresh per case. This keeps drafts consistent and compliant, and confines the agent's generative work to the variable, client-specific narrative.

> These are transcribed from the structure of the real cases as a **starting library** for Melody to confirm/adjust. Melody owns the final wording; the agent must use her approved phrasing, not improvise compliance language.

## A. Replacement / switching disadvantages (ROP cases) — insert when `is_rop = yes`

The four general disadvantages the client must be shown (from the "For Switching / Replacement of Policies" declaration):

1. May incur penalties / transaction costs for terminating the existing policy(ies) or investment product(s), without gaining any real benefit from replacing them.
2. Financial benefits accumulated over the years may be lost.
3. May be offered a lower level of benefit at a higher or same cost (or the same benefit at higher cost); the new policy may be less suitable.
4. If existing medical conditions are covered by the existing plan, coverage for those conditions may be lost.

Plus the standard acknowledgements seen in the narratives:
- Pre-existing medical conditions may not be covered under the new policy; a 90-day waiting period may apply before certain benefits take effect.
- The client may incur losses on premiums already paid on the existing policy, and will lose existing coverage on surrender.
- Other options — increasing the sum assured under the existing policy, attaching riders, or converting the policy — were explored; replacement was recommended only after assessment confirmed it suitable and in the client's best interest.
- The product was recommended after the Financial Consultant performed a cost-benefit comparison with the policy to be replaced, as this is a replacement of policy.

## B. General recommendation disclosures — insert on every BOR

- The client was informed that in the event of non-disclosure of any pre-existing medical conditions, the insurer has the right to not pay out benefits as stated if diagnosed due to pre-existing conditions. **GIO CARVE-OUT (R20, Melody 2026-08-03): omit this sentence entirely for a guaranteed issue offer (GIO) product — it is issued without underwriting, so a non-disclosure warning is wrong on its face. HSBC Life Wealth Abundance is GIO. If GIO status is unclear, ask one line before drafting rather than inserting the sentence by default.** The other three sentences below are inserted regardless.
- The product was recommended after fact-find, needs analysis, and product comparison.
- The client is aware that the Financial Consultant may receive additional commission for selling the recommended product.
- The client has agreed for soft copies of the documents to be electronically mailed after the company has processed them.

## C. ILP-specific disclosures — insert when an investment-linked product is involved

- The ILP comes with charges, investment risks, exposure to market volatility, and non-guaranteed returns; past performance is not an indication of future performance.
- Saving/investment needs may not be met and might be insufficient as returns are non-guaranteed and dependent on the future performance of the chosen fund; the client would like to address any remaining shortfall at a later date.
- Surrender charges apply if the client surrenders before the {{Nth}} year, reducing to 0% at the {{N+1}}th-year mark (per product summary).
- A welcome / start-up bonus applies on early-year premiums and can help hedge against market downturn.
- A premium-holiday feature is available after the {{Nth}} year, allowing the client to pause premiums under financial hardship; the client is aware this affects targeted returns on withdrawal/surrender.
- The client confirms no gift was provided, and any promotion offered did not influence the decision to purchase.

## Usage rules
- The agent **inserts** the relevant blocks (A for ROP, B always, C for ILP); it does **not** paraphrase compliance language.
- `{{…}}` slots inside C are filled from collected facts (surrender-charge year, premium-holiday year) — never invented.
- If Melody's approved wording differs from this starter library, **her wording wins** — this file is replaced with her confirmed text before live use.
