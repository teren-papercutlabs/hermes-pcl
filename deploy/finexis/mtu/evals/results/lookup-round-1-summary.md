# MTU S2 lookup replay — round 1

- Model: `gpt-5.6-luna`
- Runtime: production `GatewayRunner.replay` and WhatsApp adapter against a temporary, non-live `HERMES_HOME`
- Result: **13/13 draws passed** across four multi-turn cases
- Known product: exact `HSBC Term Protector` row returned example 16; category resolved through exact canonical `Term`; no failed taxonomy lookup
- Unknown product: four draws returned `found=false`, `match=none`, followed the Melody escalation cue, and exposed no internal lookup vocabulary or file number
- Typo canary: four draws treated `HSBC Term Protecter` as not found; no fuzzy substitution or example 16
- Unknown replacement path: four draws resolved exact taxonomy keys `Term` and `Whole Life`, returned no entry for exact path `Term -> Whole Life`, and escalated without reusing another path
- Evaluator: every declared expected label has a deterministic predicate; an undeclared predicate fails closed instead of being ignored
- Production home written: `false`
