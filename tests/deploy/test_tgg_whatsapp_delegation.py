from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
CONSTITUTION = ROOT / "deploy" / "tgg" / "christopher" / "christopher_tgg_constitution.yaml"


def test_management_whatsapp_evidence_delegation_is_bounded_and_receipted():
    constitution = yaml.safe_load(CONSTITUTION.read_text(encoding="utf-8"))
    management = constitution["job_briefs"]["tgg_management"]
    ingest = constitution["job_briefs"]["tgg_ops_ingest"]
    instructions = "\n".join(management["instructions"])

    assert "delegation" in management["enabled_toolsets"]
    assert "delegation" not in ingest["enabled_toolsets"]
    assert "WhatsApp source evidence" in instructions
    assert "Ordinary database, report, and structured-source questions stay inline" in instructions
    assert "one bounded child per case" in instructions
    assert "at most 25 cases per dispatch" in instructions
    assert "every child reaches a terminal result" in instructions
    assert "failed or blocked children" in instructions
