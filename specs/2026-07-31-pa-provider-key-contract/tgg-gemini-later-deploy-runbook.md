# TGG Gemini client-key cutover — later authorized transaction

Status at this source change: OpenAI is already on `OPENAI_API_KEY_TGG`.
Gemini remains on the shared PA key. This runbook is intentionally not
executed by WB `a6afdf68`; the TGG deploy freeze requires a later authorized
PA-agent transaction.

1. Teren or Amelia supplies the TGG Gemini provider key through an approved
   secret-intake surface.
2. As the immediate next action, register the value in
   `~/.marshal/secrets.env` under `GEMINI_API_KEY_TGG` without putting the
   value in argv, logs, chat, or git. Confirm the slot name exists without
   printing its value. Confirm `OPENAI_API_KEY_TGG` remains registered.
3. Obtain the explicit release of the TGG deploy freeze for this transaction.
4. From a clean checkout at the exact merged `origin/main`, run:

   ```bash
   deploy/tgg/christopher/scripts/deploy_runtime.sh
   ```

   The wrapper validates the Bedrock provider-key capability, refuses if
   either TGG slot is missing, assembles `.env` plus root-owned provenance,
   dry-runs the immutable PA-agent transaction, deploys, restarts through that
   transaction, and runs `pcl pa-agent verify`.
5. Require the full verifier to pass its exact comparisons across declared
   source slots, the assembled environment, provenance receipt, and the live
   process environment. Confirm the service and health timer are active and
   the controlled output-quality evaluation passes.
6. Inspect only provider slot names and verification status. Never print
   values or receipt digests. The runtime environment must contain
   `OPENAI_API_KEY` and `GEMINI_API_KEY`; it must not contain
   `GEMINI_API_KEY_PCL_PA_SHARED`.

Rollback uses the transaction receipt emitted by `deploy_runtime.sh` with
`pcl pa-agent rollback --transaction <receipt>`. Roll back runtime and source
as one unit; do not hand-edit the live `.env` or provenance receipt.
