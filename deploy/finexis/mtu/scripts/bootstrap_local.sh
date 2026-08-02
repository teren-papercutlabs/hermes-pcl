#!/usr/bin/env bash
# Retired direct MTU writer. The source-enforced gate and writer are one CLI.
set -euo pipefail
echo "ERROR: MTU_DEPLOY_EVAL_REFUSED: direct bootstrap is retired; use scripts/deploy_guarded.py" >&2
exit 2
