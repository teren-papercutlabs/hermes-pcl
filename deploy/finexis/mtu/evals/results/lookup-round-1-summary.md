# MTU S2 lookup replay round 1

Model: `gpt-5.6-luna`  
Path: `GatewayRunner.replay` through the real WhatsApp adapter in an isolated temporary `HERMES_HOME`  
Result: **13/13 draws passed** across four pre-build multi-turn cases.  
Production home written: **no**.

The known product case returned the exact `HSBC Term Protector` row with reference example 16. Unknown product, typo, and unknown replacement-path canaries returned `found=false`, `entry=null`, `match=none`, carried the configured escalation cue, and escalated to Melody without a nearest match.

Full responses and tool rows: `lookup-round-1.json`.
