from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[2] / "deploy/tgg/christopher/scripts/apply_engine_slot.py"
SMOKE = Path(__file__).resolve().parents[2] / "deploy/tgg/christopher/scripts/run_isolated_smoke.py"
PROVIDER_SWITCH = Path(__file__).resolve().parents[2] / "deploy/tgg/christopher/scripts/switch_provider_profile.sh"
ENGINE_SWITCH = Path(__file__).resolve().parents[2] / "deploy/tgg/christopher/scripts/switch_engine_slot.sh"

def _module():
    spec = importlib.util.spec_from_file_location("provider_profile_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def _run(module, monkeypatch, app, home, *arguments):
    monkeypatch.setattr(module.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=os.getuid()))
    monkeypatch.setattr(module.grp, "getgrnam", lambda _: SimpleNamespace(gr_gid=os.getgid()))
    monkeypatch.setattr(module.os, "chown", lambda *_: None)
    monkeypatch.setattr(module.os, "lchown", lambda *_: None)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--app-root", str(app), "--hermes-home", str(home), *arguments])
    assert module.main() == 0

def test_codex_profile_survives_every_boot_materialization(tmp_path, monkeypatch):
    module = _module()
    home = tmp_path / "home"
    _run(module, monkeypatch, SCRIPT.parents[4], home, "--provider-profile", "openai-codex", "--credential-label", "teren-temporary")
    _run(module, monkeypatch, SCRIPT.parents[4], home)
    config = yaml.safe_load((home / "config.yaml").read_text())
    constitution = yaml.safe_load((home / "christopher_tgg_constitution.yaml").read_text())
    receipt = json.loads((home / "runtime/engine-slot-receipt.json").read_text())
    assert config["model"]["provider"] == "openai-codex"
    assert config["model"]["credential_label"] == "teren-temporary"
    assert constitution["runtime"]["provider"] == "openai-codex"
    assert receipt["credential_label"] == "teren-temporary"
    assert "token" not in json.dumps(receipt).lower()

def test_codex_profile_requires_named_credential(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="credential-label"):
        _run(_module(), monkeypatch, SCRIPT.parents[4], tmp_path / "home", "--provider-profile", "openai-codex")


def test_isolated_ready_turn_uses_live_provider_and_auth() -> None:
    source = SMOKE.read_text()
    assert 'config["model"] = selected["model"]' in source
    assert 'live_constitution,' in source
    assert 'shutil.copyfile(live_auth, run_root / "auth.json")' in source


@pytest.mark.parametrize("script", [PROVIDER_SWITCH, ENGINE_SWITCH])
def test_operator_switch_resets_start_limit_and_handles_restart_failure(script: Path) -> None:
    source = script.read_text()
    reset = "systemctl reset-failed christopher-tgg-hermes.service"
    restart = "systemctl restart christopher-tgg-hermes.service"
    assert reset in source
    assert source.index(reset) < source.index(restart)
    assert "if ! restart_service; then" in source
