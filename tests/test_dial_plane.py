import json
import os
import subprocess
import sys
import time
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_cli.dial_plane import (
    DialOverlayStore,
    DialPlaneRefusal,
    DialPlaneSchema,
    dials_command,
)


@pytest.fixture
def schema():
    return DialPlaneSchema.load().with_scopes(["chat:management", "chat:operations"])


@pytest.fixture
def store(tmp_path, schema):
    return DialOverlayStore(tmp_path / "dial-overlays.json", schema)


def test_schema_declares_five_named_model_effort_slots_without_spend_default(schema):
    assert len(schema.slots) == 5
    assert all(slot.model and slot.cost_tag for slot in schema.slots.values())
    assert {slot.cost_rank for slot in schema.slots.values()} == {1, 2, 3, 4, 5}
    assert schema.dials["model_slot"].default == "inherit"
    authority = schema.authority_tiers["directional_model_cost"]
    assert authority["swap_down"] == "edna"
    assert authority["swap_up"] == "teren"


def test_bad_key_is_refused(store):
    with pytest.raises(DialPlaneRefusal) as error:
        store.set(key="imaginary", scope="default", value=1, actor="operator")
    assert error.value.code == "UNKNOWN_KEY"


@pytest.mark.parametrize("value", [0, 201, "90", True])
def test_out_of_range_or_wrong_type_is_refused(store, value):
    with pytest.raises(DialPlaneRefusal) as error:
        store.set(key="turn_budget", scope="default", value=value, actor="operator")
    assert error.value.code == "INVALID_VALUE"


def test_unknown_scope_is_refused(store):
    with pytest.raises(DialPlaneRefusal) as error:
        store.set(key="turn_budget", scope="chat:not-declared", value=50, actor="operator")
    assert error.value.code == "UNKNOWN_SCOPE"


def test_scope_override_persists_and_unmapped_scope_falls_back_to_default(store):
    store.set(key="turn_budget", scope="default", value=70, actor="operator-a")
    store.set(key="turn_budget", scope="chat:management", value=110, actor="operator-b")

    reloaded = DialOverlayStore(store.path, store.schema)
    assert reloaded.resolve("turn_budget", "chat:management") == 110
    assert reloaded.resolve("turn_budget", "chat:operations") == 70
    assert json.loads(store.path.read_text())["values"] == {
        "turn_budget": {"default": 70, "chat:management": 110}
    }


def test_no_overlay_falls_back_to_declared_schema_default(store):
    assert store.resolve("log_level", "chat:operations") == "INFO"
    assert store.resolve("model_slot", "chat:management") == "inherit"


def test_atomic_state_keeps_append_only_change_receipts(store):
    store.set(key="log_level", scope="default", value="DEBUG", actor="first")
    store.set(key="log_level", scope="default", value="WARNING", actor="second")
    audit = store.audit()

    assert len(audit) == 2
    assert audit[0]["actor"] == "first"
    assert audit[0]["old"] == "INFO" and audit[0]["new"] == "DEBUG"
    assert audit[1]["actor"] == "second"
    assert audit[1]["old"] == "DEBUG" and audit[1]["new"] == "WARNING"
    assert all(entry["changed_at"].endswith("+00:00") for entry in audit)


def test_concurrent_writers_preserve_both_values_and_receipts(store, monkeypatch):
    from hermes_cli import dial_plane

    real_write = dial_plane._atomic_write_json

    def slow_write(path, payload):
        time.sleep(0.1)
        real_write(path, payload)

    monkeypatch.setattr(dial_plane, "_atomic_write_json", slow_write)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            store.set,
            key="turn_budget",
            scope="chat:management",
            value=70,
            actor="first",
        )
        second = executor.submit(
            store.set,
            key="turn_budget",
            scope="chat:operations",
            value=80,
            actor="second",
        )
        first.result()
        second.result()

    assert store.resolve("turn_budget", "chat:management") == 70
    assert store.resolve("turn_budget", "chat:operations") == 80
    assert {entry["actor"] for entry in store.audit()} == {"first", "second"}


def test_gated_write_command_refuses_invalid_value(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    result = dials_command(
        Namespace(
            definitions=None,
            store=tmp_path / "store.json",
            key="turn_budget",
            scope="default",
            value="999",
            actor="operator",
        )
    )
    output = json.loads(capsys.readouterr().out)
    assert result == 2
    assert output["ok"] is False
    assert output["error"]["code"] == "INVALID_VALUE"


def test_write_time_cannot_declare_an_unknown_scope(tmp_path, capsys, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("dial_plane:\n  scopes: [chat:known]\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    result = dials_command(
        Namespace(
            definitions=None,
            store=tmp_path / "store.json",
            key="turn_budget",
            scope="chat:invented-at-write-time",
            value="50",
            actor="operator",
        )
    )
    output = json.loads(capsys.readouterr().out)
    assert result == 2
    assert output["error"]["code"] == "UNKNOWN_SCOPE"


def test_hermes_dials_set_runs_the_real_cli_path(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("dial_plane:\n  scopes: [chat:management]\n")
    store = tmp_path / "store.json"
    env = dict(os.environ, HERMES_HOME=str(home))

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "dials",
            "set",
            "--store",
            str(store),
            "--key",
            "model_slot",
            "--scope",
            "chat:management",
            "--value",
            "economy",
            "--actor",
            "edna",
        ],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["ok"] is True
    assert output["data"]["receipt"]["authority_tier"] == "directional_model_cost"
    assert json.loads(store.read_text())["values"] == {
        "model_slot": {"chat:management": "economy"}
    }
