# Review verdict — hermes-pcl 83f41abc9c59ef61dbe0128c8fa96b2c199c6658

VERDICT: BLOCK  (diff from 0f96b0198e; reviewed from fresh non-authoring session worker/d86f0dd9)
External review attempt (recorded): cb4335e3-14c3-4190-b77a-5482d7e1d9c0  (model=claude, verdict=fail)

## BLOCKING (verified empirically by this session)
- B1 (requirement 3 unmet): the "staged env unexpectedly contains token -> refuse" check is nested
  under `if matches:`, so it only fires when the CURRENT live .env already holds a token. Tested:
  current-without-token + staged-WITH-token -> rc=0, the Studio-sourced token is installed onto the
  host. Req 3 says an unexpectedly-staged key must refuse UNCONDITIONALLY; this is exactly the
  pre-migration (req-4) state, so it also punches a hole in "initial migration owned only by
  processing_activation_transaction.py." Fix: hoist the staged-env check above `if matches:`.
- B2 (test coverage gap, brief explicitly asked): the committed regression test string-slices the
  python and runs `python3 -c`, so it exercises NEITHER the ssh/heredoc shell path NOR any refusal/
  abort path — only the happy path. No test for: dup-current -> nonzero + .env untouched; token-in-
  staged -> nonzero; tokenless -> staged byte-identical. Add a test that runs the remote command
  string through bash against a temp root.

## SHOULD-FIX (verified; works today, latent)
- S1 (shell quoting): `<<'PY'` inside the single-quoted ssh arg resolves to an UNQUOTED `<<PY` on the
  remote (confirmed: resolved remote script shows `<<PY`). Correct today only because the python body
  has no `$`/backtick/`'`. A future `$`-bearing edit mangles silently on the client host. Escape the
  delimiter or switch to `ssh "$target" bash -s` on stdin.
- S2 (matcher divergence): fix uses `line.strip().startswith(f"{key}=")`; canonical parser uses
  `^(?:export\s+)?KEY=` + comment-skip. An `export KEY=` line is missed (not preserved, not counted
  dup); `matches[0]` is appended raw, so a leading-whitespace line passes the fix's lenient match but
  then fails bootstrap/verify's anchored `^KEY=` grep. Low real-world weight (system writes column-0,
  no-export), but align semantics.

## NON-BLOCKING
- N1: on refusal, `set -e` skips `rm -f /root/.pcl-secret-staging/christopher.env`, stranding fresh
  OPENAI/GEMINI plaintext on host (root:root 0700/0600). Add `trap ... EXIT`. New abort path this change introduces.

## VERIFIED CORRECT
- Value never crosses argv/stdout/Studio: only key NAME + file paths in argv; runs entirely on host;
  SystemExit prints key name only; the Studio-built staged file never contains the token.
- dup-current refuse precedes install -> a refusal leaves the live .env byte-identical (tested).
- Idempotent (exactly 1 token after repeated deploys); no-op when .env absent; no client-behavior broadening (deploy-script + test only).
- Target test file runs clean at target SHA: 14 passed (base 13 + the new one).
