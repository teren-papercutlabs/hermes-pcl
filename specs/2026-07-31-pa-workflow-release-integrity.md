# PA Workflow Staging Release Integrity

## Scope

Restore release assembly for `pa-workflow-dev` only. This change must not touch
any live PA deployment.

## Root cause

The staging gateway runs the console script at
`current/.venv/bin/hermes`. Releases were assembled with ad hoc shell commands
instead of a source-owned release primitive:

1. One deployment copied the previous release, including `.venv`, into the new
   release and ran the copied interpreter to perform an editable install.
2. A later deployment explicitly symlinked the new release's `.venv` to an
   older release.

Python virtual environments are not relocatable. Console-script shebangs and
editable-install finder files contain absolute paths, so copying or symlinking
the environment retained the previous release as the actual interpreter and
module source.

Live staging evidence on 2026-07-31 showed the three generations at once:

- `current` selected release `ffc5a396987bf9d0f20d5cb9f70ccc1b19796ea9`.
- `current/.venv` resolved to release
  `3da526d027b26ae1f26c91316a32c6f10488748f`.
- `current/.venv/bin/hermes` named the interpreter in release
  `b8264f88314314baea2543842f1f8ef7b6c161a5`.

The selected source contained `hermes_cli.pa_credentials`, but the gateway
started through the stale interpreter and logged `No module named
'hermes_cli.pa_credentials'`. A synthetic email then reached the conversational
authorization path without producing a workflow event.

## Design passport

- Passport: `SS-PASSPORT-2026-06-22-F3A9D1`
- Classification: `design-bearing`
- Existing primitive: none in this repository. The prior staging deployments
  were shell transcripts, not a checked-in deploy mechanism. The older
  Pip-specific deployment controller uses a shared environment and a different
  filesystem contract, so extending it would preserve the defect.
- Chosen layer: a source-owned Python release assembler in `scripts/deploy/`.
  It owns extraction, creation of a brand-new release-local virtual
  environment, installation, verification, and atomic promotion.
- Rejected alternative: rewrite only the systemd command to call
  `python -m ...`. That bypasses a stale console-script shebang but leaves the
  stale editable import map intact.
- Rejected alternative: repair shebangs after copying an environment. Other
  absolute paths remain and the environment is still non-relocatable.
- Activation: every candidate is checked before `current` can move. The check
  verifies each required entrypoint's interpreter and each required module's
  origin against the candidate release.
- Failure owner: the deploying PA operator. A failed candidate never changes
  `current`; the assembler removes the partial candidate it owns.
- Rollback: atomically repoint `current` to the recorded previous release and
  restart the staging unit. Workflow data lives outside the release tree, so
  rollback has no data-loss boundary.
- Blast boundary: the selected staging application root and its systemd unit.
  No live PA host or client data is in scope.
- Independent review: exact worker head, opposite provider, before PR
  submission. Repository checks and the native merge queue own integration.

## Success evidence

The automated regression creates two releases at different absolute paths and
requires the second release's console script and imported modules to resolve
only within the second release. Staging acceptance additionally requires:

1. the selected release, virtual environment, console-script interpreter, and
   module origins to agree;
2. the credential watcher to start without a missing-module error; and
3. a new SMTP-to-IMAP message to create a `wf_event` before the unauthorized
   conversational response.
