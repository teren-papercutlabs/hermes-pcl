# RP1 Allied-Like Role-Play Campaign

_Final staging campaign result — 2026-07-31._

## Executive result

**FAIL.** All 12 locked arcs ran through the real `pa-workflow-dev` SMTP → IMAP → adapter → LM extraction → workflow-engine path. All 25 synthetic emails were sent and observed; the one ITOS state probe also ran. No live PA deployment or real client record was touched.

The interpretation layer classified most messages and usually recovered the primary booking reference, but it did not preserve enough typed payload fields, correlation was correct on only a minority of evaluable comparisons, and no expected action/proposal was durably produced. Even the two clean happy paths failed. This is a real capability boundary, not a harness simulation.

## Population and aggregate rates

| Surface | Matched | Failed | Evidence-limited | Accuracy over evaluable comparisons |
|---|---:|---:|---:|---:|
| Extraction fields | 88 | 220 | 0 | 28.6% (88/308) |
| Correlation | 29 | 51 | 19 | 36.2% (29/80) |
| Action / proposal | 0 | 75 | 0 | 0.0% (0/75) |
| Expected final state | 0 | 98 | 0 | 0.0% (0/98) |
| State probe | 0 | 5 | 0 | 0.0% (0/5) |

- Arc population: **12 executed / 12 authored**; **0 passed, 12 failed**.
- Email population: **25 observed / 25 sent**; **0 passed, 25 failed**.
- Probe population: **1 observed / 1 planned**; **0 passed, 1 failed**.
- Email-level miss classifications: extraction **25**, correlation **21**, decision **25**. One email can carry more than one class.

## Field-level results

### Extraction

| Field | Matched | Failed | Evidence-limited | Accuracy |
|---|---:|---:|---:|---:|
| `corr.bill_of_lading` | 0 | 3 | 0 | 0.0% (0/3) |
| `corr.booking_ref` | 20 | 5 | 0 | 80.0% (20/25) |
| `corr.container_no` | 14 | 1 | 0 | 93.3% (14/15) |
| `corr.eir_reference` | 0 | 2 | 0 | 0.0% (0/2) |
| `corr.entity_key` | 0 | 9 | 0 | 0.0% (0/9) |
| `corr.forwarded_original_message_id` | 0 | 1 | 0 | 0.0% (0/1) |
| `corr.in_reply_to_message_id` | 0 | 2 | 0 | 0.0% (0/2) |
| `corr.job_no` | 12 | 3 | 0 | 80.0% (12/15) |
| `corr.new_bill_of_lading` | 0 | 1 | 0 | 0.0% (0/1) |
| `corr.normalized_content_key` | 0 | 2 | 0 | 0.0% (0/2) |
| `corr.prior_bill_of_lading` | 0 | 1 | 0 | 0.0% (0/1) |
| `corr.thread_parent_message_id` | 0 | 1 | 0 | 0.0% (0/1) |
| `event_type` | 15 | 10 | 0 | 60.0% (15/25) |
| `payload.acknowledgement_request` | 0 | 2 | 0 | 0.0% (0/2) |
| `payload.bill_of_lading` | 0 | 3 | 0 | 0.0% (0/3) |
| `payload.bill_of_lading_status` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.booking_ref` | 1 | 23 | 0 | 4.2% (1/24) |
| `payload.chase_target` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.confirmation_status` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.container_no` | 1 | 14 | 0 | 6.7% (1/15) |
| `payload.content_key` | 0 | 2 | 0 | 0.0% (0/2) |
| `payload.copy_kind` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.correction_phrase` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.customer` | 4 | 2 | 0 | 66.7% (4/6) |
| `payload.customer_deadline` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.customer_impact` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.customer_notice` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.deadline` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.dedupe_strategy` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.delay_type` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.depot_receipt_at` | 0 | 2 | 0 | 0.0% (0/2) |
| `payload.destination` | 0 | 2 | 0 | 0.0% (0/2) |
| `payload.direction` | 5 | 1 | 0 | 83.3% (5/6) |
| `payload.driver_confirmed_at` | 0 | 2 | 0 | 0.0% (0/2) |
| `payload.duplicate_of_message_id` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.eir_reference` | 0 | 2 | 0 | 0.0% (0/2) |
| `payload.empty_depot` | 0 | 6 | 0 | 0.0% (0/6) |
| `payload.equipment` | 0 | 2 | 0 | 0.0% (0/2) |
| `payload.equipment_type` | 0 | 6 | 0 | 0.0% (0/6) |
| `payload.escalation_threat` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.estimated_slip_days` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.forwarded_from_message_id` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.gate_in` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.gate_in_at` | 0 | 3 | 0 | 0.0% (0/3) |
| `payload.gate_in_claim` | 0 | 2 | 0 | 0.0% (0/2) |
| `payload.gross_mass_kg` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.job_no` | 0 | 15 | 0 | 0.0% (0/15) |
| `payload.mail_class` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.method` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.new_bill_of_lading` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.noise_marker` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.origin` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.pickup_date` | 0 | 4 | 0 | 0.0% (0/4) |
| `payload.pickup_reference` | 0 | 6 | 0 | 0.0% (0/6) |
| `payload.pickup_status` | 0 | 2 | 0 | 0.0% (0/2) |
| `payload.pickup_window_end` | 0 | 5 | 0 | 0.0% (0/5) |
| `payload.pickup_window_start` | 0 | 5 | 0 | 0.0% (0/5) |
| `payload.prior_bill_of_lading` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.reason` | 0 | 2 | 0 | 0.0% (0/2) |
| `payload.requested_handling` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.requested_job_id` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.requested_resolution` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.required_validation` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.route` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.same_booking` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.seal_no` | 2 | 0 | 0 | 100.0% (2/2) |
| `payload.source_claim` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.source_message_id` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.submitted_at` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.submitted_by` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.terminal` | 1 | 2 | 0 | 33.3% (1/3) |
| `payload.terminal_receipt` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.timestamp_discrepancy_minutes` | 0 | 2 | 0 | 0.0% (0/2) |
| `payload.timestamp_discrepancy_status` | 0 | 2 | 0 | 0.0% (0/2) |
| `payload.tone` | 0 | 2 | 0 | 0.0% (0/2) |
| `payload.urgency` | 0 | 2 | 0 | 0.0% (0/2) |
| `payload.vessel` | 9 | 3 | 0 | 75.0% (9/12) |
| `payload.vessel_name` | 0 | 5 | 0 | 0.0% (0/5) |
| `payload.vgm_document_status` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.vgm_kg` | 0 | 3 | 0 | 0.0% (0/3) |
| `payload.vgm_method` | 1 | 2 | 0 | 33.3% (1/3) |
| `payload.vgm_status` | 0 | 1 | 0 | 0.0% (0/1) |
| `payload.voyage` | 2 | 7 | 0 | 22.2% (2/9) |
| `payload.weighing_ticket` | 1 | 0 | 0 | 100.0% (1/1) |

### Correlation

| Field | Matched | Failed | Evidence-limited | Accuracy |
|---|---:|---:|---:|---:|
| `correlation.buffered_event_to_consume` | 0 | 1 | 0 | 0.0% (0/1) |
| `correlation.candidate_count` | 0 | 0 | 6 | n/a |
| `correlation.candidate_count_after_creation` | 0 | 1 | 0 | 0.0% (0/1) |
| `correlation.candidates_rejected` | 0 | 0 | 1 | n/a |
| `correlation.compatible_candidate_count` | 0 | 0 | 3 | n/a |
| `correlation.created_instance` | 0 | 1 | 0 | 0.0% (0/1) |
| `correlation.created_step` | 0 | 1 | 0 | 0.0% (0/1) |
| `correlation.created_step_advance` | 0 | 1 | 0 | 0.0% (0/1) |
| `correlation.creation_mode` | 0 | 1 | 0 | 0.0% (0/1) |
| `correlation.current_step` | 0 | 2 | 0 | 0.0% (0/2) |
| `correlation.event_compatible_step` | 0 | 1 | 0 | 0.0% (0/1) |
| `correlation.held_event_released` | 0 | 1 | 0 | 0.0% (0/1) |
| `correlation.held_event_transition` | 0 | 1 | 0 | 0.0% (0/1) |
| `correlation.logical_event_key` | 0 | 1 | 0 | 0.0% (0/1) |
| `correlation.match_method` | 7 | 1 | 0 | 87.5% (7/8) |
| `correlation.method` | 0 | 8 | 0 | 0.0% (0/8) |
| `correlation.next_step` | 0 | 1 | 0 | 0.0% (0/1) |
| `correlation.position` | 0 | 0 | 1 | n/a |
| `correlation.prose_discriminator` | 0 | 1 | 0 | 0.0% (0/1) |
| `correlation.reason` | 0 | 0 | 5 | n/a |
| `correlation.selection_reason` | 0 | 0 | 1 | n/a |
| `correlation.target` | 9 | 16 | 0 | 36.0% (9/25) |
| `correlation.usable_discriminator_count` | 0 | 0 | 2 | n/a |
| `correlation.verdict` | 13 | 12 | 0 | 52.0% (13/25) |

### Action / proposal

| Field | Matched | Failed | Evidence-limited | Accuracy |
|---|---:|---:|---:|---:|
| `agent_action.action` | 0 | 25 | 0 | 0.0% (0/25) |
| `agent_action.constraints` | 0 | 25 | 0 | 0.0% (0/25) |
| `agent_action.kind` | 0 | 25 | 0 | 0.0% (0/25) |

## Per-arc scorecards

| Arc | Emails | Extraction | Correlation | Action | Final state | Miss taxonomy |
|---|---:|---:|---:|---:|---:|---|
| RP1-A01 | 2 | 27.3% (6/22) | 57.1% (4/7) + 7 limited | 0.0% (0/6) | 0.0% (0/3) | correlation, decision, extraction |
| RP1-A02 | 2 | 36.4% (8/22) | 38.5% (5/13) + 3 limited | 0.0% (0/6) | 0.0% (0/8) | correlation, decision, extraction |
| RP1-A03 | 2 | 20.0% (4/20) | 42.9% (3/7) + 8 limited | 0.0% (0/6) | 0.0% (0/6) | correlation, decision, extraction |
| RP1-A04 | 2 | 43.5% (10/23) | 36.4% (4/11) + 1 limited | 0.0% (0/6) | 0.0% (0/7) | correlation, decision, extraction |
| RP1-A05 | 2 | 34.0% (16/47) | 100.0% (4/4) + 0 limited | 0.0% (0/6) | 0.0% (0/12) | decision, extraction |
| RP1-A06 | 2 | 25.0% (8/32) | 50.0% (2/4) + 0 limited | 0.0% (0/6) | 0.0% (0/12) | correlation, decision, extraction |
| RP1-A07 | 3 | 9.8% (4/41) | 16.7% (1/6) + 0 limited | 0.0% (0/9) | 0.0% (0/10) | correlation, decision, extraction |
| RP1-A08 | 2 | 24.1% (7/29) | 75.0% (3/4) + 0 limited | 0.0% (0/6) | 0.0% (0/8) | correlation, decision, extraction |
| RP1-A09 | 3 | 37.5% (9/24) | 11.1% (1/9) + 0 limited | 0.0% (0/9) | 0.0% (0/8) | correlation, decision, extraction |
| RP1-A10 | 1 | 0.0% (0/5) | 33.3% (1/3) + 0 limited | 0.0% (0/3) | 0.0% (0/9) | correlation, decision, extraction |
| RP1-A11 | 2 | 44.4% (8/18) | 0.0% (0/6) + 0 limited | 0.0% (0/6) | 0.0% (0/7) | correlation, decision, extraction |
| RP1-A12 | 2 | 32.0% (8/25) | 16.7% (1/6) + 0 limited | 0.0% (0/6) | 0.0% (0/8) | correlation, decision, extraction |

### RP1-A01
**Coverage:** shared_booking_ref, step_compatibility_selects_one, prose_discriminator, deterministic_match, past_step_noop.
**Result:** FAIL. Extraction 27.3% (6/22); correlation 57.1% (4/7) with 7 evidence-limited comparisons; action 0.0% (0/6); final-state 0.0% (0/3). Miss classes: correlation, decision, extraction.
**Observed email outcomes:** `rpa01-001@rp1.synthetic.test` → `pickup_advice` / `matched` / `job:RP1-JOB-0101`; `rpa01-002@rp1.synthetic.test` → `pickup_advice` / `superseded` / `job:RP1-JOB-0101`.
**DB citations:** `wf_event.id=6`; `wf_event.id=7`; `wf_instance.entity_key=job:RP1-JOB-0101`; `wf_instance.entity_key=job:RP1-JOB-0102`; `wf_event.external_id in arc wire Message-IDs`.
### RP1-A02
**Coverage:** unmatched_event, signal_with_start, late_heal, deterministic_rematch_sweep.
**Result:** FAIL. Extraction 36.4% (8/22); correlation 38.5% (5/13) with 3 evidence-limited comparisons; action 0.0% (0/6); final-state 0.0% (0/8). Miss classes: correlation, decision, extraction.
**Observed email outcomes:** `rpa02-001@rp1.synthetic.test` → `pickup_advice` / `unmatched` / `None`; `rpa02-002@rp1.synthetic.test` → `trucking_instruction` / `matched` / `RP1-JOB-0201`.
**DB citations:** `wf_event.id=9`; `wf_event.id=10`; `wf_instance.entity_key=job:RP1-JOB-0201`; `wf_instance.entity_key=RP1-JOB-0201`; `wf_event.external_id in arc wire Message-IDs`.
### RP1-A03
**Coverage:** shared_booking_ref, two_plausible_matches, ambiguous_match, human_pick_required, no_auto_apply.
**Result:** FAIL. Extraction 20.0% (4/20); correlation 42.9% (3/7) with 8 evidence-limited comparisons; action 0.0% (0/6); final-state 0.0% (0/6). Miss classes: correlation, decision, extraction.
**Observed email outcomes:** `rpa03-001@rp1.synthetic.test` → `pickup_advice` / `ambiguous` / `None`; `rpa03-002@rp1.synthetic.test` → `None` / `routed_out` / `None`.
**DB citations:** `wf_event.id=13`; `wf_event.id=14`; `wf_instance.entity_key=job:RP1-JOB-0301`; `wf_instance.entity_key=job:RP1-JOB-0302`; `wf_event.external_id in arc wire Message-IDs`.
### RP1-A04
**Coverage:** out_of_order_event, future_step_buffer, compatible_step_advance, buffer_consumed_on_advance.
**Result:** FAIL. Extraction 43.5% (10/23); correlation 36.4% (4/11) with 1 evidence-limited comparisons; action 0.0% (0/6); final-state 0.0% (0/7). Miss classes: correlation, decision, extraction.
**Observed email outcomes:** `rpa04-001@rp1.synthetic.test` → `gate_in` / `buffered` / `job:RP1-JOB-0401`; `rpa04-002@rp1.synthetic.test` → `vgm_reply` / `matched` / `job:RP1-JOB-0401`.
**DB citations:** `wf_event.id=16`; `wf_event.id=17`; `wf_instance.entity_key=job:RP1-JOB-0401`; `wf_event.external_id in arc wire Message-IDs`.
### RP1-A05
**Coverage:** duplicate_message_id, forwarded_duplicate, content_dedupe, superseded_noop.
**Result:** FAIL. Extraction 34.0% (16/47); correlation 100.0% (4/4) with 0 evidence-limited comparisons; action 0.0% (0/6); final-state 0.0% (0/12). Miss classes: decision, extraction.
**Observed email outcomes:** `<rp1-a05-forwarded-0501@rp1.synthetic.test>` → `pickup_advice` / `superseded` / `job:RP1-JOB-0501`; `<rp1-a05-original-0501@rp1.synthetic.test>` → `pickup_advice` / `matched` / `job:RP1-JOB-0501`.
**DB citations:** `wf_event.id=20`; `wf_event.id=19`; `wf_instance.entity_key=job:RP1-JOB-0501`; `wf_event.external_id in arc wire Message-IDs`.
### RP1-A06
**Coverage:** mid_thread_correction, bill_of_lading_change, contradiction, needs_review_no_silent_overwrite.
**Result:** FAIL. Extraction 25.0% (8/32); correlation 50.0% (2/4) with 0 evidence-limited comparisons; action 0.0% (0/6); final-state 0.0% (0/12). Miss classes: correlation, decision, extraction.
**Observed email outcomes:** `<rp1-a06-correction-0601b@rp1.synthetic.test>` → `container_assigned` / `buffered` / `job:RP1-JOB-0601`; `<rp1-a06-initial-0601@rp1.synthetic.test>` → `pickup_advice` / `buffered` / `job:RP1-JOB-0601`.
**DB citations:** `wf_event.id=23`; `wf_event.id=22`; `wf_instance.entity_key=job:RP1-JOB-0601`; `wf_event.external_id in arc wire Message-IDs`.
### RP1-A07
**Coverage:** chase, angry_customer_escalation, urgency_extraction, tone_extraction, proposed_reply, no_autonomous_send.
**Result:** FAIL. Extraction 9.8% (4/41); correlation 16.7% (1/6) with 0 evidence-limited comparisons; action 0.0% (0/9); final-state 0.0% (0/10). Miss classes: correlation, decision, extraction.
**Observed email outcomes:** `<rp1-a07-carrier-0701@rp1.synthetic.test>` → `gate_in` / `buffered` / `job:RP1-JOB-0701`; `<rp1-a07-chase-0701@rp1.synthetic.test>` → `None` / `routed_out` / `None`; `<rp1-a07-customer-escalation-0701@rp1.synthetic.test>` → `None` / `routed_out` / `None`.
**DB citations:** `wf_event.id=25`; `wf_event.id=26`; `wf_event.id=27`; `wf_instance.entity_key=job:RP1-JOB-0701`; `wf_event.external_id in arc wire Message-IDs`.
### RP1-A08
**Coverage:** itos_state_mismatch, email_claim_gate_in, state_poll_not_gate_in, needs_review, exception_proposal.
**Result:** FAIL. Extraction 24.1% (7/29); correlation 75.0% (3/4) with 0 evidence-limited comparisons; action 0.0% (0/6); final-state 0.0% (0/8). Miss classes: correlation, decision, extraction.
**Observed email outcomes:** `<rp1-a08-carrier-gatein-0801@rp1.synthetic.test>` → `gate_in` / `matched` / `job:RP1-JOB-0801`; `<rp1-a08-forward-0801@rp1.synthetic.test>` → `gate_in` / `superseded` / `job:RP1-JOB-0801`.
**DB citations:** `wf_event.id=29`; `wf_event.id=30`; `wf_instance.entity_key=job:RP1-JOB-0801`; `wf_event.external_id in arc wire Message-IDs`; `wf_event.id=31`; `state_poll.entity_key=job:RP1-JOB-0801`.
### RP1-A09
**Coverage:** partial_info_dribble, cumulative_extraction, no_invention, buffered_future_event.
**Result:** FAIL. Extraction 37.5% (9/24); correlation 11.1% (1/9) with 0 evidence-limited comparisons; action 0.0% (0/9); final-state 0.0% (0/8). Miss classes: correlation, decision, extraction.
**Observed email outcomes:** `rp1-a09-1@rp1.synthetic.test` → `pickup_advice` / `buffered` / `job:RP1-JOB-0901`; `rp1-a09-2@rp1.synthetic.test` → `container_assigned` / `buffered` / `job:RP1-JOB-0901`; `rp1-a09-3@rp1.synthetic.test` → `vgm_reply` / `unmatched` / `None`.
**DB citations:** `wf_event.id=33`; `wf_event.id=34`; `wf_event.id=35`; `wf_instance.entity_key=job:RP1-JOB-0901`; `wf_event.external_id in arc wire Message-IDs`.
### RP1-A10
**Coverage:** wrong_recipient, noise_mail, routed_out, no_mutation.
**Result:** FAIL. Extraction 0.0% (0/5); correlation 33.3% (1/3) with 0 evidence-limited comparisons; action 0.0% (0/3); final-state 0.0% (0/9). Miss classes: correlation, decision, extraction.
**Observed email outcomes:** `rp1-a10-1@rp1.synthetic.test` → `None` / `routed_out` / `None`.
**DB citations:** `wf_event.id=36`; `wf_event.external_id in arc wire Message-IDs`.
### RP1-A11
**Coverage:** clean_happy_path, strong_unique_keys, create_job_proposal.
**Result:** FAIL. Extraction 44.4% (8/18); correlation 0.0% (0/6) with 0 evidence-limited comparisons; action 0.0% (0/6); final-state 0.0% (0/7). Miss classes: correlation, decision, extraction.
**Observed email outcomes:** `rp1-a11-1@rp1.synthetic.test` → `trucking_instruction` / `unmatched` / `None`; `rp1-a11-2@rp1.synthetic.test` → `pickup_advice` / `buffered` / `job:RP1-JOB-1101`.
**DB citations:** `wf_event.id=38`; `wf_event.id=39`; `wf_instance.entity_key=job:RP1-JOB-1101`; `wf_event.external_id in arc wire Message-IDs`.
### RP1-A12
**Coverage:** clean_happy_path, vgm_progress, gate_in_progress, strong_unique_keys, customer_update_proposal.
**Result:** FAIL. Extraction 32.0% (8/25); correlation 16.7% (1/6) with 0 evidence-limited comparisons; action 0.0% (0/6); final-state 0.0% (0/8). Miss classes: correlation, decision, extraction.
**Observed email outcomes:** `rp1-a12-1@rp1.synthetic.test` → `vgm_reply` / `matched` / `job:RP1-JOB-1201`; `rp1-a12-2@rp1.synthetic.test` → `gate_in` / `unmatched` / `None`.
**DB citations:** `wf_event.id=41`; `wf_event.id=42`; `wf_instance.entity_key=job:RP1-JOB-1201`; `wf_event.external_id in arc wire Message-IDs`.

## Campaign findings

1. **Stale release assembly — engine defect, repaired before measurement.** Staging selected one release while systemd and the editable finder loaded an older release. The permanent repair rebuilt a release-local environment and added interpreter/module-origin promotion checks. Literal SMTP → IMAP → `wf_event` verification passed after deploy.
2. **Live ingress lacked the LM extraction leg — missing contract leg, repaired before measurement.** Earlier harnesses injected already-extracted payloads. Live email ingress did not call the extractor. The new tenant-neutral ingress leg now invokes auxiliary-LM extraction using the registered template contract and then uses the existing validation and propose-lane semantics.
3. **Recorder defects — campaign harness defects, repaired without changing capability.** Empty classified payloads and seedless arcs originally aborted the measurement instead of recording misses. The helper now captures those outcomes and preserves DB citations; it does not affect engine decisions or answer keys.
4. **Gmail canonicalizes plus aliases — staging integration fact.** Display-name personas remained available, but the envelope/header sender collapsed to dorm1. A default-off, tenant-neutral self-ingress test capability was used in staging; the production echo-prevention default stayed unchanged.
5. **Board-global template resolution — platform gap, deliberately not built in-campaign.** Create matching and email-extraction contract selection resolve across the board rather than to a template-scoped/latest-per-slug target. Allied will run multiple workflows, so ambiguity is certain rather than hypothetical. This needs a post-campaign platform WB; changing it during the campaign would contaminate the measurement.
6. **Interpretation capability line — extraction/correlation/decision misses.** `event_type` classification was 15/25 and primary correlation keys were often recovered, but typed payload retention was weak, correlation target/verdict frequently differed from the locked key, and action/proposal accuracy was 0/75. These are score-as-is findings, not defects quietly patched to the answer key.

## Evidence contract

- Locked stories and answer keys: `arcs-01-04.json`, `arcs-05-08.json`, `arcs-09-12.json`.
- Exact execution plan: `evidence/rp1-plan.json`.
- As-it-happens ingress journal: `evidence/rp1-live-journal.jsonl` (**230 rows**; 25 sends, 25 observations, two preflights, and the resumed-wave history).
- Durable DB observations and **84 citation records**: `evidence/rp1-observed.json`.
- Mechanical scorer output: `evidence/rp1-score.json`.
- Campaign decision/finding log: `dive-records/rp.jsonl`.
- Preflight failure journals are preserved under `evidence/`; they were not rewritten into success.
- Citations name durable staging row identities such as `wf_event.id=N` and `wf_instance.entity_key=…`. Candidate/reason details absent from the DB are marked evidence-limited rather than inferred.

## Conclusion

The chassis now carries a real email through extraction and workflow handling, but RP1 shows that this is not yet an Allied-ready interpretation layer. The next capability work should be demand-led from this result: first make template selection unambiguous for multiple workflows, then improve the specific extraction, correlation, and proposal behaviors reflected by the failed locked keys. The campaign itself is complete; rerunning it unchanged after those scoped changes is the clean regression test.
