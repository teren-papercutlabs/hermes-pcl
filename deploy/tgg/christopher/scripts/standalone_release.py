#!/usr/bin/env python3
"""Small, self-contained Christopher runtime + capability release executor.

This deliberately replaces only Christopher's competing in-place deploy paths.
It is not a generic PA release framework: one host, two immutable payloads, one
activity lock, one service restart, and a JSON receipt.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

SERVICE = "christopher-tgg-hermes.service"
SCHEMA = "tgg_christopher_standalone_release/v1"
DEFAULT_ROOT = Path("/opt/tgg-christopher")
DEFAULT_HOME = Path("/home/pclaw/.hermes-christopher-tgg")
DEFAULT_UNIT = Path("/etc/systemd/system/christopher-tgg-hermes.service")
UNIT_REL = Path("deploy/tgg/christopher/systemd/christopher-tgg-hermes.service")


class ReleaseError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def safe_member(member: tarfile.TarInfo) -> None:
    path = Path(member.name)
    if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
        raise ReleaseError(f"unsafe archive member: {member.name}")


def archive_tree(source: Path, target: Path) -> str:
    if not source.is_dir():
        raise ReleaseError(f"payload directory does not exist: {source}")
    with tarfile.open(target, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(source.rglob("*")):
            archive.add(path, arcname=str(path.relative_to(source)), recursive=False)
    return sha256(target)


def file_inventory(source: Path) -> dict[str, str]:
    return {
        str(path.relative_to(source)): sha256(path)
        for path in sorted(source.rglob("*")) if path.is_file()
    }


def make_immutable_tree(root: Path) -> None:
    """Remove write bits after a release's exact fileset has been verified."""
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            raise ReleaseError(f"immutable release contains symlink: {path}")
        path.chmod(path.stat().st_mode & ~0o222)
    root.chmod(root.stat().st_mode & ~0o222)


def stage_runtime(source: Path, manifest_path: Path, destination: Path, commit: str) -> dict[str, str]:
    """Make a payload from the declared runtime include list, never a worktree."""
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    include = raw.get("include")
    if not isinstance(include, list) or not include or not all(isinstance(x, str) for x in include):
        raise ReleaseError("runtime manifest must contain a non-empty include list")
    for relative in include:
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts or not (source / rel).is_file():
            raise ReleaseError(f"runtime manifest includes invalid file: {relative}")
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, target)
    (destination / ".git-revision").write_text(commit + "\n")
    return file_inventory(destination)


def required_capability_files(capability: Path, manifest: dict[str, Any]) -> dict[str, str]:
    """Validate capability's own file inventory before archiving it."""
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ReleaseError("capability manifest must contain files {path: sha256}")
    normalized: dict[str, str] = {}
    for relative, digest in files.items():
        rel = Path(str(relative))
        if rel.is_absolute() or ".." in rel.parts or not isinstance(digest, str):
            raise ReleaseError("capability manifest has invalid file inventory")
        actual = capability / rel
        if not actual.is_file() or sha256(actual) != digest:
            raise ReleaseError(f"capability manifest mismatch: {relative}")
        normalized[str(rel)] = digest
    actual_files = file_inventory(capability)
    allowed = set(normalized) | {"manifest.json"}
    if "SHA256SUMS" in actual_files:
        listed: dict[str, str] = {}
        for line in (capability / "SHA256SUMS").read_text().splitlines():
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                raise ReleaseError("invalid capability SHA256SUMS")
            digest, relative = parts[0], parts[1].lstrip(" *")
            if relative in listed or relative not in set(normalized) | {"manifest.json"} or listed.get(relative):
                raise ReleaseError("invalid capability SHA256SUMS path")
            listed[relative] = digest
        expected_sums = {**normalized, "manifest.json": sha256(capability / "manifest.json")}
        if listed != expected_sums:
            raise ReleaseError("capability SHA256SUMS differs from manifest files")
        allowed.add("SHA256SUMS")
    if set(actual_files) != allowed:
        raise ReleaseError("capability payload files differ from manifest inventory")
    return normalized


def extract_verified(archive_path: Path, expected_sha: str, destination: Path) -> None:
    if sha256(archive_path) != expected_sha:
        raise ReleaseError(f"payload hash mismatch: {archive_path.name}")
    if destination.exists():
        raise ReleaseError(f"immutable release already exists: {destination}")
    destination.mkdir(parents=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            safe_member(member)
        archive.extractall(destination, members=members, filter="data")


def read_manifest(capability: Path) -> dict[str, Any]:
    manifest = capability / "manifest.json"
    if not manifest.is_file():
        raise ReleaseError("capability payload has no manifest.json")
    raw = json.loads(manifest.read_text())
    if not isinstance(raw.get("release_id"), str) or not raw["release_id"]:
        raise ReleaseError("capability manifest has no release_id")
    return raw


def capability_plugins(capability: Path) -> list[str]:
    plugins = capability / "plugins"
    if not plugins.is_dir():
        return []
    result = [path.name for path in sorted(plugins.iterdir()) if path.is_dir()]
    if any("/" in value or value in {".", ".."} for value in result):
        raise ReleaseError("invalid capability plugin name")
    return result


def declared_runtime_compatibility(manifest: dict[str, Any], runtime_commit: str) -> str:
    """Require the capability to name the exact runtime (or its git prefix)."""
    declared = str((manifest.get("runtime") or {}).get("hermes_commit") or "").strip()
    if len(declared) < 7 or any(char not in "0123456789abcdef" for char in declared.lower()):
        raise ReleaseError("capability manifest has no valid runtime.hermes_commit")
    if not runtime_commit.startswith(declared):
        raise ReleaseError(f"capability runtime mismatch: declared={declared} staged={runtime_commit}")
    return declared


def runtime_identity(runtime: Path) -> str:
    marker = runtime / ".git-revision"
    if marker.is_file():
        return marker.read_text().strip()
    try:
        return subprocess.run(
            ["git", "-C", str(runtime), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseError("runtime payload must contain .git-revision") from exc


def replace_pointer(pointer: Path, target: Path) -> None:
    if not target.is_dir():
        raise ReleaseError(f"pointer target is not a directory: {target}")
    pointer.parent.mkdir(parents=True, exist_ok=True)
    temporary = pointer.with_name(f".{pointer.name}.{os.getpid()}.new")
    temporary.symlink_to(target)
    os.replace(temporary, pointer)


def ensure_plugin_pointer_directory(home: Path) -> dict[str, Any]:
    """Keep the service-owned plugin pointer directory traversable.

    A prior installer created this directory as root with mode 0600. Root could
    still flip symlinks, so deployment appeared healthy, while the pclaw
    service silently loaded no capability tools after restart.
    """
    directory = home / "plugins"
    if directory.is_symlink():
        raise ReleaseError("plugin pointer directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True)
    if not directory.is_dir():
        raise ReleaseError("plugin pointer path is not a directory")
    owner = home.stat()
    os.chown(directory, owner.st_uid, owner.st_gid)
    directory.chmod(0o750)
    state = directory.stat()
    return {"path": str(directory), "uid": state.st_uid, "gid": state.st_gid,
            "mode": oct(state.st_mode & 0o777)}


def verify_plugin_pointer_directory(home: Path) -> dict[str, Any]:
    directory = home / "plugins"
    if directory.is_symlink() or not directory.is_dir():
        raise ReleaseError("plugin pointer directory is invalid")
    owner, state = home.stat(), directory.stat()
    if (state.st_uid, state.st_gid, state.st_mode & 0o777) != (owner.st_uid, owner.st_gid, 0o750):
        raise ReleaseError("plugin pointer directory is not service-traversable")
    return {"path": str(directory), "uid": state.st_uid, "gid": state.st_gid,
            "mode": oct(state.st_mode & 0o777)}


def install_unit(source: Path, destination: Path) -> str:
    """Atomically install the runtime-owned systemd unit and return its hash."""
    if not source.is_file():
        raise ReleaseError("runtime payload has no Christopher systemd unit")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.new")
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)
    return sha256(destination)


def restore_unit(previous: bytes | None, destination: Path) -> str | None:
    if previous is None:
        destination.unlink(missing_ok=True)
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.restore")
    temporary.write_bytes(previous)
    os.replace(temporary, destination)
    return sha256(destination)


def pointer_target(pointer: Path) -> str | None:
    return str(pointer.resolve()) if pointer.is_symlink() else None


def restore_pointer(pointer: Path, target: str | None) -> None:
    """Restore an old pointer, or remove a plugin introduced by this release."""
    if target is not None:
        replace_pointer(pointer, Path(target))
        return
    if pointer.is_symlink():
        pointer.unlink()
    elif pointer.exists():
        raise ReleaseError(f"refusing to remove non-pointer path: {pointer}")


def processing_rows(inbox: Path) -> int:
    if not inbox.exists():
        return 0
    with sqlite3.connect(f"file:{inbox}?mode=ro", uri=True) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "ingress_events" not in tables:
            return 0
        return int(db.execute("SELECT COUNT(*) FROM ingress_events WHERE status='processing'").fetchone()[0])


class ExclusiveActivityLock:
    def __init__(self, path: Path):
        self.path, self.handle = path, None

    def __enter__(self) -> "ExclusiveActivityLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            raise ReleaseError("Christopher is processing; release refused") from exc
        return self

    def __exit__(self, *_: object) -> None:
        if self.handle:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def command(argv: list[str]) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout.strip()


def restart_service() -> None:
    """Clear systemd's inherited start-limit before our single owned restart."""
    command(["systemctl", "reset-failed", SERVICE])
    command(["systemctl", "restart", SERVICE])


def recover_service() -> None:
    subprocess.run(["systemctl", "reset-failed", SERVICE], check=False)
    subprocess.run(["systemctl", "restart", SERVICE], check=False)


def systemctl_status(unit: str, verb: str) -> dict[str, Any]:
    """Read systemd status without mistaking inactive/disabled for an error."""
    result = subprocess.run(["systemctl", verb, unit], check=False, capture_output=True, text=True)
    state = result.stdout.strip()
    allowed = ({"active", "inactive", "failed"} if verb == "is-active"
               else {"enabled", "disabled", "static", "linked"})
    if state not in allowed:
        raise ReleaseError(f"unexpected systemctl {verb} result: {state!r} rc={result.returncode}")
    return {"state": state, "returncode": result.returncode}


def control_state(home: Path) -> dict[str, Any]:
    config = home / "config.yaml"; gate = home / "runtime/processing-gate.json"
    if not config.is_file() or not gate.is_file():
        raise ReleaseError("runtime configuration or processing gate missing")
    timer = "christopher-tgg-nightly-whatsapp.timer"
    controls = {"config_sha256": sha256(config), "gate_sha256": sha256(gate),
            "gate_enabled": bool(json.loads(gate.read_text()).get("enabled")),
            "timer_active": systemctl_status(timer, "is-active"),
            "timer_enabled": systemctl_status(timer, "is-enabled")}
    # The 18-August rollout is intentionally manual; a release must not turn
    # an unattended timer into an accidental side effect.
    if controls["timer_active"]["state"] != "inactive":
        raise ReleaseError("nightly timer must remain inactive for this rollout")
    return controls


def operational_controls_unchanged(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Capability config may change; gate and schedule must not change implicitly."""
    keys = ("gate_sha256", "gate_enabled", "timer_active", "timer_enabled")
    return all(before.get(key) == after.get(key) for key in keys)


def focused_verify(root: Path, home: Path, expected: dict[str, Any], before_controls: dict[str, Any]) -> dict[str, Any]:
    runtime = root / "runtime/current"
    capability = root / "capability/current"
    if runtime_identity(runtime) != expected["runtime_commit"]:
        raise ReleaseError("effective runtime identity mismatch")
    manifest = read_manifest(capability)
    if manifest["release_id"] != expected["capability_release_id"]:
        raise ReleaseError("effective capability identity mismatch")
    if command(["systemctl", "is-active", SERVICE]) != "active":
        raise ReleaseError("Christopher service is not active")
    import yaml
    config = home / "config.yaml"
    enabled = bool((yaml.safe_load(config.read_text(encoding="utf-8")) or {}).get("pa", {}).get("enabled"))
    controls = control_state(home)
    if controls["gate_enabled"] != enabled:
        raise ReleaseError("processing gate disagrees with configuration")
    if not operational_controls_unchanged(before_controls, controls):
        raise ReleaseError("release changed processing gate or timer state")
    engine = json.loads((home / "runtime/engine-slot-receipt.json").read_text(encoding="utf-8"))
    profile = json.loads((home / "runtime/provider-profile.json").read_text(encoding="utf-8"))
    if engine.get("config_sha256") != controls["config_sha256"]:
        raise ReleaseError("effective configuration is not bound to engine receipt")
    if (engine.get("provider"), engine.get("model"), engine.get("reasoning_effort")) != (
        expected["provider"], expected["model"], expected["reasoning_effort"]):
        raise ReleaseError("effective engine identity mismatch")
    if profile.get("provider") != expected["provider"]:
        raise ReleaseError("effective provider-profile mismatch")
    rows = processing_rows(home / "runtime/capture-inbox.db")
    if rows:
        raise ReleaseError("processing rows appeared during release")
    plugin_directory = verify_plugin_pointer_directory(home)
    return {"service": "active", "processing_enabled": enabled, "controls": controls,
            "runtime_commit": expected["runtime_commit"], "capability_release_id": manifest["release_id"],
            "plugin_pointer_directory": plugin_directory}


def prepare(args: argparse.Namespace) -> int:
    runtime, capability, out = Path(args.runtime).resolve(), Path(args.capability).resolve(), Path(args.out).resolve()
    manifest = read_manifest(capability)
    required_capability_files(capability, manifest)
    commit = runtime_identity(runtime)
    declared_compatibility = declared_runtime_compatibility(manifest, commit)
    out.mkdir(parents=True, exist_ok=False)
    runtime_archive, capability_archive = out / "runtime.tgz", out / "capability.tgz"
    staged_runtime = out / ".runtime-payload"
    runtime_files = stage_runtime(runtime, Path(args.runtime_manifest).resolve(), staged_runtime, commit)
    release = {"schema": SCHEMA, "created_at": int(time.time()), "runtime_commit": commit,
               "runtime_files": runtime_files,
               "runtime_sha256": archive_tree(staged_runtime, runtime_archive),
               "capability_release_id": manifest["release_id"],
               "capability_manifest_sha256": sha256(capability / "manifest.json"),
               "capability_files": file_inventory(capability),
               "capability_sha256": archive_tree(capability, capability_archive),
               "provider": args.provider, "model": args.model,
               "reasoning_effort": args.reasoning_effort,
               "capability_declared_runtime_commit": declared_compatibility}
    shutil.rmtree(staged_runtime)
    atomic_json(out / "release.json", release)
    print(json.dumps(release, sort_keys=True))
    return 0


def apply(args: argparse.Namespace) -> int:
    bundle, root, home = Path(args.bundle).resolve(), Path(args.root), Path(args.hermes_home)
    unit_path = Path(getattr(args, "systemd_unit", DEFAULT_UNIT))
    release = json.loads((bundle / "release.json").read_text())
    if release.get("schema") != SCHEMA:
        raise ReleaseError("unsupported release schema")
    expected = {key: release[key] for key in ("runtime_commit", "capability_release_id", "provider", "model", "reasoning_effort")}
    inbox = home / "runtime/capture-inbox.db"
    receipt = root / "transactions" / f"{int(time.time())}-{release['runtime_commit'][:12]}" / "receipt.json"
    with ExclusiveActivityLock(root / "release-activity.lock"):
        if processing_rows(inbox):
            raise ReleaseError("Christopher has processing inbox rows; release refused")
        old = {"runtime": pointer_target(root / "runtime/current"), "capability": pointer_target(root / "capability/current")}
        old["home_capability"] = pointer_target(home / "runtime/capabilities/christopher-tgg/current")
        if not old["runtime"] or not old["capability"] or not old["home_capability"]:
            raise ReleaseError("first activation must be seeded with valid runtime and capability pointers")
        ensure_plugin_pointer_directory(home)
        before_controls = control_state(home)
        before_unit = unit_path.read_bytes() if unit_path.exists() else None
        old["unit_sha256"] = sha256(unit_path) if before_unit is not None else None
        stage = root / ".stage" / str(os.getpid())
        activation_started = False
        try:
            extract_verified(bundle / "runtime.tgz", release["runtime_sha256"], stage / "runtime")
            extract_verified(bundle / "capability.tgz", release["capability_sha256"], stage / "capability")
            if runtime_identity(stage / "runtime") != release["runtime_commit"]:
                raise ReleaseError("runtime archive identity mismatch")
            if sha256(stage / "capability/manifest.json") != release["capability_manifest_sha256"]:
                raise ReleaseError("capability manifest hash mismatch")
            if file_inventory(stage / "runtime") != release["runtime_files"]:
                raise ReleaseError("runtime fileset mismatch")
            if file_inventory(stage / "capability") != release["capability_files"]:
                raise ReleaseError("capability fileset mismatch")
            runtime_dest = root / "runtime/releases" / release["runtime_commit"]
            # apply_engine_slot deliberately trusts only this existing Hermes
            # release tree. Keep one immutable copy there; the transaction
            # identity pointer under /opt targets it rather than duplicating it.
            capability_dest = home / "runtime/capabilities/christopher-tgg/releases" / release["capability_release_id"]
            runtime_dest.parent.mkdir(parents=True, exist_ok=True); capability_dest.parent.mkdir(parents=True, exist_ok=True)
            for staged, destination, expected_files in ((stage / "runtime", runtime_dest, release["runtime_files"]), (stage / "capability", capability_dest, release["capability_files"])):
                if destination.exists():
                    if file_inventory(destination) != expected_files:
                        raise ReleaseError("release id exists with different immutable payload")
                else:
                    os.replace(staged, destination)
                make_immutable_tree(destination)
            plugins = capability_plugins(capability_dest)
            old["plugins"] = {name: pointer_target(home / "plugins" / name) for name in plugins}
            activation_started = True
            replace_pointer(root / "runtime/current", runtime_dest)
            replace_pointer(root / "capability/current", capability_dest)
            # Home-level links preserve existing runtime selectors without copying capability files.
            replace_pointer(home / "runtime/capabilities/christopher-tgg/current", capability_dest)
            for name in plugins:
                replace_pointer(home / "plugins" / name, capability_dest / "plugins" / name)
            after_unit_hash = install_unit(runtime_dest / UNIT_REL, unit_path)
            command(["systemctl", "daemon-reload"])
            restart_service()
            verified = focused_verify(root, home, expected, before_controls)
            outcome = {"schema": SCHEMA, "status": "committed", "before": old, "after": {**verified, "unit_sha256": after_unit_hash}}
        except Exception as exc:
            if activation_started:
                restore_unit(before_unit, unit_path)
                subprocess.run(["systemctl", "daemon-reload"], check=False)
                if old["runtime"]: replace_pointer(root / "runtime/current", Path(old["runtime"]))
                if old["capability"]:
                    replace_pointer(root / "capability/current", Path(old["capability"]))
                    replace_pointer(home / "runtime/capabilities/christopher-tgg/current", Path(old["capability"]))
                for name, target in (old.get("plugins") or {}).items():
                    restore_pointer(home / "plugins" / name, target)
                recover_service()
            outcome = {"schema": SCHEMA, "status": "rolled_back", "before": old, "error": str(exc)}
            atomic_json(receipt, outcome)
            raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    atomic_json(receipt, outcome)
    print(json.dumps({**outcome, "receipt": str(receipt)}, sort_keys=True))
    return 0


def rollback(args: argparse.Namespace) -> int:
    receipt = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
    before = receipt.get("before") or {}
    if not before.get("runtime") or not before.get("capability") or not isinstance(before.get("plugins"), dict):
        raise ReleaseError("receipt has no rollback targets")
    root, home = Path(args.root), Path(args.hermes_home)
    unit_path = Path(getattr(args, "systemd_unit", DEFAULT_UNIT))
    with ExclusiveActivityLock(root / "release-activity.lock"):
        if processing_rows(home / "runtime/capture-inbox.db"):
            raise ReleaseError("Christopher is processing; rollback refused")
        ensure_plugin_pointer_directory(home)
        replace_pointer(root / "runtime/current", Path(before["runtime"]))
        replace_pointer(root / "capability/current", Path(before["capability"]))
        replace_pointer(home / "runtime/capabilities/christopher-tgg/current", Path(before["capability"]))
        for name, target in before["plugins"].items():
            restore_pointer(home / "plugins" / name, target)
        prior_runtime_unit = Path(before["runtime"]) / UNIT_REL
        install_unit(prior_runtime_unit, unit_path)
        command(["systemctl", "daemon-reload"])
        restart_service()
    print(json.dumps({"schema": SCHEMA, "status": "rolled_back", "receipt": args.receipt}, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("prepare"); make.add_argument("--runtime", required=True); make.add_argument("--runtime-manifest", required=True); make.add_argument("--capability", required=True); make.add_argument("--out", required=True)
    make.add_argument("--provider", required=True); make.add_argument("--model", required=True); make.add_argument("--reasoning-effort", required=True)
    for name in ("apply", "rollback"):
        item = sub.add_parser(name); item.add_argument("--root", default=str(DEFAULT_ROOT)); item.add_argument("--hermes-home", default=str(DEFAULT_HOME))
        item.add_argument("--systemd-unit", default=str(DEFAULT_UNIT))
    sub.choices["apply"].add_argument("--bundle", required=True)
    sub.choices["rollback"].add_argument("--receipt", required=True)
    args = parser.parse_args(argv)
    try:
        return {"prepare": prepare, "apply": apply, "rollback": rollback}[args.command](args)
    except ReleaseError as exc:
        print(f"release refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
