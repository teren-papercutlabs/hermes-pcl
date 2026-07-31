# Release assembly

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
