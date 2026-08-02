from __future__ import annotations

from pathlib import Path

import pytest

from agent.pa_constitution import PAJobBrief
from tools.pa_tools import fetch_knowledge, lookup_reference


def _brief(*entries: str) -> PAJobBrief:
    return PAJobBrief(
        job_type="test",
        title="Test",
        purpose="Test knowledge.",
        instructions=(),
        knowledge=entries,
    )


def _config(limit: int = 100_000) -> dict:
    return {"pa": {"enabled": True, "knowledge_max_bytes": limit}}


def test_fetch_is_manifest_bound_and_size_guarded(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "guide.md").write_text("approved guidance", encoding="utf-8")
    assert fetch_knowledge(
        "guide.md", config=_config(), brief=_brief("guide.md"), hermes_home=tmp_path
    )["content"] == "approved guidance"

    with pytest.raises(ValueError, match="not declared"):
        fetch_knowledge(
            "other.md", config=_config(), brief=_brief("guide.md"), hermes_home=tmp_path
        )
    with pytest.raises(ValueError, match="size limit"):
        fetch_knowledge(
            "guide.md", config=_config(3), brief=_brief("guide.md"), hermes_home=tmp_path
        )


def test_fetch_refuses_manifest_traversal(tmp_path: Path) -> None:
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "outside.md").write_text("outside", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot traverse"):
        fetch_knowledge(
            "../outside.md",
            config=_config(),
            brief=_brief("../outside.md"),
            hermes_home=tmp_path,
        )


def test_fetch_refuses_whole_structured_reference(tmp_path: Path) -> None:
    root = tmp_path / "knowledge" / "reference"
    root.mkdir(parents=True)
    (root / "products.yaml").write_text(
        "kind: keyed-reference\n"
        "escalation_cue: Escalate.\n"
        "entries:\n  - {key: Alpha, value: 1}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="pa_reference_lookup"):
        fetch_knowledge(
            "reference/products.yaml",
            config=_config(),
            brief=_brief("reference/products.yaml"),
            hermes_home=tmp_path,
        )


def test_lookup_returns_exact_entry_or_nothing_with_cue(tmp_path: Path) -> None:
    root = tmp_path / "knowledge" / "reference"
    root.mkdir(parents=True)
    (root / "products.yaml").write_text(
        "kind: keyed-reference\n"
        "escalation_cue: Escalate the unresolved key to the configured owner.\n"
        "entries:\n"
        "  - key: Alpha Plan\n"
        "    value: 16\n",
        encoding="utf-8",
    )
    brief = _brief("reference/products.yaml")

    found = lookup_reference(
        "reference/products.yaml",
        "Alpha Plan",
        config=_config(),
        brief=brief,
        hermes_home=tmp_path,
    )
    assert found == {
        "file": "reference/products.yaml",
        "key": "Alpha Plan",
        "found": True,
        "entry": {"key": "Alpha Plan", "value": 16},
        "escalation_cue": "Escalate the unresolved key to the configured owner.",
        "match": "exact",
    }

    for unknown in ("Alpha Plam", "alpha plan", "Alpha Plan "):
        missing = lookup_reference(
            "reference/products.yaml",
            unknown,
            config=_config(),
            brief=brief,
            hermes_home=tmp_path,
        )
        assert missing["found"] is False
        assert missing["entry"] is None
        assert missing["match"] == "none"
        assert missing["escalation_cue"].startswith("Escalate")


def test_lookup_rejects_duplicate_keys(tmp_path: Path) -> None:
    root = tmp_path / "knowledge" / "reference"
    root.mkdir(parents=True)
    (root / "duplicate.yaml").write_text(
        "kind: keyed-reference\n"
        "escalation_cue: Escalate.\n"
        "entries:\n"
        "  - {key: A, value: 1}\n"
        "  - {key: A, value: 2}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate key"):
        lookup_reference(
            "reference/duplicate.yaml",
            "A",
            config=_config(),
            brief=_brief("reference/duplicate.yaml"),
            hermes_home=tmp_path,
        )
