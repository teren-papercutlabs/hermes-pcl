from __future__ import annotations

import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[2]
LEGACY_DEPLOY = ROOT / "deploy/tgg/christopher/scripts/deploy_runtime.sh"
SPEC = ROOT / "deploy/tgg/christopher/client-agent-deployment.yaml"
CANONICAL = "/usr/local/lib/tgg-christopher/standalone_release.py"


def test_legacy_deploy_refuses_before_any_external_command() -> None:
    # An empty PATH makes any attempted git/python/ssh/pcl/systemctl invocation
    # fail differently. The retired shell script uses only shell builtins.
    result = subprocess.run(
        ["/bin/bash", str(LEGACY_DEPLOY)],
        text=True,
        capture_output=True,
        env={"PATH": ""},
        check=False,
    )
    assert result.returncode == 64
    assert result.stdout == ""
    assert "retired" in result.stderr
    assert CANONICAL in result.stderr
    assert "not found" not in result.stderr


def test_christopher_spec_has_no_marshal_deploy_command() -> None:
    deployment = yaml.safe_load(SPEC.read_text())
    deploy = deployment["spec"]["deploy"]
    assert deploy["controller"] == "tgg-christopher-standalone-release"
    assert deploy["canonicalDeployCommand"] == f"{CANONICAL} apply"
    assert "deployCommand" not in deploy
    assert deploy["legacyMarshalDeployment"]["status"] == "retired"
    assert deploy["legacyMarshalDeployment"]["formerDeployCommand"] == (
        "deploy/tgg/christopher/scripts/deploy_runtime.sh"
    )
