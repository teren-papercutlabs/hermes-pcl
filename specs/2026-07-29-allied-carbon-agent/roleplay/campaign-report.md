# RP1 Allied-Like Role-Play Campaign

_Checkpoint recorded 2026-07-31T06:52:32.471341+00:00._

## Status

**Execution blocked after answer-key lock.** Twelve story arcs passed the author-wave gate. No arc has been scored because the live staging path cannot yet execute the interpretation contract end to end. Reporting a score before that repair would be a fabricated capability result.

## Objective

Test LM extraction, deterministic correlation, and action/proposal choices against messy freight-forwarding email threads by driving synthetic messages through the real `pa-workflow-dev` SMTP → IMAP → adapter → workflow ledger path.

## Pre-execution acceptance

- 12 arcs authored and committed before any campaign execution.
- Every email has an explicit extraction, correlation, and agent-action answer key.
- Required classes are covered: shared booking reference, late heal, two plausible matches, out-of-order events, duplicate/forwarded mail, BL correction, angry chase, ITOS mismatch, partial dribble, wrong-recipient noise, and two clean paths.
- Synthetic-only staging scope held. No live PA deployment was touched.

## Blocking engine defects

### 1. Selected release executes a prior release interpreter (`314b3736`)

`pa-workflow-dev` resolves `current` to release `ffc5a396…`, while `current/.venv/bin/hermes` names release `b8264f88…` in its absolute shebang. The running service therefore loads the older package set. A controlled real email reached the adapter but produced no new `wf_event`; the live log also reproduced the missing `hermes_cli.pa_credentials` module.

### 2. Live email ingress has no LM-extraction caller (`dbbab08a`)

Current source ingests email with `event_type = NULL` and empty correlation, then the embedded watcher calls `sweep` without calling `extract_event`. A controlled temp-board reproduction moved the raw event from `received` to `routed_out` with `match_method = deterministic`. The only repository caller of `extract_event` is the P5a in-process harness, which the campaign explicitly may not use.

These are independent defects: repairing release coherence exposes the missing interpretation stage rather than fixing it. Both were filed through the bug pipeline; neither was patched inside the campaign.

### Score-as-is boundary

Preflight also found locked expectations beyond the current registered template and durable evidence surface: 8 of 25 email steps use event types the existing synthetic template does not declare; seeded `ingest` instances cannot current-match create-on events; and the engine does not persist all candidate/reason/action details requested by the answer keys. The orchestrator ruled these are campaign data, not build scope. The campaign will execute them unchanged, classify the actual gaps, and mark cells `EVIDENCE-LIMITED` where the consumer surface cannot support a stronger claim. No capability will be added merely to make the locked keys pass.

## Arc scorecards

All scorecards remain **NOT RUN**. There are no campaign DB citations yet, so aggregate rates are intentionally withheld.

| Arc | Coverage | Emails | Execution | Extraction | Correlation | Action | Miss class |
|---|---|---:|---|---|---|---|---|
| RP1-A01 | shared_booking_ref, step_compatibility_selects_one, prose_discriminator, deterministic_match, past_step_noop | 2 | NOT RUN | — | — | — | engine-defect (campaign gate) |
| RP1-A02 | unmatched_event, signal_with_start, late_heal, deterministic_rematch_sweep | 2 | NOT RUN | — | — | — | engine-defect (campaign gate) |
| RP1-A03 | shared_booking_ref, two_plausible_matches, ambiguous_match, human_pick_required, no_auto_apply | 2 | NOT RUN | — | — | — | engine-defect (campaign gate) |
| RP1-A04 | out_of_order_event, future_step_buffer, compatible_step_advance, buffer_consumed_on_advance | 2 | NOT RUN | — | — | — | engine-defect (campaign gate) |
| RP1-A05 | duplicate_message_id, forwarded_duplicate, content_dedupe, superseded_noop | 2 | NOT RUN | — | — | — | engine-defect (campaign gate) |
| RP1-A06 | mid_thread_correction, bill_of_lading_change, contradiction, needs_review_no_silent_overwrite | 2 | NOT RUN | — | — | — | engine-defect (campaign gate) |
| RP1-A07 | chase, angry_customer_escalation, urgency_extraction, tone_extraction, proposed_reply, no_autonomous_send | 3 | NOT RUN | — | — | — | engine-defect (campaign gate) |
| RP1-A08 | itos_state_mismatch, email_claim_gate_in, state_poll_not_gate_in, needs_review, exception_proposal | 2 | NOT RUN | — | — | — | engine-defect (campaign gate) |
| RP1-A09 | partial_info_dribble, cumulative_extraction, no_invention, buffered_future_event | 3 | NOT RUN | — | — | — | engine-defect (campaign gate) |
| RP1-A10 | wrong_recipient, noise_mail, routed_out, no_mutation | 1 | NOT RUN | — | — | — | engine-defect (campaign gate) |
| RP1-A11 | clean_happy_path, strong_unique_keys, create_job_proposal | 2 | NOT RUN | — | — | — | engine-defect (campaign gate) |
| RP1-A12 | clean_happy_path, vgm_progress, gate_in_progress, strong_unique_keys, customer_update_proposal | 2 | NOT RUN | — | — | — | engine-defect (campaign gate) |

## Aggregate rates

Not computed. Population: 12 authored arcs / 25 authored email steps; executed population: 0 arcs / 0 email steps. The denominator travels with the number.

## Miss taxonomy

- **Engine defect:** 2 pre-execution blockers.
- **Extraction:** not measurable until the live extractor is wired.
- **Correlation:** not measurable until extracted events reach matching.
- **Decision:** not measurable until matched events generate proposals.
- **Key defect:** none found during the author-wave gate.

## Next gate

Resume only after both fixes are consumer-verified on `pa-workflow-dev`: a fresh external email must create a workflow event under the selected release, and that event must be LM-extracted before deterministic classification. Then execute the locked fixtures without editing their answer keys and replace each NOT RUN row with DB-cited results.
