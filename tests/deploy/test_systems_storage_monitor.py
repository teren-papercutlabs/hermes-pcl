"""State-transition tests for the non-agent Systems storage monitor."""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def _module():
    path = Path(__file__).parents[2] / "deploy/tgg/systems/scripts/systems_storage_monitor.py"
    spec = importlib.util.spec_from_file_location("systems_storage_monitor_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Disk:
    total = 100 * 1024 ** 3
    free = 20 * 1024 ** 3


def test_warning_is_deduplicated_then_repeated_daily(tmp_path):
    monitor = _module()
    receipt_dir = tmp_path / "receipts"; receipt_dir.mkdir()
    (receipt_dir / "systems-retention-20260817T000000Z.json").write_text(json.dumps({"status": "completed", "deleted_bytes": 0}))
    args = argparse.Namespace(disk_path="/", receipt_dir=str(receipt_dir), state_path=str(tmp_path / "state.json"),
                              telegram_chat_id="chat", dry_run=False)
    alerts = []
    result = monitor.run_monitor(args, now=lambda: 1000, disk_usage=lambda _: Disk(),
                                 run=lambda _: type("R", (), {"stdout": "inactive"})(),
                                 notify=lambda _, message, **__: alerts.append(message))
    assert result["conditions"]["disk"] == "warning"
    assert len(alerts) == 1
    monitor.run_monitor(args, now=lambda: 1001, disk_usage=lambda _: Disk(),
                        run=lambda _: type("R", (), {"stdout": "inactive"})(),
                        notify=lambda _, message, **__: alerts.append(message))
    assert len(alerts) == 1
    monitor.run_monitor(args, now=lambda: 1000 + 86401, disk_usage=lambda _: Disk(),
                        run=lambda _: type("R", (), {"stdout": "inactive"})(),
                        notify=lambda _, message, **__: alerts.append(message))
    assert len(alerts) == 2


def test_send_failure_does_not_consume_alert_and_live_after_dry_run_sends(tmp_path):
    monitor = _module()
    receipt_dir = tmp_path / "receipts"; receipt_dir.mkdir()
    (receipt_dir / "systems-retention-20260817T000000Z.json").write_text(json.dumps({"status": "completed"}))
    state_path = tmp_path / "state.json"
    args = argparse.Namespace(disk_path="/", receipt_dir=str(receipt_dir), state_path=str(state_path),
                              telegram_chat_id="chat", dry_run=False)
    systemctl = lambda _: type("R", (), {"stdout": "inactive"})()
    try:
        monitor.run_monitor(args, now=lambda: 1000, disk_usage=lambda _: Disk(), run=systemctl,
                            notify=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("network")))
    except RuntimeError as error:
        assert str(error) == "network"
    else:
        raise AssertionError("send failure must be visible")
    assert json.loads(state_path.read_text())["alerted_at"] == {}
    sent = []
    monitor.run_monitor(args, now=lambda: 1001, disk_usage=lambda _: Disk(), run=systemctl,
                        notify=lambda _, message, **__: sent.append(message))
    assert len(sent) == 1

    dry_state = tmp_path / "dry-state.json"
    dry_args = argparse.Namespace(disk_path="/", receipt_dir=str(receipt_dir), state_path=str(dry_state),
                                  telegram_chat_id="chat", dry_run=True)
    monitor.run_monitor(dry_args, now=lambda: 2000, disk_usage=lambda _: Disk(), run=systemctl,
                        notify=lambda *_args, **_kwargs: None)
    assert json.loads(dry_state.read_text())["alerted_at"] == {}
    dry_args.dry_run = False
    monitor.run_monitor(dry_args, now=lambda: 2001, disk_usage=lambda _: Disk(), run=systemctl,
                        notify=lambda _, message, **__: sent.append(message))
    assert len(sent) == 2
