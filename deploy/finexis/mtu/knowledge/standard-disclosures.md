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

- The client was informed that in the event of non-disclosure of any pre-existing medical conditions, the insurer has the right to not pay out benefits as stated if diagnosed due to pre-existing conditions. **GIO CARVE-OUT (R20, Melody 2026-08-03): omit this sentence entirely for a guaranteed issue offer (GIO) product — it is issued without underwriting, so a non-disclosure warning is wrong on its face. HSBC Life Wealth Abundance is GIO. NEVER ASK THE ADVISOR WHETHER A PRODUCT IS GIO OR UNDERWRITTEN, and never withhold a draft over it: the runtime resolves the underwriting scope deterministically from the case record's product category (`reference/065-disclaimer-selection.yaml`) and removes this sentence when it does not apply. An earlier version of this line said to "ask one line before drafting" when GIO status is unclear; the constitution's GENERAL DISCLOSURES — GIO CARVE-OUT is the authority and forbids the ask, so the ask is gone. A category that resolves to no underwriting scope fails assembly closed — it never falls back to inserting this sentence.** The other three sentences below are inserted regardless.
- The product was recommended after fact-find, needs analysis, and product comparison.
- The client is aware that the Financial Consultant may receive additional commission for selling the recommended product.
- The client has agreed for soft copies of the documents to be electronically mailed after the company has processed them.

## C. ILP-specific disclosures — insert when an investment-linked product is involved

**BLOCK C IS NOT INSERTED WHOLE.** The ILP GATE in the constitution is the authority: the generic, always-applicable ILP wording is limited to charges, investment risk, market volatility, non-guaranteed returns, and past performance. Everything below that line is conditional on a supplied product summary, and two items may never carry values at all. This file used to read as a single always-inserted block, which is how welcome-bonus and surrender-charge values with nothing behind them could reach a draft.

**C1 — generic ILP wording. Always inserted for an ILP. This is the WHOLE always-inserted set — the ILP GATE limits the generic, always-applicable wording to charges, investment risk, market volatility, non-guaranteed returns, and past performance, and that is exactly the one sentence below.**

- The ILP comes with charges, investment risks, exposure to market volatility, and non-guaranteed returns; past performance is not an indication of future performance.

**C1b — client-intent sentence. NOT generic and NOT a default.** The shortfall sentence states something about THE CLIENT'S INTENTION, so it is only true when the advisor actually said it. Insert it only when the advisor supplied a remaining-shortfall-to-address-later intent; otherwise omit it entirely. Writing it unprompted is inventing a client intention (MTU-018).

- Saving/investment needs may not be met and might be insufficient as returns are non-guaranteed and dependent on the future performance of the chosen fund; the client would like to address any remaining shortfall at a later date.

**C2 — standard-per-product. Written from the named product's standard terms; NEVER asked of the advisor (R04). State a specific year or percentage ONLY when a supplied product summary confirms that exact feature and its values.**

- Surrender charges apply if the client surrenders before the {{Nth}} year, reducing to 0% at the {{N+1}}th-year mark (per product summary).
- A premium-holiday feature is available after the {{Nth}} year, allowing the client to pause premiums under financial hardship; the client is aware this affects targeted returns on withdrawal/surrender.

**C3 — varies-per-case. NEVER stated as a fact and NEVER asked of the advisor (R04): welcome/start-up bonus and promotion/gift wording change between campaigns. Do not insert these sentences with values. Instead append one closing line for the advisor, e.g. "Please check whether any start-up bonus or promotion/gift wording applies to this case, as these vary." The two sentences below are the historical corpus wording, kept for reference only — they are not a default insert.**

- A welcome / start-up bonus applies on early-year premiums and can help hedge against market downturn.
- The client confirms no gift was provided, and any promotion offered did not influence the decision to purchase.

**IUL, wrap/platform, and annuity products do not inherit block C at all.**

## Usage rules
- The agent **inserts** the relevant blocks (A for ROP, B always, C for ILP — and C by the C1 / C1b / C2 / C3 split above, never as one whole block); it does **not** paraphrase compliance language.
- `{{…}}` slots inside C2 are filled ONLY from a supplied product summary — never invented, never asked for, and the sentence is omitted when the values are not supplied.
- If Melody's approved wording differs from this starter library, **her wording wins** — this file is replaced with her confirmed text before live use.
