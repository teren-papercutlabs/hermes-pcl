from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "deploy" / "tgg" / "christopher" / "pa-agent.hermes.manifest.json"


def test_pa97_runtime_components_are_in_christopher_deploy_manifest() -> None:
    included = set(json.loads(MANIFEST.read_text(encoding="utf-8"))["include"])

    assert "hermes_cli/ephemeral_session.py" in included
    assert "scripts/tgg_continuous_reviewer.py" in included
