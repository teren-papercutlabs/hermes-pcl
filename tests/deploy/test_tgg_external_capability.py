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


def make_release(
    tmp_path: Path,
    *,
    manifest_canary_chat_id: str | None = None,
    audience: str = "shadow",
    include_spreadsheet_skill: bool = False,
    include_whatsapp_skill: bool = False,
    include_hdb_skill: bool = False,
    include_nightly_plugin: bool = False,
    include_nightly_launcher: bool = False,
) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    runtime = home / "runtime"
    capability = runtime / "capabilities" / "christopher-tgg"
    release = capability / "releases" / "test-release"
    plugin = release / "plugins" / "tgg-whatsapp-evidence"
    plugin.mkdir(parents=True)
    constitution = release / "christopher_tgg_constitution.yaml"
    constitution.write_bytes((SLOT / "christopher_tgg_constitution.yaml").read_bytes())
    config = yaml.safe_load((SLOT / "config.yaml").read_text(encoding="utf-8"))
    config["pa"]["constitution_path"] = str(capability / "current" / constitution.name)
    config.setdefault("plugins", {}).setdefault("enabled", []).append("tgg-whatsapp-evidence")
    if include_nightly_plugin:
        config["plugins"]["enabled"].append("tgg-nightly-whatsapp")
    if include_spreadsheet_skill or include_whatsapp_skill or include_hdb_skill:
        config["skills"] = {"external_dirs": [str(capability / "current" / "skills")]}
    else:
        config.pop("skills", None)
    config_path = release / "christopher-slot-config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    (plugin / "__init__.py").write_text("def register(ctx):\n    return None\n", encoding="utf-8")
    (plugin / "plugin.yaml").write_text("name: tgg-whatsapp-evidence\n", encoding="utf-8")
    nightly_plugin = release / "plugins" / "tgg-nightly-whatsapp"
    if include_nightly_plugin:
        nightly_plugin.mkdir(parents=True)
        (nightly_plugin / "__init__.py").write_text(
            "def register(ctx):\n    return None\n", encoding="utf-8"
        )
        (nightly_plugin / "plugin.yaml").write_text(
            "name: tgg-nightly-whatsapp\n", encoding="utf-8"
        )
    nightly_launcher = release / "scripts" / "run-nightly-whatsapp.py"
    if include_nightly_launcher:
        nightly_launcher.parent.mkdir(parents=True)
        nightly_launcher.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
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
        (whatsapp_skill / "SKILL.md").write_text(
            "---\nname: whatsapp-investigation\ndescription: Investigate WhatsApp source.\n---\n",
            encoding="utf-8",
        )
        (whatsapp_skill / "agents" / "openai.yaml").write_text(
            "interface:\n  display_name: WhatsApp Investigation\n",
            encoding="utf-8",
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
        config_path,
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
    files = {
        str(path.relative_to(release)): digest(path)
        for path in release_files
    }
    manifest = {
        "schema": "christopher-tgg-capability-release/v1",
        "release_id": release.name,
        "audience": audience,
        "files": files,
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
    config_path = release / "christopher-slot-config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config.pop("skills")
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    manifest_path = release / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["christopher-slot-config.yaml"] = digest(config_path)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    sums_path = release / "SHA256SUMS"
    sums = {
        **manifest["files"],
        "manifest.json": digest(manifest_path),
    }
    sums_path.write_text(
        "".join(f"{value}  {name}\n" for name, value in sorted(sums.items())),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="skills path mismatch"):
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


def test_accepts_canary_manifest_when_constitution_keeps_other_management_chats(
    tmp_path: Path,
) -> None:
    module = load_module()
    home, runtime, release = make_release(
        tmp_path,
        manifest_canary_chat_id="120363426509183563@g.us",
    )

    selected = module._external_capability(runtime, home, "gpt-5.6-terra-medium")

    assert selected is not None
    assert selected["release_root"] == release.resolve()


def test_rejects_canary_manifest_when_named_selector_is_missing(
    tmp_path: Path,
) -> None:
    module = load_module()
    home, runtime, _release = make_release(
        tmp_path,
        manifest_canary_chat_id="120363499999999999@g.us",
    )

    with pytest.raises(RuntimeError, match="canary selector missing"):
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
    home, _runtime, release = make_release(tmp_path)
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

    assert module.main() == 0

    live_config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    release_config = yaml.safe_load(
        (release / "christopher-slot-config.yaml").read_text(encoding="utf-8")
    )
    assert live_config["pa"]["constitution_path"] == str(
        home / "christopher_tgg_constitution.yaml"
    )
    live_config["pa"]["constitution_path"] = release_config["pa"]["constitution_path"]
    assert live_config == release_config
    assert (home / "christopher_tgg_constitution.yaml").read_bytes() == (
        release / "christopher_tgg_constitution.yaml"
    ).read_bytes()
    plugin = home / "plugins/tgg-whatsapp-evidence"
    assert plugin.is_symlink()
    assert plugin.resolve() == (release / "plugins/tgg-whatsapp-evidence").resolve()
    receipt = json.loads((home / "runtime/engine-slot-receipt.json").read_text())
    assert receipt["configuration_source"] == "external-capability"
    assert receipt["capability_release_id"] == "test-release"
    assert receipt["capability_manifest_sha256"] == digest(release / "manifest.json")


def test_engine_slot_overlays_runtime_fields_on_external_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load_module()
    home, _runtime, release = make_release(tmp_path)
    config_path = release / "christopher-slot-config.yaml"
    stale_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    stale_config["pa"]["media_retention"].pop("min_free_bytes", None)
    stale_config["pa"]["media_retention"]["min_free_percent"] = 20
    config_path.write_text(yaml.safe_dump(stale_config, sort_keys=False), encoding="utf-8")
    manifest_path = release / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["christopher-slot-config.yaml"] = digest(config_path)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    sums = {**manifest["files"], "manifest.json": digest(manifest_path)}
    (release / "SHA256SUMS").write_text(
        "".join(f"{value}  {name}\n" for name, value in sorted(sums.items())),
        encoding="utf-8",
    )
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
            "gpt-5.6-terra-high",
            "--provider-profile",
            "openai-codex",
            "--credential-label",
            "teren-temporary",
        ],
    )

    assert module.main() == 0

    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    assert config["model"]["provider"] == "openai-codex"
    assert config["model"]["credential_label"] == "teren-temporary"
    assert config["model"]["default"] == "gpt-5.6-terra"
    assert config["agent"]["reasoning_effort"] == "high"
    assert config["pa"]["media_retention"]["min_free_bytes"] == 5 * 1024**3
    assert "min_free_percent" not in config["pa"]["media_retention"]
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
