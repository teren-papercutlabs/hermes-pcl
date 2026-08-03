import json
import re
from pathlib import Path

import pytest
import yaml

from hermes_cli.pa_compose import (
    PaComposeError,
    compose_pa_constitution,
    load_typed_sources,
    sync_pa_knowledge,
)


ROOT = Path(__file__).resolve().parents[2]
MTU = ROOT / "deploy" / "finexis" / "mtu"


def _test_constitution(knowledge: str) -> str:
    return (
        "id: test\n"
        "agent_name: Test\n"
        "identity:\n  role: test\n"
        "client:\n  name: Test\n"
        "job_briefs:\n"
        "  test:\n"
        "    title: Test\n"
        "    purpose: Test knowledge sync.\n"
        "    knowledge:\n"
        f"      - {knowledge}\n"
    )


def _normalise_yaml(path: Path):
    return yaml.safe_load(path.read_text())


def test_loader_reads_all_typed_directories_with_unique_sequence():
    sources = load_typed_sources(MTU)
    assert {source.source_type for source in sources} == {
        "rules", "compliance", "reference", "templates", "job-briefs"
    }
    assert len(sources) == 36
    assert [source.sequence for source in sources] == sorted(
        source.sequence for source in sources
    )
    required = {"approved_by", "approved_date", "ruling_ref", "status"}
    assert all(required <= source.provenance.keys() for source in sources)


def test_dated_ruling_fragments_carry_dates_in_provenance_or_are_unverified():
    for source in load_typed_sources(MTU):
        body_dates = set(re.findall(r"20\d\d-\d\d-\d\d", source.body.decode()))
        if not body_dates or source.provenance["status"] == "unverified":
            continue
        approved_dates = source.provenance["approved_date"]
        if isinstance(approved_dates, str):
            approved_dates = [approved_dates]
        assert body_dates <= set(approved_dates), source.relative_path


def test_compose_refuses_unverified_compliance_by_default(tmp_path):
    with pytest.raises(PaComposeError, match="unverified compliance artifacts refuse"):
        compose_pa_constitution(
            MTU, tmp_path / "constitution.yaml", tmp_path / "manifest.json"
        )
    assert not (tmp_path / "constitution.yaml").exists()
    assert not (tmp_path / "manifest.json").exists()


def test_allow_unverified_composes_parity_and_records_escape(tmp_path):
    output = tmp_path / "constitution.yaml"
    manifest_path = tmp_path / "manifest.json"
    result = compose_pa_constitution(MTU, output, manifest_path, allow_unverified=True)
    committed = MTU / "mtu_constitution.yaml"
    assert _normalise_yaml(output) == _normalise_yaml(committed)
    assert output.read_bytes() == committed.read_bytes()
    manifest = json.loads(manifest_path.read_text())
    assert manifest == result
    assert manifest["allow_unverified"] is True
    assert manifest["unverified_compliance"]
    assert manifest["source_count"] == 36
    assert manifest["composed_source_count"] == 26
    excluded = [
        source["path"]
        for source in manifest["sources"]
        if not source["included_in_constitution"]
    ]
    assert excluded == [
        "compliance/010-sustainability-enforcement.yaml",
        "reference/061-ekyc-directory-map.yaml",
        "reference/062-approved-products.yaml",
        "reference/063-product-insurers.yaml",
        "reference/064-replacement-taxonomy.yaml",
        "reference/065-disclaimer-selection.yaml",
        "compliance/180-general-disclosures.yaml",
        "compliance/185-gio-general-disclosures.yaml",
        "compliance/190-rop-disadvantages.yaml",
        "compliance/195-rop-standard-declarations.yaml",
    ]
    assert all(len(source["source_sha256"]) == 64 for source in manifest["sources"])
    for source in manifest["sources"]:
        for field_name in ("approved_by", "approved_date", "ruling_ref", "status"):
            value = source[field_name]
            assert value not in (None, "", []), (source["path"], field_name)
            if isinstance(value, list):
                assert all(item not in (None, "") for item in value), (
                    source["path"],
                    field_name,
                )


def test_missing_provenance_field_fails_closed(tmp_path):
    for sequence, name in enumerate(
        ("rules", "compliance", "reference", "templates", "job-briefs")
    ):
        directory = tmp_path / name
        directory.mkdir()
        (directory / f"{name}.yaml").write_text(
            "# pa-source:\n"
            f"# sequence: {sequence}\n"
            "# approved_by: test\n"
            "# approved_date: 2026-08-01\n"
            "# status: approved\n"
            "# ---\n"
            "id: test\n"
        )
    with pytest.raises(PaComposeError, match="missing provenance fields: ruling_ref"):
        load_typed_sources(tmp_path)


def _typed_tree(tmp_path, provenance_overrides=None):
    """Write one minimally valid artifact per typed directory."""
    overrides = provenance_overrides or {}
    for sequence, name in enumerate(
        ("rules", "compliance", "reference", "templates", "job-briefs")
    ):
        directory = tmp_path / name
        directory.mkdir()
        header = {
            "sequence": sequence,
            "approved_by": "[amelia]",
            "approved_date": "['2026-08-01']",
            "ruling_ref": "[R13]",
            "status": "approved",
        }
        header.update(overrides.get(name, {}))
        lines = ["# pa-source:"]
        lines.extend(f"# {key}: {value}" for key, value in header.items())
        lines.extend(["# ---", "id: test", ""])
        (directory / f"{name}.yaml").write_text("\n".join(lines))
    return tmp_path


def test_live_mtu_provenance_is_strictly_non_null():
    """No required provenance field may be null, blank, or a null-bearing list."""
    for source in load_typed_sources(MTU):
        for field_name in ("approved_by", "approved_date", "ruling_ref", "status"):
            value = source.provenance[field_name]
            assert value is not None, f"{source.relative_path}: {field_name} is null"
            if isinstance(value, list):
                assert value, f"{source.relative_path}: {field_name} is an empty list"
                for item in value:
                    assert item is not None, (
                        f"{source.relative_path}: {field_name} carries a null entry"
                    )
                    assert str(item).strip(), (
                        f"{source.relative_path}: {field_name} carries a blank entry"
                    )
            else:
                assert str(value).strip(), f"{source.relative_path}: {field_name} is blank"


@pytest.mark.parametrize(
    "field_name, value",
    [
        ("approved_date", "[null]"),
        ("approved_date", "null"),
        ("approved_by", "[]"),
        ("ruling_ref", "['']"),
    ],
)
def test_null_provenance_value_fails_closed(tmp_path, field_name, value):
    """A present-but-empty field must refuse, not compose as an implied approval."""
    _typed_tree(tmp_path, {"compliance": {field_name: value}})
    with pytest.raises(
        PaComposeError, match=f"provenance fields must be non-null and non-empty: {field_name}"
    ):
        load_typed_sources(tmp_path)


def test_non_null_provenance_tree_still_loads(tmp_path):
    """Control: the same fixture shape loads when every field carries a claim."""
    _typed_tree(tmp_path)
    assert len(load_typed_sources(tmp_path)) == 5


def test_sync_knowledge_copies_only_manifest_entries(tmp_path):
    source = tmp_path / "source"
    (source / "reference").mkdir(parents=True)
    (source / "reference" / "declared.yaml").write_text("kind: keyed-reference\n")
    (source / "reference" / "not-declared.yaml").write_text("private: true\n")
    constitution = tmp_path / "constitution.yaml"
    constitution.write_text(_test_constitution("reference/declared.yaml"))
    target = tmp_path / "runtime" / "knowledge"
    manifest = tmp_path / "runtime" / "knowledge-sync.manifest.json"

    result = sync_pa_knowledge(source, constitution, target, manifest)

    assert result["file_count"] == 1
    assert (target / "reference" / "declared.yaml").read_text() == "kind: keyed-reference\n"
    assert not (target / "reference" / "not-declared.yaml").exists()
    assert json.loads(manifest.read_text()) == result


def test_sync_knowledge_supports_deploy_knowledge_source_directory(tmp_path):
    source = tmp_path / "source"
    (source / "knowledge").mkdir(parents=True)
    (source / "knowledge" / "guide.md").write_text("grounded guidance\n")
    constitution = tmp_path / "constitution.yaml"
    constitution.write_text(_test_constitution("guide.md"))

    sync_pa_knowledge(
        source,
        constitution,
        tmp_path / "runtime",
        tmp_path / "manifest.json",
    )

    assert (tmp_path / "runtime" / "guide.md").read_text() == "grounded guidance\n"


def _assembly_constitution(knowledge: str, artifacts: tuple[str, ...]) -> str:
    artifact_lines = "".join(f"          - {entry}\n" for entry in artifacts)
    return (
        "id: test\n"
        "agent_name: Test\n"
        "identity:\n  role: test\n"
        "client:\n  name: Test\n"
        "job_briefs:\n"
        "  test:\n"
        "    title: Test\n"
        "    purpose: Test knowledge sync.\n"
        "    knowledge:\n"
        f"      - {knowledge}\n"
        "    response_policy:\n"
        "      output_assembly:\n"
        "        enabled: true\n"
        "        mode: marker\n"
        "        artifacts:\n"
        f"{artifact_lines}"
    )


def test_sync_knowledge_copies_runtime_only_assembly_artifacts(tmp_path):
    """Deterministic-insertion artifacts are synced for the runtime without being model-visible."""
    source = tmp_path / "source"
    (source / "reference").mkdir(parents=True)
    (source / "compliance").mkdir(parents=True)
    (source / "reference" / "declared.yaml").write_text("kind: keyed-reference\n")
    (source / "compliance" / "block.yaml").write_text("schema_version: 1\n")
    constitution = tmp_path / "constitution.yaml"
    constitution.write_text(
        _assembly_constitution("reference/declared.yaml", ("compliance/block.yaml",))
    )

    result = sync_pa_knowledge(
        source,
        constitution,
        tmp_path / "runtime" / "knowledge",
        tmp_path / "runtime" / "knowledge-sync.manifest.json",
    )

    assert result["file_count"] == 2
    visibility = {entry["path"]: entry["visibility"] for entry in result["files"]}
    assert visibility == {
        "compliance/block.yaml": "runtime-only",
        "reference/declared.yaml": "model",
    }
    assert (tmp_path / "runtime" / "knowledge" / "compliance" / "block.yaml").is_file()


def test_sync_knowledge_refuses_missing_runtime_only_assembly_artifact(tmp_path):
    """A declared-but-absent deterministic artifact fails closed like any knowledge entry."""
    source = tmp_path / "source"
    (source / "reference").mkdir(parents=True)
    (source / "reference" / "declared.yaml").write_text("kind: keyed-reference\n")
    constitution = tmp_path / "constitution.yaml"
    constitution.write_text(
        _assembly_constitution("reference/declared.yaml", ("compliance/absent.yaml",))
    )

    with pytest.raises(PaComposeError, match="declared knowledge entry is missing"):
        sync_pa_knowledge(
            source,
            constitution,
            tmp_path / "runtime",
            tmp_path / "manifest.json",
        )


def test_mtu_compliance_artifacts_are_runtime_only_never_model_visible(tmp_path):
    """MTU's approved compliance text reaches the runtime but never the knowledge manifest."""
    from agent.pa_constitution import load_constitution

    constitution_path = MTU / "mtu_constitution.yaml"
    constitution = load_constitution(constitution_path)
    brief = constitution.job_briefs["bor_generation"]
    artifacts = tuple(brief.response_policy["output_assembly"]["artifacts"])

    assert artifacts, "MTU declares deterministic compliance artifacts"
    assert all(entry.startswith("compliance/") for entry in artifacts)
    assert not set(artifacts) & set(brief.knowledge)
    assert not [entry for entry in brief.knowledge if entry.startswith("compliance/")]

    constitution_text = constitution_path.read_text()
    knowledge_block = constitution_text.split("knowledge:", 1)[1].split("instructions:", 1)[0]
    for entry in artifacts:
        assert entry not in knowledge_block

    result = sync_pa_knowledge(
        MTU,
        constitution_path,
        tmp_path / "knowledge",
        tmp_path / "knowledge-sync.manifest.json",
    )
    synced = {entry["path"]: entry["visibility"] for entry in result["files"]}
    for entry in artifacts:
        assert synced[entry] == "runtime-only"
        assert (tmp_path / "knowledge" / entry).is_file()
    for entry in brief.knowledge:
        assert synced[entry] == "model"


def test_sync_knowledge_refuses_missing_declared_entry(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    constitution = tmp_path / "constitution.yaml"
    constitution.write_text(_test_constitution("reference/missing.yaml"))

    with pytest.raises(PaComposeError, match="declared knowledge entry is missing"):
        sync_pa_knowledge(
            source,
            constitution,
            tmp_path / "runtime",
            tmp_path / "manifest.json",
        )
