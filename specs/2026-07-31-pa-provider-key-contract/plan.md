# PA per-client provider-key contract

## Outcome

Every client Hermes deployment assembles provider credentials from principal-supplied,
client-namespaced slots. A missing client slot or any attempt to source a generic fleet
slot stops before bundle/deploy mutation. Deployment verification proves that the
runtime process is using the values assembled from those named slots.

This slice changes source and operator guidance only. It does not deploy, restart, or
mutate a client host.

## Current state

- `deploy/tgg/christopher/scripts/prepare_host_secrets.sh` sources the canonical
  Studio secrets file and copies generic `OPENAI_API_KEY` and `GEMINI_API_KEY`.
- `verify_runtime.sh` checks only that generic runtime variable names exist.
- `client-agent-deployment.yaml` describes environment refs, but not their
  principal-supplied, per-client provenance.
- No reusable provider-key assembly primitive exists for the next PA client.

## Chosen shape

1. Add one client-agnostic provider-key contract helper under `deploy/pa/`.
   It reads the provider-key declaration in a `ClientAgentDeployment`, rejects
   generic/fleet source slots, assembles generic runtime variable names from the
   required client-namespaced canonical slots, and writes a mode-0600 provenance
   receipt containing slot names and secret digests (never values).
2. Make Christopher's `prepare_host_secrets.sh` a thin host/transport wrapper around
   that helper. It stages both the `.env` and its provenance receipt atomically.
3. Add a verification mode that compares the assembled env and the live service
   process environment against the provenance receipt. Any fleet-key drift or stale
   process key is red.
4. Declare the standing capability in `client-agent-deployment.yaml` and enforce its
   exact shape in `validate_deployment_spec.py`. This is the current Bedrock
   capability-contract carrier until a fleet registry/compiler exists.
5. Update `carbon-build` and `vps-provision` at the onboarding/deploy moments:
   obtain keys from Teren or Amelia, register client slots before first deploy, and
   never substitute fleet credentials.

The TGG declaration requires both `OPENAI_API_KEY_TGG` and
`GEMINI_API_KEY_TGG`. The source can land while the live host remains unchanged;
the later authorized deploy cannot proceed until the Gemini slot exists.

## Rejected shapes

- **Per-client bespoke shell checks only:** fixes Christopher but leaves the next
  client able to repeat the incident.
- **Generic runtime variable names as canonical sources:** preserves the failure
  class because provenance is erased before assembly.
- **Tail-only verification:** avoids storing a digest but cannot prove exact
  equality. The receipt stores SHA-256 digests in a root/runtime-readable file,
  never prints or commits them, and verification emits only pass/fail plus slot names.
- **Verify only the env file:** misses a service that was not restarted after key
  assembly. The live process environment is part of the check.

## Verification

1. Unit tests exercise:
   - valid client-scoped assembly;
   - missing slot refusal with the principal-onboarding instruction;
   - generic/fleet source-slot refusal;
   - malformed client/slot declarations;
   - env-file drift and live-process drift;
   - output/provenance files contain no slot aliases or values in command output.
2. Deployment-spec validation accepts the new TGG capability declaration and rejects
   mutation of its client, source slots, runtime targets, supplier, or provenance path.
3. Script-level tests assert Christopher preparation and verification invoke the
   shared contract and that shared Gemini aliases are absent.
4. Run the focused deploy tests, then the repository test gate required by the PR.
5. Inspect the pushed diff and obtain the single required opposite-provider review
   before PR submission.

## Rollback mechanics

- Before any later deploy, rollback is `git revert` of the source commits; no client
  state has changed and data loss is zero.
- After a later authorized deploy, rollback is the existing `pcl pa-agent rollback`
  transaction to the prior immutable bundle. The provider-key receipt and `.env` move
  together in the bundle/bootstrap transaction.
- Blast boundary: PA deploy assembly and verification source, plus the two onboarding
  skills. No current client host is touched in this WB.

## Scaffolding co-deliverable

| new state | scaffolding | writer | landing path | completion evidence |
|---|---|---|---|---|
| reusable provider-key assembly/verification contract | module CLI help, deployment declaration, tests | Edna worker | `deploy/pa/provider_key_contract.py`, `deploy/tgg/christopher/client-agent-deployment.yaml`, `tests/deploy/test_pa_provider_key_contract.py` | focused tests pass and deployment spec rejects drift |
| PA onboarding obligation | existing workflow skills updated in place | Edna worker | `skills/carbon-build/SKILL.md`, `skills/vps-provision/SKILL.md` in shared-agent-memory | skill text names principal-supplied client slots before first deploy |

Both rows are explicit WB DoD co-deliverables in the criterion beginning
“Scaffolding co-deliverables land in the same slice”; neither is follow-up work.

No `dev.*` schema is created or changed.

## Standing guidance

- `carbon-build` describes PA agent build/onboarding.
- `vps-provision` describes new client host provisioning and first-deploy readiness.
- `client-agent-deployment.yaml` is the executable client capability declaration.

## Design passport

- passport: `SS-PASSPORT-2026-06-22-F3A9D1`
- gate0 source: current `prepare_host_secrets.sh`, `verify_runtime.sh`, and TGG deployment spec | implicated surface: deploy assembly copies generic fleet provider keys and verification checks presence only | known-vs-inferred: source-read and incident-grounded
- gate1 symptom: TGG OpenAI usage was charged to the fleet key | immediate cause: generic `OPENAI_API_KEY` was copied into the client runtime | root mechanism: PA deployment had no client-scoped provider-key contract or provenance check | recurrence: category | audit_wb: this WB covers the only current Hermes client deployment plus the reusable future-client path
- gate2 mechanism: source-enforcement plus actor-surface recognition | non-loading test: assembly and verification refuse without relying on the operator reading prose; skills supply the onboarding cue before the source gate
- gate3 primitive: extend `ClientAgentDeployment`, `prepare_host_secrets`, and runtime verification with one shared helper | contract answer: these are the existing assembly/deploy-gate surfaces; a parallel secret system is unnecessary
- gate4 result standard: client deploys can use only principal-supplied client slots | event proof: focused assembly and drift tests | state proof: every deploy verification compares receipt, env, and live process | thing: client-agent deployment | activation: pre-deploy assembly and post-deploy verify | red owner: edna | mutates/consumer: source deployment contract consumed by `pcl pa-agent` wrappers and PA builders | rollback/blast: git revert before deploy; pa-agent rollback after a later authorized deploy; no host mutation in this WB
- gate5 advisory: target scripts, deployment spec, validator, manifests, deploy tests, carbon-build, vps-provision | findings: current spec already carries env refs and verify hooks, so extend rather than create a parallel contract | recommended action: land the capability in the deployment spec and shared helper | plan changed by gate5: no
- review passes: ruth=skipped:source-enforced bounded design; cowboy=run:cheapest compliant path and missing-slot failure checked; codex=run:implementation and tests in this worker
- schema-lens: n/a — no schema/data-model mutation
- classes applied: runtime/config, enforcement/hook, public/client, state-writing/canonical-source, scaffolding/rule
- refs loaded: fail-open-vs-fail-closed, check-resulting-state, consumer-altitude-verification, cue-at-action-surface, principal-gate-vs-iom-dra-review
- rejected shape: per-client shell-only checks without a reusable deployment contract
- delta count: 0
