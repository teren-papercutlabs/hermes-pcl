#!/usr/bin/env python3
"""Daily disk and Systems-retention monitor with durable alert deduplication."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

GIB = 1024 ** 3
WARN_USED_PERCENT = 75
CRITICAL_USED_PERCENT = 85
WARN_FREE_BYTES = 15 * GIB
CRITICAL_FREE_BYTES = 8 * GIB
FRESHNESS_SECONDS = 26 * 60 * 60


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _latest_receipt(receipt_dir: Path) -> tuple[Path | None, dict[str, Any]]:
    receipts = sorted(path for path in receipt_dir.glob("systems-retention-*.json")
                      if not path.name.endswith(".intent.json")) if receipt_dir.is_dir() else []
    if not receipts:
        return None, {}
    path = receipts[-1]
    return path, _read_json(path, {})


def _service_failed(run: Callable[[list[str]], subprocess.CompletedProcess[str]]) -> bool:
    result = run(["systemctl", "is-failed", "systems-papercut-labs-tgg-retention-cleanup.service"])
    return result.stdout.strip() == "failed"


def _disk_state(used_percent: float, free: int) -> str:
    if used_percent >= CRITICAL_USED_PERCENT or free < CRITICAL_FREE_BYTES:
        return "critical"
    if used_percent >= WARN_USED_PERCENT or free < WARN_FREE_BYTES:
        return "warning"
    return "ok"


def _cleanup_state(receipt: dict[str, Any], receipt_path: Path | None, now: float,
                   service_failed: bool) -> tuple[str, str]:
    if service_failed:
        return "failure", "cleanup service is failed"
    if receipt_path is None:
        return "failure", "no cleanup receipt exists"
    age = now - receipt_path.stat().st_mtime
    if receipt.get("status") != "completed":
        return "failure", "latest cleanup receipt is not completed"
    if age > FRESHNESS_SECONDS:
        return "failure", f"latest cleanup receipt is {int(age // 3600)}h old"
    return "ok", f"last cleanup deleted {int(receipt.get('deleted_bytes', 0)) // GIB} GiB"


def _send_telegram(chat_id: str, message: str, *, dry_run: bool) -> None:
    if dry_run:
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Telegram token or chat id is not configured")
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:
        if json.loads(response.read()).get("ok") is not True:
            raise RuntimeError("Telegram rejected Systems alert")


def _message(kind: str, state: str, *, usage: float, free: int, growth: int, cleanup: str) -> str:
    prefix = "RECOVERED" if state == "ok" else state.upper()
    return (f"[Systems] {prefix}: {kind}. Disk {usage:.1f}% used, {free / GIB:.1f} GiB free, "
            f"{growth / GIB:+.2f} GiB since yesterday. {cleanup}.")


def run_monitor(args: argparse.Namespace, *, now: Callable[[], float] = time.time,
                disk_usage: Callable[[str], Any] = shutil.disk_usage,
                run: Callable[[list[str]], subprocess.CompletedProcess[str]] = lambda cmd: subprocess.run(cmd, text=True, capture_output=True, check=False),
                notify: Callable[..., None] = _send_telegram) -> dict[str, Any]:
    moment = now()
    state_path = Path(args.state_path)
    old = _read_json(state_path, {})
    disk = disk_usage(args.disk_path)
    used = disk.total - disk.free
    percent = used * 100 / disk.total
    growth = used - int(old.get("used_bytes", used))
    disk_state = _disk_state(percent, disk.free)
    receipt_path, receipt = _latest_receipt(Path(args.receipt_dir))
    cleanup_state, cleanup_detail = _cleanup_state(receipt, receipt_path, moment, _service_failed(run))
    conditions = {"disk": disk_state, "cleanup": cleanup_state}
    old_conditions = old.get("conditions", {}) if isinstance(old.get("conditions"), dict) else {}
    alerts: list[tuple[str, str]] = []
    next_alerted = dict(old.get("alerted_at", {})) if isinstance(old.get("alerted_at"), dict) else {}
    for kind, state in conditions.items():
        previous = old_conditions.get(kind)
        due_repeat = state != "ok" and (kind not in next_alerted or moment - float(next_alerted[kind]) >= 24 * 60 * 60)
        changed = previous != state
        if (changed and not (previous is None and state == "ok")) or due_repeat:
            alerts.append((kind, _message(kind, state, usage=percent, free=disk.free, growth=growth, cleanup=cleanup_detail)))
    result = {"schema": "systems-storage-monitor/v1", "checked_at": datetime.fromtimestamp(moment, timezone.utc).isoformat(),
              "used_bytes": used, "free_bytes": disk.free, "used_percent": percent, "growth_since_last_bytes": growth,
              "conditions": conditions, "alerted_at": next_alerted,
              "cleanup": {"receipt": str(receipt_path) if receipt_path else None, "detail": cleanup_detail},
              "alerts": [message for _, message in alerts]}
    # Persist the observed state before delivery. If delivery fails the durable
    # absence of an alerted_at advance makes that exact alert retryable.
    _atomic_json(state_path, result)
    for kind, message in alerts:
        try:
            notify(args.telegram_chat_id, message, dry_run=args.dry_run)
        except Exception as exc:
            result["last_notification_error"] = f"{type(exc).__name__}: {exc}"
            _atomic_json(state_path, result)
            raise
        if not args.dry_run:
            next_alerted[kind] = moment
            result["alerted_at"] = next_alerted
            _atomic_json(state_path, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disk-path", default="/home/pclaw/.systems-pcl")
    parser.add_argument("--receipt-dir", default="/home/pclaw/.systems-pcl/data/retention-receipts")
    parser.add_argument("--state-path", default="/var/lib/systems-papercut-labs/storage-monitor-state.json")
    parser.add_argument("--telegram-chat-id", default=os.environ.get("SYSTEMS_TELEGRAM_CHAT_ID", ""))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run_monitor(args), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
