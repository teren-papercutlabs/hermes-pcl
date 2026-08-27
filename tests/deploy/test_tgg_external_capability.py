from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "deploy/tgg/christopher/scripts/apply_engine_slot.py"
SLOT = ROOT / "deploy/tgg/christopher/runtime-slots/gpt-5.6-terra-medium"


def load_module():
    spec = importlib.util.spec_from_file_location("apply_engine_slot_external_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_live_host_runtime_identity_is_validated_without_reserializing(
    tmp_path: Path,
) -> None:
    module = load_module()
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "pa": {"enabled": False},
        "model": {"provider": "openai-codex", "default": "gpt-5.6-terra"},
        "agent": {"reasoning_effort": "medium"},
        "group_sessions_per_user": False,
        "platforms": {"whatsapp": {"enabled": False}},
    }), encoding="utf-8")
    value = yaml.safe_load(config.read_text())
    value["model"]["credential_label"] = "configured-account"
    config.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")

    assert module._validate_live_runtime_config(
        config, "gpt-5.6-terra-medium"
    ) == ("openai-codex", "configured-account")


def make_release(
    tmp_path: Path,
    *,
    manifest_canary_chat_id: str | None = None,
    audience: str = "production",
    include_spreadsheet_skill: bool = False,
    include_whatsapp_skill: bool = False,
    include_hdb_skill: bool = False,
    include_nightly_plugin: bool = False,
    include_nightly_launcher: bool = False,
    include_per_case_plugin: bool = False,
    include_per_case_helpers: bool = False,
    include_coordinator_plugin: bool = False,
) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    runtime = home / "runtime"
    capability = runtime / "capabilities" / "christopher-tgg"
    release = capability / "releases" / "test-release"
    plugin = release / "plugins" / "tgg-whatsapp-evidence"
    plugin.mkdir(parents=True)
    constitution = release / "christopher_tgg_constitution.yaml"
    constitution.write_bytes((SLOT / "christopher_tgg_constitution.yaml").read_bytes())
    release_constitution = yaml.safe_load(constitution.read_text(encoding="utf-8"))
    release_constitution.pop("runtime", None)
    for brief in release_constitution.get("job_briefs", {}).values():
        if isinstance(brief, dict):
            brief.pop("runtime", None)
    constitution.write_text(
        yaml.safe_dump(release_constitution, sort_keys=False), encoding="utf-8"
    )
    config = yaml.safe_load((SLOT / "config.yaml").read_text(encoding="utf-8"))
    config["pa"]["constitution_path"] = str(home / constitution.name)
    enabled_plugins = config.setdefault("plugins", {}).setdefault("enabled", [])
    for plugin_id in ("report-operations", "tgg-whatsapp-evidence"):
        if plugin_id not in enabled_plugins:
            enabled_plugins.append(plugin_id)
    if include_nightly_plugin:
        enabled_plugins.append("tgg-nightly-whatsapp")
    if include_per_case_plugin:
        enabled_plugins.append("tgg-per-case-whatsapp")
    if include_coordinator_plugin:
        enabled_plugins.append("tgg-per-case-whatsapp-coordinator")
    if include_spreadsheet_skill or include_whatsapp_skill or include_hdb_skill:
        config["skills"] = {"external_dirs": [str(capability / "current" / "skills")]}
    else:
        config.pop("skills", None)
    config["host_sentinel"] = {"preserve": "bytes-and-values"}
    config_path = home / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    report_plugin = home / "plugins/report-operations/plugin.yaml"
    report_plugin.parent.mkdir(parents=True, exist_ok=True)
    report_plugin.write_text(
        "name: report-operations\nprovides_tools:\n  - report_status\n",
        encoding="utf-8",
    )
    (plugin / "__init__.py").write_text("def register(ctx):\n    return None\n", encoding="utf-8")
    (plugin / "plugin.yaml").write_text(
        "name: tgg-whatsapp-evidence\nprovides_tools:\n  - tgg_whatsapp_source_query\n",
        encoding="utf-8",
    )
    nightly_plugin = release / "plugins" / "tgg-nightly-whatsapp"
    if include_nightly_plugin:
        nightly_plugin.mkdir(parents=True)
        (nightly_plugin / "__init__.py").write_text(
            "def register(ctx):\n    return None\n", encoding="utf-8"
        )
        (nightly_plugin / "plugin.yaml").write_text(
            "name: tgg-nightly-whatsapp\nprovides_tools:\n  - tgg_nightly_prepare_batch\n", encoding="utf-8"
        )
    nightly_launcher = release / "scripts" / "run-nightly-whatsapp.py"
    if include_nightly_launcher:
        nightly_launcher.parent.mkdir(parents=True)
        nightly_launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    per_case_plugin = release / "plugins" / "tgg-per-case-whatsapp"
    if include_per_case_plugin:
        per_case_plugin.mkdir(parents=True)
        (per_case_plugin / "__init__.py").write_text(
            "def register(ctx):\n    return None\n", encoding="utf-8"
        )
        (per_case_plugin / "plugin.yaml").write_text(
            "name: tgg-per-case-whatsapp\nprovides_tools:\n  - tgg_whatsapp_case_prepare\n", encoding="utf-8"
        )
    coordinator_plugin = release / "plugins" / "tgg-per-case-whatsapp-coordinator"
    if include_coordinator_plugin:
        coordinator_plugin.mkdir(parents=True)
        (coordinator_plugin / "__init__.py").write_text(
            "def register(ctx):\n    return None\n", encoding="utf-8"
        )
        (coordinator_plugin / "plugin.yaml").write_text(
            "name: tgg-per-case-whatsapp-coordinator\nprovides_tools:\n  - tgg_whatsapp_case_list_create\n", encoding="utf-8"
        )
    per_case_engine = release / "scripts" / "per-case-whatsapp-engine.mjs"
    if include_per_case_helpers:
        per_case_engine.parent.mkdir(parents=True, exist_ok=True)
        per_case_engine.write_text("export {}\n", encoding="utf-8")
    skill = release / "skills" / "spreadsheet-work"
    if include_spreadsheet_skill:
        (skill / "agents").mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: spreadsheet-work\ndescription: Work with spreadsheets.\n---\n",
            encoding="utf-8",
        )
        (skill / "agents" / "openai.yaml").write_text(
            "interface:\n  display_name: Spreadsheet Work\n",
            encoding="utf-8",
        )
    whatsapp_skill = release / "skills" / "whatsapp-investigation"
    if include_whatsapp_skill:
        (whatsapp_skill / "agents").mkdir(parents=True)
        (whatsapp_skill / "references").mkdir(parents=True)
        (whatsapp_skill / "SKILL.md").write_text(
            "---\nname: whatsapp-investigation\ndescription: Investigate WhatsApp source.\n---\n",
            encoding="utf-8",
        )
        (whatsapp_skill / "agents" / "openai.yaml").write_text(
            "interface:\n  display_name: WhatsApp Investigation\n",
            encoding="utf-8",
        )
        (whatsapp_skill / "references" / "perception-policy.md").write_text(
            "# Perception policy\n", encoding="utf-8",
        )
    hdb_skill = release / "skills" / "hdb-reconciliation"
    if include_hdb_skill:
        (hdb_skill / "agents").mkdir(parents=True)
        (hdb_skill / "assets").mkdir(parents=True)
        (hdb_skill / "SKILL.md").write_text(
            "---\nname: hdb-reconciliation\ndescription: Reconcile HDB workbooks.\n---\n",
            encoding="utf-8",
        )
        (hdb_skill / "agents" / "openai.yaml").write_text(
            "interface:\n  display_name: HDB Reconciliation\n",
            encoding="utf-8",
        )
        (hdb_skill / "assets" / "source-layouts.md").write_text(
            "# Source layouts\n", encoding="utf-8"
        )
    release_files = [
        constitution,
        plugin / "__init__.py",
        plugin / "plugin.yaml",
    ]
    if include_spreadsheet_skill:
        release_files.extend([skill / "SKILL.md", skill / "agents" / "openai.yaml"])
    if include_whatsapp_skill:
        release_files.extend([
            whatsapp_skill / "SKILL.md",
            whatsapp_skill / "agents" / "openai.yaml",
            whatsapp_skill / "references" / "perception-policy.md",
        ])
    if include_hdb_skill:
        release_files.extend([
            hdb_skill / "SKILL.md",
            hdb_skill / "agents" / "openai.yaml",
            hdb_skill / "assets" / "source-layouts.md",
        ])
    if include_nightly_plugin:
        release_files.extend([
            nightly_plugin / "__init__.py",
            nightly_plugin / "plugin.yaml",
        ])
    if include_nightly_launcher:
        release_files.append(nightly_launcher)
    if include_per_case_plugin:
        release_files.extend([
            per_case_plugin / "__init__.py",
            per_case_plugin / "plugin.yaml",
        ])
    if include_coordinator_plugin:
        release_files.extend([
            coordinator_plugin / "__init__.py",
            coordinator_plugin / "plugin.yaml",
        ])
    if include_per_case_helpers:
        release_files.append(per_case_engine)
    files = {
        str(path.relative_to(release)): digest(path)
        for path in release_files
    }
    manifest = {
        "schema": "christopher-tgg-capability-release/v1",
        "release_id": release.name,
        "audience": audience,
        "files": files,
        "compatibility": {
            "contract": "christopher-tgg-host-config/v1",
            "plugins": [
                {"id": "report-operations", "tools": ["report_status"]},
                *(
                    [{"id": "tgg-nightly-whatsapp", "tools": ["tgg_nightly_prepare_batch"]}]
                    if include_nightly_plugin else []
                ),
                *(
                    [{"id": "tgg-per-case-whatsapp", "tools": ["tgg_whatsapp_case_prepare"]}]
                    if include_per_case_plugin else []
                ),
                *(
                    [{"id": "tgg-per-case-whatsapp-coordinator", "tools": ["tgg_whatsapp_case_list_create"]}]
                    if include_coordinator_plugin else []
                ),
                {"id": "tgg-whatsapp-evidence", "tools": ["tgg_whatsapp_source_query"]},
            ],
        },
        "canary": (
            {"management_chat_ids": [manifest_canary_chat_id]}
            if manifest_canary_chat_id
            else None
        ),
    }
    manifest_path = release / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    sums = {**files, "manifest.json": digest(manifest_path)}
    (release / "SHA256SUMS").write_text(
        "".join(f"{value}  {name}\n" for name, value in sorted(sums.items())),
        encoding="utf-8",
    )
    (capability / "current").symlink_to(release)
    return home, runtime, release


def test_resolves_verified_external_capability(tmp_path: Path) -> None:
    module = load_module()
    home, runtime, release = make_release(tmp_path)

    selected = module._external_capability(runtime, home, "gpt-5.6-terra-medium")

    assert selected is not None
    assert selected["release_id"] == "test-release"
    assert selected["release_root"] == release.resolve()
    assert selected["plugin_source"] == release / "plugins/tgg-whatsapp-evidence"
    assert not (release / "christopher-slot-config.yaml").exists()


def test_missing_host_plugin_refuses_without_config_or_pointer_mutation(
    tmp_path: Path,
) -> None:
    module = load_module()
    home, runtime, release = make_release(tmp_path)
    config_path = home / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["plugins"]["enabled"].remove("tgg-whatsapp-evidence")
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    config_before = config_path.read_bytes()
    current = runtime / "capabilities/christopher-tgg/current"
    current_before = current.resolve()

    with pytest.raises(RuntimeError, match="required plugin missing"):
        module._external_capability(runtime, home, "gpt-5.6-terra-medium")

    assert config_path.read_bytes() == config_before
    assert current.resolve() == current_before == release.resolve()
    assert not (home / "plugins/tgg-whatsapp-evidence").exists()


def test_resolves_external_capability_with_spreadsheet_skill(tmp_path: Path) -> None:
    module = load_module()
    home, runtime, release = make_release(tmp_path, include_spreadsheet_skill=True)

    selected = module._external_capability(runtime, home, "gpt-5.6-terra-medium")

    assert selected is not None
    assert selected["release_root"] == release.resolve()


def test_resolves_external_capability_with_new_manifest_pinned_skill(tmp_path: Path) -> None:
    module = load_module()
    home, runtime, release = make_release(tmp_path, include_hdb_skill=True)

    selected = module._external_capability(runtime, home, "gpt-5.6-terra-medium")

    assert selected is not None
    assert selected["release_root"] == release.resolve()


@pytest.mark.parametrize("include_spreadsheet_skill", [False, True])
def test_resolves_external_capability_with_whatsapp_skill(
    tmp_path: Path, include_spreadsheet_skill: bool
) -> None:
    module = load_module()
    home, runtime, release = make_release(
        tmp_path,
        include_spreadsheet_skill=include_spreadsheet_skill,
        include_whatsapp_skill=True,
    )

    selected = module._external_capability(runtime, home, "gpt-5.6-terra-medium")

    assert selected is not None
    assert selected["release_root"] == release.resolve()


def test_resolves_external_capability_with_nightly_plugin(tmp_path: Path) -> None:
    module = load_module()
    home, runtime, release = make_release(tmp_path, include_nightly_plugin=True)

    selected = module._external_capability(runtime, home, "gpt-5.6-terra-medium")

    assert selected is not None
    assert selected["release_root"] == release.resolve()


def test_resolves_external_capability_with_nightly_launcher(tmp_path: Path) -> None:
    module = load_module()
    home, runtime, release = make_release(
        tmp_path,
        include_nightly_plugin=True,
        include_nightly_launcher=True,
    )

    selected = module._external_capability(runtime, home, "gpt-5.6-terra-medium")

    assert selected is not None
    assert selected["release_root"] == release.resolve()
    assert set(selected["plugin_sources"]) == {
        "tgg-whatsapp-evidence",
        "tgg-nightly-whatsapp",
    }


def test_resolves_external_capability_with_per_case_component(tmp_path: Path) -> None:
    module = load_module()
    home, runtime, release = make_release(
        tmp_path,
        include_per_case_plugin=True,
        include_per_case_helpers=True,
    )

    selected = module._external_capability(runtime, home, "gpt-5.6-terra-medium")

    assert selected is not None
    assert selected["release_root"] == release.resolve()
    assert set(selected["plugin_sources"]) == {
        "tgg-whatsapp-evidence",
        "tgg-per-case-whatsapp",
    }


def test_resolves_manifest_pinned_new_coordinator_plugin(tmp_path: Path) -> None:
    module = load_module()
    home, runtime, release = make_release(
        tmp_path,
        include_per_case_plugin=True,
        include_per_case_helpers=True,
        include_coordinator_plugin=True,
    )

    selected = module._external_capability(runtime, home, "gpt-5.6-terra-medium")

    assert selected is not None
    assert selected["release_root"] == release.resolve()
    assert set(selected["plugin_sources"]) == {
        "tgg-whatsapp-evidence",
        "tgg-per-case-whatsapp",
        "tgg-per-case-whatsapp-coordinator",
    }


@pytest.mark.parametrize(
    ("include_per_case_plugin", "include_per_case_helpers"),
    [(True, False), (False, True)],
)
def test_rejects_incomplete_per_case_component(
    tmp_path: Path,
    include_per_case_plugin: bool,
    include_per_case_helpers: bool,
) -> None:
    module = load_module()
    home, runtime, _release = make_release(
        tmp_path,
        include_per_case_plugin=include_per_case_plugin,
        include_per_case_helpers=include_per_case_helpers,
    )

    with pytest.raises(RuntimeError, match="per-case component is incomplete"):
        module._external_capability(runtime, home, "gpt-5.6-terra-medium")


def test_rejects_skill_without_skill_md(tmp_path: Path) -> None:
    module = load_module()
    home, runtime, release = make_release(tmp_path, include_whatsapp_skill=True)
    manifest_path = release / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].pop("skills/whatsapp-investigation/SKILL.md")
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    sums = {**manifest["files"], "manifest.json": digest(manifest_path)}
    (release / "SHA256SUMS").write_text(
        "".join(f"{value}  {name}\n" for name, value in sorted(sums.items())),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="missing SKILL.md"):
        module._external_capability(runtime, home, "gpt-5.6-terra-medium")


def test_rejects_executable_skill_payload(tmp_path: Path) -> None:
    module = load_module()
    home, runtime, release = make_release(tmp_path, include_hdb_skill=True)
    executable = release / "skills" / "hdb-reconciliation" / "scripts" / "run.py"
    executable.parent.mkdir(parents=True)
    executable.write_text("raise SystemExit(1)\n", encoding="utf-8")
    manifest_path = release / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][str(executable.relative_to(release))] = digest(executable)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    sums = {**manifest["files"], "manifest.json": digest(manifest_path)}
    (release / "SHA256SUMS").write_text(
        "".join(f"{value}  {name}\n" for name, value in sorted(sums.items())),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="skill file is not allowed"):
        module._external_capability(runtime, home, "gpt-5.6-terra-medium")


def test_rejects_spreadsheet_skill_without_external_skill_path(tmp_path: Path) -> None:
    module = load_module()
    home, runtime, release = make_release(tmp_path, include_spreadsheet_skill=True)
    config_path = home / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config.pop("skills")
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    with pytest.raises(RuntimeError, match="capability skills path missing"):
        module._external_capability(runtime, home, "gpt-5.6-terra-medium")


def test_rejects_tampered_external_capability(tmp_path: Path) -> None:
    module = load_module()
    home, runtime, release = make_release(tmp_path)
    (release / "plugins/tgg-whatsapp-evidence/__init__.py").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        module._external_capability(runtime, home, "gpt-5.6-terra-medium")


def test_rejects_current_pointer_outside_release_root(tmp_path: Path) -> None:
    module = load_module()
    home, runtime, _release = make_release(tmp_path)
    current = runtime / "capabilities/christopher-tgg/current"
    current.unlink()
    outside = tmp_path / "outside"
    outside.mkdir()
    current.symlink_to(outside)

    with pytest.raises(RuntimeError, match="escapes the releases directory"):
        module._external_capability(runtime, home, "gpt-5.6-terra-medium")


def test_rejects_canary_manifest_even_when_both_management_chats_remain(
    tmp_path: Path,
) -> None:
    module = load_module()
    home, runtime, _release = make_release(
        tmp_path,
        manifest_canary_chat_id="120363426509183563@g.us",
    )

    with pytest.raises(RuntimeError, match="cannot restrict management selectors"):
        module._external_capability(runtime, home, "gpt-5.6-terra-medium")


def test_rejects_release_that_removes_real_management_chat(tmp_path: Path) -> None:
    module = load_module()
    home, runtime, release = make_release(tmp_path)
    constitution_path = release / "christopher_tgg_constitution.yaml"
    constitution = yaml.safe_load(constitution_path.read_text(encoding="utf-8"))
    constitution["selectors"] = [
        selector
        for selector in constitution["selectors"]
        if selector.get("match", {}).get("source.chat_id")
        != "120363407903158826@g.us"
    ]
    constitution_path.write_text(
        yaml.safe_dump(constitution, sort_keys=False), encoding="utf-8"
    )
    manifest_path = release / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["christopher_tgg_constitution.yaml"] = digest(
        constitution_path
    )
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    sums = {**manifest["files"], "manifest.json": digest(manifest_path)}
    (release / "SHA256SUMS").write_text(
        "".join(f"{value}  {name}\n" for name, value in sorted(sums.items())),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="shared management selector missing"):
        module._external_capability(runtime, home, "gpt-5.6-terra-medium")


def test_rejects_shadow_release(
    tmp_path: Path,
) -> None:
    module = load_module()
    home, runtime, _release = make_release(
        tmp_path,
        audience="shadow",
    )

    with pytest.raises(RuntimeError, match="must be a production release"):
        module._external_capability(runtime, home, "gpt-5.6-terra-medium")


def test_rejects_canary_restriction_on_production_release(tmp_path: Path) -> None:
    module = load_module()
    home, runtime, _release = make_release(
        tmp_path,
        manifest_canary_chat_id="120363426509183563@g.us",
        audience="production",
    )

    with pytest.raises(RuntimeError, match="cannot restrict management selectors"):
        module._external_capability(runtime, home, "gpt-5.6-terra-medium")


def test_main_materializes_external_capability_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_module()
    home, _runtime, release = make_release(
        tmp_path,
        include_per_case_plugin=True,
        include_per_case_helpers=True,
    )
    config_before = (home / "config.yaml").read_bytes()
    monkeypatch.setattr(module.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=501))
    monkeypatch.setattr(module.grp, "getgrnam", lambda _name: SimpleNamespace(gr_gid=20))
    monkeypatch.setattr(module.os, "chown", lambda *_args: None)
    monkeypatch.setattr(module.os, "lchown", lambda *_args: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--app-root",
            str(ROOT),
            "--hermes-home",
            str(home),
            "--slot",
            "gpt-5.6-terra-medium",
            "--preserve-host-config",
        ],
    )

    assert module.main() == 0

    assert (home / "config.yaml").read_bytes() == config_before
    live_config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert live_config["host_sentinel"] == {"preserve": "bytes-and-values"}
    release_constitution = yaml.safe_load(
        (release / "christopher_tgg_constitution.yaml").read_text(encoding="utf-8")
    )
    assert "runtime" not in release_constitution
    live_constitution = yaml.safe_load(
        (home / "christopher_tgg_constitution.yaml").read_text(encoding="utf-8")
    )
    assert live_constitution["runtime"] == {
        "provider": "openai-direct-primary",
        "model": "gpt-5.6-terra",
    }
    assert all(
        brief["runtime"] == live_constitution["runtime"]
        for brief in live_constitution["job_briefs"].values()
    )
    plugin = home / "plugins/tgg-whatsapp-evidence"
    assert plugin.is_symlink()
    assert plugin.resolve() == (release / "plugins/tgg-whatsapp-evidence").resolve()
    per_case_plugin = home / "plugins/tgg-per-case-whatsapp"
    assert per_case_plugin.is_symlink()
    assert per_case_plugin.resolve() == (
        release / "plugins/tgg-per-case-whatsapp"
    ).resolve()
    receipt = json.loads((home / "runtime/engine-slot-receipt.json").read_text())
    assert receipt["configuration_source"] == "external-capability"
    assert receipt["host_config_authoritative"] is True
    assert receipt["capability_release_id"] == "test-release"
    assert receipt["capability_manifest_sha256"] == digest(release / "manifest.json")


def test_preserve_host_config_accepts_unrelated_values_without_slot_reserialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_module()
    home, _runtime, _release = make_release(tmp_path)
    config_before = (home / "config.yaml").read_bytes()
    monkeypatch.setattr(module.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=501))
    monkeypatch.setattr(module.grp, "getgrnam", lambda _name: SimpleNamespace(gr_gid=20))
    monkeypatch.setattr(module.os, "chown", lambda *_args: None)
    monkeypatch.setattr(module.os, "lchown", lambda *_args: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--app-root",
            str(ROOT),
            "--hermes-home",
            str(home),
            "--slot",
            "gpt-5.6-terra-medium",
            "--preserve-host-config",
        ],
    )

    assert module.main() == 0

    assert (home / "config.yaml").read_bytes() == config_before
    assert (home / "plugins/tgg-whatsapp-evidence").is_symlink()


def test_main_refuses_to_replace_unpreserved_plugin_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_module()
    home, _runtime, _release = make_release(tmp_path)
    plugin = home / "plugins/tgg-whatsapp-evidence"
    plugin.mkdir(parents=True)
    monkeypatch.setattr(module.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=501))
    monkeypatch.setattr(module.grp, "getgrnam", lambda _name: SimpleNamespace(gr_gid=20))
    monkeypatch.setattr(module.os, "chown", lambda *_args: None)
    monkeypatch.setattr(module.os, "lchown", lambda *_args: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--app-root",
            str(ROOT),
            "--hermes-home",
            str(home),
            "--slot",
            "gpt-5.6-terra-medium",
        ],
    )

    with pytest.raises(RuntimeError, match="installer must preserve"):
        module.main()
