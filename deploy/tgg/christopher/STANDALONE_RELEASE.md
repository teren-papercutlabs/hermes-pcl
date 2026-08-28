# Christopher standalone release

This is the sole small release path for the Christopher host. It is deliberately
not a PA deployment framework: it activates one immutable Hermes runtime and
one immutable Christopher capability under `/opt/tgg-christopher`.

It exists because two whole-runtime paths restarted Christopher during a live
nightly run. The consumer and the release executor share
`/opt/tgg-christopher/release-activity.lock`: a consumer takes a shared lock
before claiming inbox work; the executor takes an exclusive non-blocking lock
and refuses if Christopher is active. It never forces a release.

## Build and apply

Build on the release machine from a clean runtime checkout and an immutable
capability directory:

```sh
python3 deploy/tgg/christopher/scripts/standalone_release.py prepare \
  --runtime /path/to/hermes-runtime \
  --runtime-manifest /path/to/hermes-runtime/deploy/tgg/christopher/pa-agent.hermes.manifest.json \
  --capability /path/to/christopher-capability-release \
  --provider openai-codex --model <model> --reasoning-effort medium \
  --out /secure/release-bundles/20260820-r1
```

Transfer that one directory to the target, then run the fixed executor from the
staged runtime (or a root-owned copy installed outside `/opt/tgg-christopher`):

```sh
python3 /usr/local/lib/tgg-christopher/standalone_release.py apply \
  --bundle /secure/release-bundles/20260820-r1
```

`prepare` copies only files enumerated by the existing runtime manifest and
requires the capability manifest to enumerate its own files and hashes; it does
not archive a worktree, `.git`, or dependency cache. It also requires the
capability's declared `runtime.hermes_commit` to match the staged runtime (a
Git prefix is accepted). Before creating the bundle it requires a clean Git
worktree, fetches the pinned canonical repository's protected `main`, and
requires the runtime commit to equal that fresh head. The bundle records the
repository URL, protected ref, head and verification time. Provider/model/reasoning are runtime-release inputs,
not capability authority. The executor verifies both payload hashes before it changes a pointer, switches
the `runtime/current` and `capability/current` symlinks, atomically installs
the runtime-owned `christopher-tgg-hermes.service`, reloads systemd, restarts Christopher
once, runs focused identity/service/config/gate/timer/inbox checks, and writes a
compact receipt below `/opt/tgg-christopher/transactions/`. A failure restores
the prior pointers and restarts once.

At apply time the fixed executor independently resolves protected `main` from
the host-pinned repository URL. It refuses if `main` advanced or if the bundle,
prepared head and runtime commit do not all match. An already prepared emergency
bundle can bypass this apply-side equality check only through an explicit,
audited root/operator invocation:

```sh
python3 /usr/local/lib/tgg-christopher/standalone_release.py apply \
  --break-glass --reason "plain operational reason" \
  --bundle /secure/release-bundles/emergency
```

The receipt records the actor, reason, runtime commit, observed protected-main
head and a repository-reconciliation obligation. Bundle content cannot select
the canonical repository or enable break glass.

Rollback is explicit and also refuses while work is processing:

```sh
python3 /usr/local/lib/tgg-christopher/standalone_release.py rollback \
  --receipt /opt/tgg-christopher/transactions/<id>/receipt.json
```

## Human-resolution notice outbox

The durable consumer can also drain the typed Systems document outbox. This is
off by default: do not set only one of these values. When PA-75 arms its test
management canary, the host-owned `.env` supplies all three:

```sh
TGG_MANAGEMENT_DOCUMENT_API_URL=https://systems.papercut-labs.com
TGG_MANAGEMENT_DOCUMENT_CHAT_ID=<approved-management-whatsapp-jid>
CHRISTOPHER_TGG_PS_SERVICE_TOKEN=<existing-agent-service-token>
```

`TGG_MANAGEMENT_DOCUMENT_TOKEN_ENV` is optional and defaults to
`CHRISTOPHER_TGG_PS_SERVICE_TOKEN`. The destination must already be a
WhatsApp `tgg_management` selector. The consumer polls Systems' exclusive
`(created_at,id)` document-entry cursor, makes one at-most-once bridge attempt
per initial entry, and stores that result in its existing `reply_deliveries`
ledger. It does not append a fabricated WhatsApp capture event or source
evidence row. Leave this environment absent until the capture-only canary is
ready; a runtime rollback disables new notices by removing both URL and chat
variables and restarting Christopher during an idle window.

## One-time migration (do not run during active processing)

1. Install the fixed executor as root-owned
   `/usr/local/lib/tgg-christopher/standalone_release.py`.
2. Copy the exact current runtime to
   `/opt/tgg-christopher/runtime/releases/<current-commit>` and write its
   `.git-revision`; copy the current capability release to
   `$HERMES_HOME/runtime/capabilities/christopher-tgg/releases/<release-id>`;
   `/opt/tgg-christopher/capability/current` points to that same approved
   directory and does not duplicate the capability bytes.
3. Create the three pointers: `runtime/current`, `capability/current`, and
   `$HERMES_HOME/runtime/capabilities/christopher-tgg/current` (the latter
   targets `capability/current`).
4. Install the supplied systemd unit and run `daemon-reload`; it genuinely
   launches `/opt/tgg-christopher/runtime/current`, not the old app checkout.
5. Perform one no-behaviour-change activation then receipt-based rollback on an
   idle host. Existing release IDs are accepted only when their complete file
   inventories match; this makes the drill possible without overwriting a
   release.
   During this first migration the old consumer does not yet hold the shared
   activity lock, so run only when the inbox has zero processing rows; the
   executor's exclusive lock plus that zero-row check are the first-step guard.
6. Disable Christopher-only `deploy_runtime.sh`,
   `direct-runtime-transaction.py`, and direct `install-release --restart-service`
   usage. Do not remove Marshal or PA-wide paths in this migration; only remove
   their Christopher entry points after the drill has a receipt.

Broad report checks, registries, artifact signing, multi-host rollout and a new
release service are intentionally out of scope. A new control should be added
only after an observed release failure that this path cannot contain.
