# Release assembly

## Whole-tree bundle (phase-one, non-default)

`build_tree_bundle.py` creates a tar archive from an exact, full commit id with
`git archive`. It then reopens that archive and generates a deterministic JSON
receipt containing the complete file inventory, modes, sizes, and SHA-256
fingerprints. The working tree is never an input.

```bash
commit="$(git rev-parse HEAD)"
python3 scripts/deploy/build_tree_bundle.py \
  --repo . \
  --commit "$commit" \
  --archive /path/to/hermes-tree.tar \
  --receipt /path/to/hermes-tree.receipt.json
```

This is deliberately beside the existing curated-list PA bundler. It is not
wired into `deploy_runtime.sh` or any default deploy path; phase one is for
same-commit parity measurement only.

## Release installation

`assemble_release.py` is the release-assembly boundary for Python services that
select immutable source releases through a `current` symlink. It never copies or
symlinks a virtual environment from an earlier release. It is a POSIX deployment
tool and requires Python 3.11.4 or newer for safe tar extraction.

For `pa-workflow-dev`, package the verified source commit with `git archive`,
copy the archive and this script to the staging host, then run:

```bash
python3 assemble_release.py \
  --archive /path/to/hermes.tar \
  --app-root /home/pa-staging/apps/hermes-pcl \
  --release-id <verified-commit-sha> \
  --python python3 \
  --extra messaging \
  --module hermes_cli.pa_credentials \
  --module gateway.run \
  --entrypoint hermes
```

The command creates the virtual environment at its final absolute path,
installs the selected source into it, and refuses promotion unless the required
entrypoint and module origins all point into that release. On failure it removes
the candidate it created and leaves `current` unchanged. On success it prints a
stdout-only JSON receipt including the previous release path and source-archive
SHA-256 for rollback and provenance. Installer progress is written to stderr.

Restart the staging unit only after a successful receipt. Rollback is an atomic
repoint of `current` to the receipt's `previous` path followed by another
staging-unit restart. Workflow data is outside the release tree.
