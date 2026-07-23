# TGG source-ref binding sandbox evidence

Both runs used the repository fixture-only consumer against the generated `gpt-5.6-luna` slot. The operator API was a loopback stub; reports record `client_mutation_requests=0` and `external_outbound_sent=0`.

## sandbox-1

- messages processed: 3
- persisted observation path: `/api/operator/cases/AM%2FJOB%2F2607%2F1032/observations`
- persisted `fields.source_refs`: `["fx1-instruction", "fx1-photo-am"]`
- excluded same-turn ref(s): `["fx1-unrelated"]`
- client mutations: `0`
- external sends: `0`

## sandbox-2

- messages processed: 3
- persisted observation path: `/api/operator/cases/SK%2FJOB%2F2607%2F2035/observations`
- persisted `fields.source_refs`: `["fx2-instruction", "fx2-photo-sk"]`
- excluded same-turn ref(s): `["fx2-photo-other"]`
- client mutations: `0`
- external sends: `0`
