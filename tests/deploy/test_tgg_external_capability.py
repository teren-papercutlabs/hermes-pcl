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


def make_release(tmp_path: Path) -> tuple[Path, Path, Path]:
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
    config_path = release / "christopher-slot-config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    (plugin / "__init__.py").write_text("def register(ctx):\n    return None\n", encoding="utf-8")
    (plugin / "plugin.yaml").write_text("name: tgg-whatsapp-evidence\n", encoding="utf-8")
    files = {
        str(path.relative_to(release)): digest(path)
        for path in (
            config_path,
            constitution,
            plugin / "__init__.py",
            plugin / "plugin.yaml",
        )
    }
    manifest = {
        "schema": "christopher-tgg-capability-release/v1",
        "release_id": release.name,
        "audience": "shadow",
        "files": files,
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

    assert (home / "config.yaml").read_bytes() == (
        release / "christopher-slot-config.yaml"
    ).read_bytes()
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
