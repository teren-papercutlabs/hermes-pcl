# PA provider-key capability contract

Every `ClientAgentDeployment` declares provider-key provenance under
`spec.capabilities.providerKeys`:

```yaml
capabilities:
  providerKeys:
    mode: per-client-principal-supplied
    suppliedBy: principal
    canonicalStore: ~/.marshal/secrets.env
    provenancePath: /absolute/runtime/provider-key-provenance.json
    providers:
      - provider: openai
        runtimeEnv: OPENAI_API_KEY
        sourceSlot: OPENAI_API_KEY_<CLIENT>
```

`sourceSlot` must equal `<runtimeEnv>_<CLIENT>`, where `<CLIENT>` is the
deployment metadata client slug normalized to uppercase underscores. Generic
provider names, shared fleet aliases, and another client's slot are invalid.

Before a first deploy, Teren or Amelia supplies each client provider key. The
deploying agent registers it in `~/.marshal/secrets.env` under the declared
client slot before first use. `deploy/pa/provider_key_contract.py assemble`
then writes the runtime's generic provider variables plus a mode-`0600`
provenance receipt. It refuses assembly if any client slot is missing; it
never consults a generic fleet slot.

`deploy/pa/provider_key_contract.py verify` checks the declaration, receipt,
materialized environment, and optionally the running process environment.
Deploy verification must call it with the live process environment before
the deployment flips. A missing receipt, mismatched declared slot,
materialized-value drift, or live-process drift is a hard failure.

Provider-key rotation is one transaction: register the replacement in the
same client slot, assemble both runtime env and provenance, deploy through the
client's authorized transaction, restart through that transaction, and run
the full live-process verification. Never print key values.
