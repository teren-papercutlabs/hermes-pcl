#!/usr/bin/env bash
# Retained only so historical receipts and old automation fail safely.
#
# Christopher releases are exclusively activated by the fixed, host-owned
# standalone executor. In particular, do not add validation, networking, or
# cleanup here: this command must refuse before it can touch any state.
set -euo pipefail

printf '%s\n' \
  'Christopher legacy deploy_runtime.sh is retired; use /usr/local/lib/tgg-christopher/standalone_release.py.' >&2
exit 64
