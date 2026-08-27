# This directory is DEPLOY/PROVISIONING. The activation ceremony's control flow is NOT here.

If you are looking for **how the TGG activation ceremony works** — how it mints check
passes, retires checks, flips processing, or releases the trust rung — **you are in the
wrong repository.** Stop grepping here.

## Where it actually lives

```
~/pcl/tgg-agent/runtime/tgg-capture-whatsapp-bridge/
```

Different repo (`tgg-agent`, not `hermes-pcl`). The ceremony's control flow is
`activation-orchestrator.js` in that directory, alongside `allowlist.js`,
`graduation.js`, `guarded-transport.js`, `mention-filter.js`, `outbound-audit.js`,
`controller-grant.js`, and `bridge.js`.

## The check lifecycle, by file and line

All in `activation-orchestrator.js`:

| what | line | call |
|---|---|---|
| pre-live check identity | 1 | `export const PRE_LIVE_CHECK = 'pa.tgg.christopher.runtime_invariants'` |
| post-flip check identity | 2 | `export const POST_FLIP_CHECK = 'pa.tgg.christopher.post_flip_outbound_invariants'` |
| pre-live pass minted | 54 | `const preRun = requirePass(...)` |
| detector armed | 76 | `const armedRun = requirePass(...)` |
| **pre-live check RETIRED** | **87** | `const retired = await ops.retirePreCheck({ preRunId: preRun.runId })` |
| first post-flip pass | 112 | `requirePass(...)` |
| rung released | 148 | `requirePass(...)` |
| **post-flip check retired** | **242 AND 478** | `const evidence = await ops.retirePostCheck()` — TWO call sites, not one |

Line numbers drift. The **call names** (`retirePreCheck`, `retirePostCheck`,
`requirePass`) are the durable handles — grep those, not the numbers.

## What IS in this directory

Deploy and provisioning only: `activate_processing.py` (the go-live *gate*, which
*consumes* the checks above but does not implement their lifecycle),
`apply_engine_slot.py`, `bootstrap_runtime.sh`, `build_runtime_slots.py`,
`verify_runtime.sh`, and friends.

Capability activation does not own the live host configuration. Bootstrap and
the service `ExecStartPre` invoke `apply_engine_slot.py --preserve-host-config`:
the loader validates the capability's bounded plugin/tool contract against the
live `config.yaml`, materializes the capability constitution and plugin links,
and proves the config bytes unchanged. Only the separate explicit engine/provider
switch commands may rewrite the runtime fields they administer.

The distinction that matters: **`activate_processing.py` READS check state; it does not
retire checks.** Reading it and finding no retirement is not evidence that the ceremony
never retires — it is evidence you are looking at the consumer, not the implementation.

## Why this file exists

**2026-07-21, hours before a client demo, this exact confusion cost a ceremony round.**

An agent searched *this* directory for "does the ceremony retire the pre-live
invariant", found nothing, and reported it — with clean positive and negative controls.
The controls were sound. They proved the grep discriminated correctly *inside a
directory that did not contain the answer.* The retirement was at
`activation-orchestrator.js:87`, in the other repo, in a file listing the agent had
already pulled earlier that morning.

That false absence became a design-bug framing, an unnecessary principal authorization,
a check retired by hand, a refused activation round, and a sticky disabled state.

**A positive control proves your instrument works. It says nothing about whether your
scope was right — and scope correctness has no control.** The only defence is
establishing where a thing would actually live *before* concluding it does not exist.
This file is that establishment, written down so the next person gets it free.
