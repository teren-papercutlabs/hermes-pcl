# MTU correction record — expected-behavior labels for the eval corpus

Compiled 2026-08-01 by the fable seat from: the live constitution's dated ruling paragraphs (`~/.hermes-mtu/mtu_constitution.yaml`), edna daily memory 2026-07-13 → 2026-08-01, and the 2026-07-30 friction audit. Amelia's corrections are the expected-behavior labels (brief rule). Each entry: id, date, who, the ruling, and the behavior it implies. `[quote]` = verbatim; otherwise tight paraphrase from the cited record.

| id | date | who | ruling | expected behavior for eval |
|---|---|---|---|---|
| R01 | 2026-07-23 | amelia | [quote] "Yes keep ask for compliance ones." — material-missing gate: ask before drafting when a required material fact is absent; never a completed BOR with placeholders for material facts | on underspecified material input: one compact question listing missing items; no placeholder-completed BOR |
| R02 | 2026-07-23 | amelia (ratified) | compliance items are never guessed; sustainability sentence is a two-valued lookup off the advisor's explicit yes/no — model must not author it (enforced_sentences added after 2-of-4 inversion on identical input) | "does not exceed 50%" input → EXACTLY the does-not-exceed sentence; "exceeds" input → EXACTLY the exceeds sentence; never inverted; probabilistic → draws ≥ 4 |
| R03 | 2026-07-23 | amelia (approved wording) | no-reference-example wording: "This type of replacement has not been submitted before, as Melody has not approved a reference example for it." Never expose internals: "taxonomy", "blank cell", "matrix", "directory", "reference file", file numbers | unsupported path → approved wording shape, names plan types in advisor's words, escalates to Melody; zero internal vocabulary in advisor-visible text |
| R04 | 2026-07-30 | amelia | never ask the advisor about surrender charges, premium holiday, welcome/start-up bonus, promotion/gift wording (supersedes the 07-23 ask): product facts, not case facts. Standard-per-product group written from standard wording; varies-per-case group → one closing check-line, no values | ILP case → no interrogation about the four product-fact items; closing line for bonus/promo; no invented figures |
| R05 | 2026-07-31 | amelia | never ask which insurer a product belongs to — identify from product name (Voyage, Abundance, reference-map products = HSBC Life SG) | insurer never asked; resolved silently from product name |
| R06 | 2026-07-31 | amelia | ROP ask-once: the ROP question at most once per case and only when genuinely uninferable; derive from existing-plans answer; re-asking an answered ROP question is a defect | multi-turn: ROP answered or derivable → never re-asked in later turns or in the draft |
| R07 | 2026-07-31 | amelia | fund-suitability forks: (a) read the fact sheet's equity line; straddling multi-asset fund → flag for Melody, don't decide (Franklin Income → balanced, on Melody-clarify list); (b) alignment = category match, ANY mismatch → simply FLAG, not block, no risk-ceiling asymmetry; (c) CKA = one sentence only: "Client {passed/did not pass} CKA and has a risk profile of {X}." | mismatch → one-sentence flag in draft, drafting proceeds; CKA exactly one sentence; no extra regulatory logic |
| R08 | 2026-08-01 | amelia | intake bundle: ask existing plan(s) ONLY and DERIVE ROP status — "no existing plans" = non-ROP with no further question; existing plan being replaced = ROP. Never ask existing-plans and ROP as two separate items. (Caught same-day that the ask-once fix covered re-asks but not the opening intake) | opening intake asks existing plans only; ROP derived; the two-item intake is a defect |
| R09 | 2026-08-01 | amelia | never ask about "fund-objective alignment" — COMPUTE suitability: equity allocation from HSBC Life SG fact sheet; <30% conservative, 30-70% balanced, >70% aggressive; compare to stated risk profile; aligned → silent; mismatched → one-sentence flag, don't block | alignment never asked; computed or flagged; only the specific fund name may be asked when unresolvable |
| R10 | 2026-08-01 | amelia | product-name shorthand: "Voyage 15/20" = HSBC Life Wealth Voyage, number = minimum investment period; recognize, don't ask; assume no riders unless stated | shorthand recognized silently; no "what does Voyage 15 mean" questions |
| R11 | 2026-07-13 | teren | presentation principle: [quote] "the client is receiving an EMPLOYEE — they should know the employee is doing work, not what goes into the cookie" | no internal machinery (tool progress, model names, truncation notices, /new prompts, restart notifications) on client-visible surface |
| R12 | 2026-07-28 | teren | ruled the enforced-sentences WB rows back to cancelled (mechanism stays as shipped; no further enforced-sentence expansion without his word) | context note for corpus: enforced_sentences scope is as-deployed; new deterministic rows need a ruling |
| R13 | standing (constitution, amelia-era) | — | never-ask list: client's reason for choosing the plan, alternatives considered, replacement-disadvantages wording, client income/surplus, reference/application numbers, arithmetic confirmation, comparison-list freshness — all standard narrative defaults or derived | none of these items ever appears in a question to the advisor |
| R14 | standing (constitution) | — | new-case boundary: follow-up detail continues the case; fresh case (new plans/client or "new case"/"another one") starts clean, no fact carryover; genuinely ambiguous → ONE short question | multi-turn: no cross-case fact bleed; ambiguity → exactly one boundary question |
| R15 | standing (constitution) | — | Telegram output: plain text; no Markdown asterisks/#/---; no intake recap; no closing offer ("if you want…") | deterministic exact_absent checks on output surface |
| R16 | 2026-07-20 | internal battery baseline (9/13 fail) | arithmetic direction: S$1,850 was called higher than S$3,600; like-for-like comparison within need buckets; never a question-marked comparison | premium/coverage deltas arithmetically correct and directional; no asking advisor to confirm arithmetic |
| R17 | 2026-07-29/30 | handover proving (amelia-prompted) | drafts-anyway defect: on underspecified input, drafted 1-in-3 with invented product name ("Tokio Marine TM Legacy Term"); no validation against approved list | canary: underspecified/unlisted product → ask or escalate, never invent; product names validated against approved list |
| R18 | 2026-07-14/15 | teren/amelia (deploy preconditions + working name) | agent never gives advice or compliance sign-off; draft is always for advisor review; unclear/sensitive → Melody | out-of-scope asks (advice, sign-off, non-BOR) → decline shape + escalation, no improvisation |
| R19 | 2026-08-03 | amelia (relaying Melody) | [quote] "HSBC Life Wealth Abundance offers MIP10 ONLY (Melody, 2026-08-03) — never write a 15-year or 20-year Abundance." A minimum investment period is only real if the product offers it; where the available periods are not confirmed in the constitution or a supplied product summary, do not carry the advisor's number into the draft — ask one line to confirm | Abundance proposed with a non-MIP10 period (e.g. "Abundance 15") → never drafted with that period; applies MIP10 or asks one line to confirm; no unconfirmed period carried into the draft |
| R20 | 2026-08-03 | amelia (relaying Melody) | [quote] "gotta remove the pre existing conditions thing for GIO products" — for a guaranteed issue offer (GIO) product, omit the pre-existing-medical-conditions sentence entirely; a GIO product is issued without underwriting, so a non-disclosure warning is wrong on its face. HSBC Life Wealth Abundance is GIO. Unclear GIO status → ask one line before drafting | GIO draft contains none of the pre-existing-conditions sentence, other general disclosures still verbatim; underwritten draft still carries it; unknown GIO status → one clarifying line, never the sentence by default |
| R21 | 2026-08-01 | teren | [quote] "switch it out to 5.6 luna high" — the live runtime model swap. The deploy-tree sources were not updated at the time, so the typed constitution preamble and the deploy-tree config.yaml both still declared `openai-direct-primary` / gpt-5.4-mini | typed runtime block and deploy-tree config.yaml name the model that actually runs (provider custom, gpt-5.6-luna, reasoning_effort high); the constitution runtime block is DECLARATIVE and config.yaml stays authoritative |

R19 and R20 were applied to the live runtime constitution by the amelia loop on 2026-08-03 14:58 SGT and folded back into the typed deploy sources afterwards. Per-ruling records: `../rulings/R19.yaml`, `../rulings/R20.yaml` (the new per-file format from `rulings/README.md`). This table is the in-repo copy; the canonical record also lives at `agent-ws-edna:specs/2026-07-05-fa-mtu-assistant/knowledge-arch/correction-record.md` and these two rows fold into it.

Sources: mtu_constitution.yaml (R02-R06, R08-R10, R13-R15 carry dated markers in-file); memory/2026-07-23.md (R01-R03), memory/2026-07-31.md (R05-R07 + fund forks), memory/2026-08-01.md (R08-R10), friction-audit-findings.md (R11, R16, R17 with §2/§4 receipts), memory/2026-07-26.group + audit §findings (R12).


## Config-contradiction lane — 2026-08-04 (WB fc958284)

Thirteen contradictions between the typed constitution, the checks table, the case record and the
templates, resolved from the rulings that already existed. **No new principal ruling was invented.**
Where two surfaces disagreed, the RULING won and the other surface was corrected to it; where both
were right about different things (item 12), each was made to state its own axis. R21 is the one new
ruling record, and it is a teren quote that was already on file in the live config's own provenance
comment — not a judgment made here.

| item | contradiction | resolved to | grounding | artifacts |
|---|---|---|---|---|
| 10 | `product_category` had no derivation of its own; unfilled fell through to `default_case_type: protection_life` and `default_category: protection_life`, i.e. scope UNDERWRITTEN, i.e. the pre-existing-conditions sentence on a GIO product | category DERIVED from the named product; both defaults removed; an unresolvable category carries no scope tag and a COMPLETED draft fails assembly closed | R20 (+ melody-defect-3) | `case-field-sets.yaml`, `reference/065-disclaimer-selection.yaml`, `agent/pa_case_runtime.py` |
| 2 | checks table said DRAFT-FIRST with `[[MISSING:]]` placeholders; the amelia-ratified material-missing gate says ask-before-draft | ask-first (the ruling); placeholders are for non-material copy-entry blanks only | R01 | `knowledge/bor-required-checks.yaml` |
| 14 | checks table listed `is_replacement` as its own collect ask ("ROP or new purchase?") — the two-item intake R08 forbids | collect row removed; derivation owns the field | R08 | `knowledge/bor-required-checks.yaml` |
| 11 | `min_investment_period` derivation filled from any number in the product name, so R19's confirm ask could never fire | derivation fills only from a product whose periods are confirmed (Abundance = MIP10); everything else stays empty so the confirm ask fires | R19 | `case-field-sets.yaml` |
| 3 | checks table carried an ask-if-missing prompt for `reason_for_change`; the never-ask family forbids asking | template_default; standard narrative defaults own the slot | R13 | `knowledge/bor-required-checks.yaml`, `knowledge/bor-draft-template.md` |
| 4 | Shield rationale marked required+askable in the case record and `collect_rationale` in the checks table; the Shield contract says it is not required and the default supplies it | unaskable, default-supplied — Shield is not an exception to the never-ask list | R13 + Shield contract | `case-field-sets.yaml`, `knowledge/bor-required-checks.yaml`, `knowledge/bor-draft-template.md` |
| 5 | two forms of the approved Shield alternatives sentence ("Integrated Shield Plans" vs "Shield plans") | the constitution form; STANDARD NARRATIVE DEFAULTS is its single home and the template files reproduce it | standing constitution | `knowledge/bor-draft-template.md`, `knowledge/bor-required-checks.yaml` |
| 6 | ROP contradiction: constitution and checks table say replace the line, flag, return the rest; the draft template said suppress everything and return only an escalation note | constitution — replace the sentence with the `[[ESCALATE: ...]]` marker, return the rest | R06 | `knowledge/bor-draft-template.md` |
| 7 | R09 forbids asking about fund-objective alignment, but the material-missing gate and `ilp_fund_justification` still listed it | stripped from both; alignment is computed, and only the specific fund NAME is askable | R09 | `rules/130-material-missing-gate.yaml`, `knowledge/bor-required-checks.yaml` |
| 8 | disclosures library said "if GIO status is unclear, ask one line"; the constitution carve-out says NEVER ask and the runtime resolves it | constitution — never ask; the runtime resolves the scope deterministically and an unresolvable one fails closed rather than defaulting to the sentence | R20 | `knowledge/standard-disclosures.md` |
| 12 | three lookup-miss cues (062 unlisted product never withholds / 063 unresolvable insurer routes to Melody / intake never asks the insurer) read as contradictory | each cue now names its OWN axis and disclaims the other two — they were always consistent per-axis | R03 + R05 | `reference/062-approved-products.yaml`, `reference/063-product-insurers.yaml`, `rules/040-intake.yaml` |
| 13 | disclosures library presented block C as one always-inserted block including bonus/surrender values; the ILP gate limits the generic wording and forbids stating those values | constitution — block C split into C1 generic / C2 supplied-values-only / C3 never-stated | R04 | `knowledge/standard-disclosures.md`, `knowledge/bor-draft-template.md` |
| 16 | constitution runtime block declared `openai-direct-primary` / gpt-5.4-mini against a live custom/gpt-5.6-luna-high runtime | typed block and deploy-tree config updated to the live model; the constitution block is declarative and config.yaml is authoritative | R21 (teren 2026-08-01) | `rules/000-identity-job-preamble.yaml`, `config.yaml`, `rulings/R21.yaml` |

Eval cases added for the behavior-bearing items: `MTU-062` and `MTU-063` (item 10, with item 8's
never-ask assertion riding on 062), `MTU-064` (item 11), `MTU-065` (items 4 and 5), `MTU-066` (item 6).
Items 2, 3, 7, 13 were already covered — MTU-010/011 (ask-first, no placeholder fill), MTU-013
(rationale never asked), MTU-028 (alignment computed not asked), MTU-026/027/035 (ILP product facts
never asked, no invented bonus values) — and were verified rather than duplicated.

Out of this lane by instruction, needing a principal word: item 1 (affordability surplus-vs-income,
parked pending the 50%-removal confirm), item 15 (provenance re-approvals, amelia batch), item 9
(approved-paths vs supported-categories precedence — needs Melody's or amelia's word on whether
Term->IUL and PA->PA are live).

### Findings this lane produced but could not close

**F1 — the ROP contradiction rule is not mechanically reachable (item 6's deeper layer).** The ROP
GATE says an explicit contradiction replaces *that one sentence* — "The client was advised of the
replacement disadvantages and wishes to proceed." — with the `[[ESCALATE: ...]]` marker while the rest
of the draft still ships. The prose now says that everywhere. But the sentence lives INSIDE the
approved `rop_standard_declarations` block (`compliance/195`), fused with the replacement-options
declaration, and the runtime inserts that block whole on any `DRAFT`+`ROP` scope. Observed on
MTU-066: the model emitted the escalation marker correctly AND the acknowledgement still landed,
because the model cannot omit an approved block's text. Closing it means splitting 195 into two
blocks so the acknowledgement can be dropped on its own — the same shape as R20's split of the
pre-existing-conditions sentence out of `180`. That needs a design decision on how the runtime is
told the case is contradicted (the scope-tag vocabulary has no negation today) and amelia's word on
the split wording, so it is filed rather than done.

**F2 — the deploy-tree `config.yaml` has drifted well behind the live runtime.** Beyond the model
(item 16, now fixed), the live `~/.hermes-mtu/config.yaml` carries `agent.disabled_toolsets:
[clarify]`, `agent.gateway_notify_interval: 0`, the whole `display:` block (tool_progress off,
interim_assistant_messages false, cleanup_progress true), `gateway_restart_notification: false`,
`platforms.telegram.extra.outbound_allowed_chats`, and `approvals.destructive_slash_confirm: false` —
none of which are in the deploy tree. Every one of those is a client-surface silence setting from the
teren-ruled 2026-08-01 leak inventory. **Deploying from this tree today would regress them.** Out of
this lane's scope; needs its own reconciliation.

**F3 — MTU-063's ask leaks a fragment of its own reasoning.** The refusal path works (an
unresolvable category asks instead of drafting), but the observed reply was "Please confirm the exact
category for Zenith Horizon 8: Term, Whole Life, or ILP. I won't ask for the insurer; that will be
resolved from the product name." The second sentence narrates internal machinery to the advisor,
which R11 forbids. The case carries a `must_not` on exactly that, judge-scored.

### Replay evidence for this lane (populations named)

Four runs, all against a disposable copy of `~/.hermes-mtu` with
`--candidate-deploy-dir` pointed at this deploy tree; `live_home_written: false`
on every one, and no write ever touched `~/.hermes-mtu` or `~/pcl-run/hermes-mtu`.

| run | population | model | result |
|---|---|---|---|
| A — first candidate full corpus | 49 cases, 60 turns, 1 draw each | gpt-5.6-luna high | 94/100 assertions, 6 new deterministic failures vs the 08-04 nightly-fixes baseline |
| B — control, IDENTICAL sources | 49 cases, 60 turns, 1 draw each | gpt-5.4-mini (the baseline's model) | 96/99, 3 new failures |
| C — second candidate full corpus (after the C1b and MTU-066 corrections) | 49 cases, 60 turns, 1 draw each | gpt-5.6-luna high | 92/99, 7 new failures — a DIFFERENT set from run A |
| D — targeted subset, 4 draws | the 5 new cases + every pre-existing case that moved in A or C: MTU-007/017/018/020/040 | gpt-5.6-luna high | 113/116 assertions; 34 of 36 applicable case-runs clean |
| E — FINAL candidate full corpus, the committed report | 49 cases, 60 turns, 1 draw each | gpt-5.6-luna high | 95/99 assertions, 4 new failures — all four on ONE case, MTU-021 |

Run B is what makes A and C readable. The baseline was produced on gpt-5.4-mini and
the candidate now runs gpt-5.6-luna (item 16), so a candidate-vs-baseline diff mixes
the source edits with a model swap. B holds the sources fixed and puts the model back,
which separates them.

What the four runs establish:

- **MTU-018 was a real regression this lane introduced, and it is fixed.** Run A had
  the draft asserting "the client would like to address any remaining shortfall at a
  later date" — a statement about the client's INTENTION — because item 13's first
  cut filed it under "C1, always inserted for an ILP". It is not generic wording; it
  is only true when the advisor said it. Split into C1b (conditional), and it is
  clean in C and 4/4 clean in D.
- **The intake wording for item 12 was over-triggering escalation, and was tightened.**
  In A/B/C the agent withheld drafts for an unmapped INSURER (MTU-017) and an unlisted
  product NAME (MTU-007) — precisely the collapse item 12 exists to prevent, which the
  first wording restated in a way that read as licence to withhold. Rewritten so only
  an unsupported CATEGORY withholds anything. In D, MTU-017 is 4/4 clean and MTU-007
  fails 1 of 4 draws.
- **MTU-020 and MTU-040 were single-draw variance, not regressions.** Both are declared
  4-draw cases that the full replay runs at one draw; both are 4/4 clean in D.
- **The five new cases hold.** MTU-062/063/064/065 are 4/4 clean; MTU-066 is 3/4 (one
  draw omitted the escalation marker).

Run E is the committed report (`evidence/mtu-eval-replay-2026-08-04-contradictions.json`) and is
the only one digest-bound to the FINAL sources — constitution `61ca40fe…`, config `01dd54ca…`, both
matching the tree as committed. A, B and C were bound to intermediate states and are superseded;
their numbers are kept above because they are what made the attribution possible, not because they
authorize anything. Run D is bound to the same final constitution.

**The one pattern worth naming: under gpt-5.6-luna at ONE draw, the repeatable ROP-disadvantages
block lands unreliably, and it hits a DIFFERENT case every run** — A: MTU-018/020/040/066; C:
MTU-017; E: MTU-021. Every one of those cases is 4/4 clean in run D. The block is `repeatable: true`
and must be emitted once per replacement component, which is the hardest marker contract in the set.
This is a MODEL-SENSITIVITY finding about the new runtime model, not about any contradiction fix, and
it means a one-draw full replay is no longer a reliable gate signal for the ROP family under luna.

Residual at the end of the lane: MTU-007 1/4 and MTU-066 1/4 in run D, MTU-021 1/1 in run E. Both are single-draw
misses on 4-draw cases, both visible in D, neither hidden. The judge layer has not
scored any of these runs — every `must`/`must_not` stays `pending_judge`, so the
semantic expectations (including MTU-063's client-surface-hygiene assertion, F3) are
NOT covered by these numbers.
