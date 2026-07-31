# RP1 Allied-Like Role-Play Campaign

_Final staging campaign result — 2026-07-31._

## Executive result

**FAIL.** All 12 locked arcs ran through the real `pa-workflow-dev` SMTP → IMAP → adapter → LM extraction → workflow-engine path. All 25 synthetic emails were sent and observed; the one ITOS state probe also ran. No live PA deployment or real client record was touched.

The interpretation layer classified most messages and usually recovered the primary booking reference, but it did not preserve enough typed payload fields and correlation was correct on fewer than half of evaluable comparisons. The action/proposal surface was not durably observable, so this campaign makes no action-accuracy claim. Even the two clean happy paths failed on observable fields. This is a real capability boundary, not a harness simulation.

## Population and aggregate rates

| Surface | Matched | Failed | Evidence-limited | Accuracy over evaluable comparisons |
|---|---:|---:|---:|---:|
| Extraction fields | 89 | 219 | 0 | 28.9% (89/308) |
| Correlation | 38 | 42 | 19 | 47.5% (38/80) |
| Action / proposal | 0 | 0 | 75 | n/a |
| Expected final state | 34 | 27 | 57 | 55.7% (34/61) |
| State probe | 4 | 0 | 1 | 100.0% (4/4) |

- Arc population: **12 executed / 12 authored**; all 12 had at least one observable email failure. Final-state scoring separately found **10 failed / 2 evidence-limited / 0 passed**.
- Email population: **25 observed / 25 sent**; **0 passed, 25 failed**.
- Probe population: **1 observed / 1 planned**; **0 failed, 1 evidence-limited**. Four observable probe claims matched; the expected proposal was not durably observable.
- Email-level miss classifications: extraction **25**, correlation **20**, decision **0**. One email can carry more than one class. Decision is zero because the proposal surface was not durable; the A08 safety miss is scored in expected final state.

## Field-level results

### Extraction

| Field | Matched | Failed | Evidence-limited | Accuracy |
|---|---:|---:|---:|---:|
| `corr.bill_of_lading` | 0 | 3 | 0 | 0.0% (0/3) |
| `corr.booking_ref` | 21 | 4 | 0 | 84.0% (21/25) |
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
| `correlation.target` | 18 | 7 | 0 | 72.0% (18/25) |
| `correlation.usable_discriminator_count` | 0 | 0 | 2 | n/a |
| `correlation.verdict` | 13 | 12 | 0 | 52.0% (13/25) |

### Action / proposal

| Field | Matched | Failed | Evidence-limited | Accuracy |
|---|---:|---:|---:|---:|
| `agent_action.action` | 0 | 0 | 25 | n/a |
| `agent_action.constraints` | 0 | 0 | 25 | n/a |
| `agent_action.kind` | 0 | 0 | 25 | n/a |

## Per-arc scorecards

| Arc | Emails | Extraction | Correlation | Action | Final state | Miss taxonomy |
|---|---:|---:|---:|---:|---:|---|
| RP1-A01 | 2 | 27.3% (6/22) | 85.7% (6/7) + 7 limited | n/a | 57.1% (4/7) + 2 limited | correlation, extraction |
| RP1-A02 | 2 | 36.4% (8/22) | 38.5% (5/13) + 3 limited | n/a | 75.0% (3/4) + 4 limited | correlation, extraction |
| RP1-A03 | 2 | 20.0% (4/20) | 42.9% (3/7) + 8 limited | n/a | 75.0% (3/4) + 4 limited | correlation, extraction |
| RP1-A04 | 2 | 43.5% (10/23) | 54.5% (6/11) + 1 limited | n/a | 66.7% (2/3) + 4 limited | correlation, extraction |
| RP1-A05 | 2 | 34.0% (16/47) | 100.0% (4/4) + 0 limited | n/a | 100.0% (3/3) + 9 limited | extraction |
| RP1-A06 | 2 | 25.0% (8/32) | 50.0% (2/4) + 0 limited | n/a | 66.7% (2/3) + 9 limited | correlation, extraction |
| RP1-A07 | 3 | 9.8% (4/41) | 16.7% (1/6) + 0 limited | n/a | 33.3% (1/3) + 7 limited | correlation, extraction |
| RP1-A08 | 2 | 24.1% (7/29) | 75.0% (3/4) + 0 limited | n/a | 33.3% (1/3) + 5 limited | correlation, extraction |
| RP1-A09 | 3 | 37.5% (9/24) | 33.3% (3/9) + 0 limited | n/a | 33.3% (5/15) + 3 limited | correlation, extraction |
| RP1-A10 | 1 | 20.0% (1/5) | 66.7% (2/3) + 0 limited | n/a | 100.0% (4/4) + 3 limited | correlation, extraction |
| RP1-A11 | 2 | 44.4% (8/18) | 16.7% (1/6) + 0 limited | n/a | 33.3% (2/6) + 3 limited | correlation, extraction |
| RP1-A12 | 2 | 32.0% (8/25) | 33.3% (2/6) + 0 limited | n/a | 66.7% (4/6) + 4 limited | correlation, extraction |

### RP1-A01
**Coverage:** shared_booking_ref, step_compatibility_selects_one, prose_discriminator, deterministic_match, past_step_noop.
**Result:** FAIL. Extraction 27.3% (6/22); correlation 85.7% (6/7) with 7 evidence-limited comparisons; action n/a; final-state 57.1% (4/7) with 2 evidence-limited comparisons. Miss classes: correlation, extraction.
**Observed email outcomes:** `rpa01-001@rp1.synthetic.test` → `pickup_advice` / `matched` / `job:RP1-JOB-0101`; `rpa01-002@rp1.synthetic.test` → `pickup_advice` / `superseded` / `job:RP1-JOB-0101`.
**DB citations:** `wf_event.id=6`; `wf_event.id=7`; `wf_instance.entity_key=job:RP1-JOB-0101`; `wf_instance.entity_key=job:RP1-JOB-0102`; `wf_event.external_id in arc wire Message-IDs`.
### RP1-A02
**Coverage:** unmatched_event, signal_with_start, late_heal, deterministic_rematch_sweep.
**Result:** FAIL. Extraction 36.4% (8/22); correlation 38.5% (5/13) with 3 evidence-limited comparisons; action n/a; final-state 75.0% (3/4) with 4 evidence-limited comparisons. Miss classes: correlation, extraction.
**Observed email outcomes:** `rpa02-001@rp1.synthetic.test` → `pickup_advice` / `unmatched` / `None`; `rpa02-002@rp1.synthetic.test` → `trucking_instruction` / `matched` / `RP1-JOB-0201`.
**DB citations:** `wf_event.id=9`; `wf_event.id=10`; `wf_instance.entity_key=job:RP1-JOB-0201`; `wf_instance.entity_key=RP1-JOB-0201`; `wf_event.external_id in arc wire Message-IDs`.
**Key-format note:** The deterministic create path stored bare `RP1-JOB-0201`, unlike the seeded `job:` form. Comparison normalizes the prefix, but the durable engine-side inconsistency remains a campaign finding.
### RP1-A03
**Coverage:** shared_booking_ref, two_plausible_matches, ambiguous_match, human_pick_required, no_auto_apply.
**Result:** FAIL. Extraction 20.0% (4/20); correlation 42.9% (3/7) with 8 evidence-limited comparisons; action n/a; final-state 75.0% (3/4) with 4 evidence-limited comparisons. Miss classes: correlation, extraction.
**Observed email outcomes:** `rpa03-001@rp1.synthetic.test` → `pickup_advice` / `ambiguous` / `None`; `rpa03-002@rp1.synthetic.test` → `None` / `routed_out` / `None`.
**DB citations:** `wf_event.id=13`; `wf_event.id=14`; `wf_instance.entity_key=job:RP1-JOB-0301`; `wf_instance.entity_key=job:RP1-JOB-0302`; `wf_event.external_id in arc wire Message-IDs`.
### RP1-A04
**Coverage:** out_of_order_event, future_step_buffer, compatible_step_advance, buffer_consumed_on_advance.
**Result:** FAIL. Extraction 43.5% (10/23); correlation 54.5% (6/11) with 1 evidence-limited comparisons; action n/a; final-state 66.7% (2/3) with 4 evidence-limited comparisons. Miss classes: correlation, extraction.
**Observed email outcomes:** `rpa04-001@rp1.synthetic.test` → `gate_in` / `buffered` / `job:RP1-JOB-0401`; `rpa04-002@rp1.synthetic.test` → `vgm_reply` / `matched` / `job:RP1-JOB-0401`.
**DB citations:** `wf_event.id=16`; `wf_event.id=17`; `wf_instance.entity_key=job:RP1-JOB-0401`; `wf_event.external_id in arc wire Message-IDs`.
### RP1-A05
**Coverage:** duplicate_message_id, forwarded_duplicate, content_dedupe, superseded_noop.
**Result:** FAIL. Extraction 34.0% (16/47); correlation 100.0% (4/4) with 0 evidence-limited comparisons; action n/a; final-state 100.0% (3/3) with 9 evidence-limited comparisons. Miss classes: extraction.
**Observed email outcomes:** `<rp1-a05-forwarded-0501@rp1.synthetic.test>` → `pickup_advice` / `superseded` / `job:RP1-JOB-0501`; `<rp1-a05-original-0501@rp1.synthetic.test>` → `pickup_advice` / `matched` / `job:RP1-JOB-0501`.
**DB citations:** `wf_event.id=20`; `wf_event.id=19`; `wf_instance.entity_key=job:RP1-JOB-0501`; `wf_event.external_id in arc wire Message-IDs`.
### RP1-A06
**Coverage:** mid_thread_correction, bill_of_lading_change, contradiction, needs_review_no_silent_overwrite.
**Result:** FAIL. Extraction 25.0% (8/32); correlation 50.0% (2/4) with 0 evidence-limited comparisons; action n/a; final-state 66.7% (2/3) with 9 evidence-limited comparisons. Miss classes: correlation, extraction.
**Observed email outcomes:** `<rp1-a06-correction-0601b@rp1.synthetic.test>` → `container_assigned` / `buffered` / `job:RP1-JOB-0601`; `<rp1-a06-initial-0601@rp1.synthetic.test>` → `pickup_advice` / `buffered` / `job:RP1-JOB-0601`.
**DB citations:** `wf_event.id=23`; `wf_event.id=22`; `wf_instance.entity_key=job:RP1-JOB-0601`; `wf_event.external_id in arc wire Message-IDs`.
### RP1-A07
**Coverage:** chase, angry_customer_escalation, urgency_extraction, tone_extraction, proposed_reply, no_autonomous_send.
**Result:** FAIL. Extraction 9.8% (4/41); correlation 16.7% (1/6) with 0 evidence-limited comparisons; action n/a; final-state 33.3% (1/3) with 7 evidence-limited comparisons. Miss classes: correlation, extraction.
**Observed email outcomes:** `<rp1-a07-carrier-0701@rp1.synthetic.test>` → `gate_in` / `buffered` / `job:RP1-JOB-0701`; `<rp1-a07-chase-0701@rp1.synthetic.test>` → `None` / `routed_out` / `None`; `<rp1-a07-customer-escalation-0701@rp1.synthetic.test>` → `None` / `routed_out` / `None`.
**DB citations:** `wf_event.id=25`; `wf_event.id=26`; `wf_event.id=27`; `wf_instance.entity_key=job:RP1-JOB-0701`; `wf_event.external_id in arc wire Message-IDs`.
### RP1-A08
**Coverage:** itos_state_mismatch, email_claim_gate_in, state_poll_not_gate_in, needs_review, exception_proposal.
**Result:** FAIL. Extraction 24.1% (7/29); correlation 75.0% (3/4) with 0 evidence-limited comparisons; action n/a; final-state 33.3% (1/3) with 5 evidence-limited comparisons. Miss classes: correlation, extraction.
**Observed email outcomes:** `<rp1-a08-carrier-gatein-0801@rp1.synthetic.test>` → `gate_in` / `matched` / `job:RP1-JOB-0801`; `<rp1-a08-forward-0801@rp1.synthetic.test>` → `gate_in` / `superseded` / `job:RP1-JOB-0801`.
**DB citations:** `wf_event.id=29`; `wf_event.id=30`; `wf_instance.entity_key=job:RP1-JOB-0801`; `wf_event.external_id in arc wire Message-IDs`; `wf_event.id=31`; `state_poll.entity_key=job:RP1-JOB-0801`.
### RP1-A09
**Coverage:** partial_info_dribble, cumulative_extraction, no_invention, buffered_future_event.
**Result:** FAIL. Extraction 37.5% (9/24); correlation 33.3% (3/9) with 0 evidence-limited comparisons; action n/a; final-state 33.3% (5/15) with 3 evidence-limited comparisons. Miss classes: correlation, extraction.
**Observed email outcomes:** `rp1-a09-1@rp1.synthetic.test` → `pickup_advice` / `buffered` / `job:RP1-JOB-0901`; `rp1-a09-2@rp1.synthetic.test` → `container_assigned` / `buffered` / `job:RP1-JOB-0901`; `rp1-a09-3@rp1.synthetic.test` → `vgm_reply` / `unmatched` / `None`.
**DB citations:** `wf_event.id=33`; `wf_event.id=34`; `wf_event.id=35`; `wf_instance.entity_key=job:RP1-JOB-0901`; `wf_event.external_id in arc wire Message-IDs`.
### RP1-A10
**Coverage:** wrong_recipient, noise_mail, routed_out, no_mutation.
**Result:** FAIL. Extraction 20.0% (1/5); correlation 66.7% (2/3) with 0 evidence-limited comparisons; action n/a; final-state 100.0% (4/4) with 3 evidence-limited comparisons. Miss classes: correlation, extraction.
**Observed email outcomes:** `rp1-a10-1@rp1.synthetic.test` → `None` / `routed_out` / `None`.
**DB citations:** `wf_event.id=36`; `wf_event.external_id in arc wire Message-IDs`.
### RP1-A11
**Coverage:** clean_happy_path, strong_unique_keys, create_job_proposal.
**Result:** FAIL. Extraction 44.4% (8/18); correlation 16.7% (1/6) with 0 evidence-limited comparisons; action n/a; final-state 33.3% (2/6) with 3 evidence-limited comparisons. Miss classes: correlation, extraction.
**Observed email outcomes:** `rp1-a11-1@rp1.synthetic.test` → `trucking_instruction` / `unmatched` / `None`; `rp1-a11-2@rp1.synthetic.test` → `pickup_advice` / `buffered` / `job:RP1-JOB-1101`.
**DB citations:** `wf_event.id=38`; `wf_event.id=39`; `wf_instance.entity_key=job:RP1-JOB-1101`; `wf_event.external_id in arc wire Message-IDs`.
### RP1-A12
**Coverage:** clean_happy_path, vgm_progress, gate_in_progress, strong_unique_keys, customer_update_proposal.
**Result:** FAIL. Extraction 32.0% (8/25); correlation 33.3% (2/6) with 0 evidence-limited comparisons; action n/a; final-state 66.7% (4/6) with 4 evidence-limited comparisons. Miss classes: correlation, extraction.
**Observed email outcomes:** `rp1-a12-1@rp1.synthetic.test` → `vgm_reply` / `matched` / `job:RP1-JOB-1201`; `rp1-a12-2@rp1.synthetic.test` → `gate_in` / `unmatched` / `None`.
**DB citations:** `wf_event.id=41`; `wf_event.id=42`; `wf_instance.entity_key=job:RP1-JOB-1201`; `wf_event.external_id in arc wire Message-IDs`.

## Campaign findings

1. **Stale release assembly — engine defect, repaired before measurement.** Staging selected one release while systemd and the editable finder loaded an older release. The permanent repair rebuilt a release-local environment and added interpreter/module-origin promotion checks. Literal SMTP → IMAP → `wf_event` verification passed after deploy.
2. **Live ingress lacked the LM extraction leg — missing contract leg, repaired before measurement.** Earlier harnesses injected already-extracted payloads. Live email ingress did not call the extractor. The new tenant-neutral ingress leg now invokes auxiliary-LM extraction using the registered template contract and then uses the existing validation and propose-lane semantics.
3. **Recorder defects — campaign harness defects, repaired without changing capability.** Empty classified payloads and seedless arcs originally aborted the measurement instead of recording misses. The helper now captures those outcomes and preserves DB citations; it does not affect engine decisions or answer keys.
4. **Gmail canonicalizes plus aliases — staging integration fact.** Display-name personas remained available, but the envelope/header sender collapsed to dorm1. A default-off, tenant-neutral self-ingress test capability was used in staging; the production echo-prevention default stayed unchanged.
5. **Board-global template resolution — platform gap, deliberately not built in-campaign.** Create matching and email-extraction contract selection resolve across the board rather than to a template-scoped/latest-per-slug target. Allied will run multiple workflows, so ambiguity is certain rather than hypothetical. This needs a post-campaign platform WB; changing it during the campaign would contaminate the measurement.
6. **Interpretation capability line — extraction and correlation misses.** `event_type` classification was 15/25 and primary correlation keys were often recovered, but typed payload retention was weak and correlation target/verdict frequently differed from the locked key. Comparison-only normalization of the `job:` entity prefix puts correlation accuracy at 38/80 (47.5%) without rewriting observed values. Most prefix differences are answer-key representation, but A02 exposed a real engine-side inconsistency: its deterministic create path stored `RP1-JOB-0201` while seeded instances use `job:RP1-JOB-0201`. The normalized score does not charge that formatting inconsistency, so it is disclosed here rather than hidden. Action/proposal comparisons are 75/75 evidence-limited because no durable approval row exposed them; no action-accuracy claim is made.
7. **A08 advanced against contradictory state evidence — high-severity decision result.** Email `wf_event.id=29` applied a `gate_in` claim to `job:RP1-JOB-0801`; the read-only ITOS probe `wf_event.id=31` then reported `not_gate_in` / `gate_in=false`. The final durable instance was nevertheless `advancing` at `invoice`, while the locked safety result required `needs_review` at `await_gatein` with invoice and customer confirmation held. This is the most consequential observable miss in the campaign.
8. **Final-state and probe scorer projection — measurement-plumbing repair.** The first score draft compared locked business vocabulary directly to generic `instances` rows and treated missing action records as failures. Cold review caught it. The scorer now projects only fields supported by durable rows, normalizes the storage-only entity prefix, and places absent fields in the evidence-limited bucket. The one probe now has four observable matches and one unobservable action claim.
9. **One locked key omitted a wrong extraction — key defect, not silently edited.** A01's second message placed pickup reference `PM-REL-8841` in `corr.container_no`, but that answer key did not compare `corr.container_no`. The locked key remains unchanged, so the reported container-number rate does not include this visible false positive. Record it as a key defect when interpreting the aggregate.

## Evidence contract

- Locked stories and answer keys: `arcs-01-04.json`, `arcs-05-08.json`, `arcs-09-12.json`.
- Lock integrity: the three files remain byte-identical to lock commit `9670003c7c08df9d2e640ca246927e883bd7aa99`; their SHA-256 values still match the original `LOCKED_FIXTURES` constants (`10f969…`, `ba62f2…`, `371ca9…`).
- Exact execution plan: `evidence/rp1-plan.json`.
- As-it-happens ingress journal: `evidence/rp1-live-journal.jsonl` (**230 rows**; 25 sends, 25 observations, two preflights, and the resumed-wave history).
- Durable DB observations: `evidence/rp1-observed.json`. Its top-level chronological capture log contains **84 records**, comprising **63 distinct full observation objects** and **48 distinct table+identity keys**; repeated preflight polls and pre/final snapshots are intentionally retained rather than counted as distinct facts.
- Mechanical scorer output: `evidence/rp1-score.json`.
- Campaign decision/finding log: `dive-records/rp.jsonl`.
- Preflight failure journals are preserved under `evidence/`; they were not rewritten into success.
- Citations name durable staging row identities such as `wf_event.id=N` and `wf_instance.entity_key=…`. Arc-scoped `_citations` bind each scorecard to its email and final snapshot; the top-level array is the chronological capture log and can contain the same identity at multiple phases. Candidate/reason/action details absent from the DB are marked evidence-limited rather than inferred. Missing fields inside durable `corr` and `vars` objects are observable non-retention and score as failures, not evidence limits.

## Conclusion

The chassis now carries a real email through extraction and workflow handling, but RP1 shows that this is not yet an Allied-ready interpretation layer. The next capability work should be demand-led from this result: first make template selection unambiguous for multiple workflows, then close the A08 contradictory-state safety failure and the specific observable extraction/correlation misses. Proposal behavior needs a durable observation surface before it can be judged as capability. The campaign itself is complete; rerunning it unchanged after those scoped changes is the clean regression test.
