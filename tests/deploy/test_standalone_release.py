from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "deploy/tgg/christopher/scripts/standalone_release.py"
spec = importlib.util.spec_from_file_location("standalone_release", SCRIPT)
assert spec and spec.loader
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def capability(path: Path, release_id: str = "r1") -> None:
    path.mkdir(parents=True)
    (path / "plugin.py").write_text("x = 1\n")
    (path / "manifest.json").write_text(json.dumps({"release_id": release_id, "runtime": {"hermes_commit": "abc1234"}, "files": {"plugin.py": release.sha256(path / "plugin.py")}}))


def runtime(path: Path, revision: str = "abc1234") -> None:
    path.mkdir(parents=True)
    (path / ".git-revision").write_text(revision + "\n")
    (path / "gateway.py").write_text("pass\n")
    unit = path / "deploy/tgg/christopher/systemd/christopher-tgg-hermes.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(f"[Service]\n# {revision}\n")


def runtime_manifest(path: Path) -> Path:
    manifest = path / "runtime-manifest.json"
    manifest.write_text(json.dumps({"include": ["gateway.py", "deploy/tgg/christopher/systemd/christopher-tgg-hermes.service"]}))
    return manifest


def test_prepare_builds_separately_hashed_payloads(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rt, cap, bundle = tmp_path / "runtime", tmp_path / "capability", tmp_path / "bundle"
    runtime(rt); capability(cap)
    assert release.main(["prepare", "--runtime", str(rt), "--runtime-manifest", str(runtime_manifest(rt)), "--capability", str(cap), "--out", str(bundle), "--provider", "openai-codex", "--model", "x", "--reasoning-effort", "medium"]) == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["runtime_commit"] == "abc1234"
    assert emitted["capability_release_id"] == "r1"
    assert release.sha256(bundle / "runtime.tgz") == emitted["runtime_sha256"]
    assert release.sha256(bundle / "capability.tgz") == emitted["capability_sha256"]


def test_extract_rejects_modified_payload(tmp_path: Path) -> None:
    payload = tmp_path / "payload"; runtime(payload)
    archive = tmp_path / "payload.tgz"
    expected = release.archive_tree(payload, archive)
    archive.write_bytes(archive.read_bytes() + b"tamper")
    with pytest.raises(release.ReleaseError, match="hash mismatch"):
        release.extract_verified(archive, expected, tmp_path / "out")


def test_capability_accepts_only_verified_sha256sums_sidecar(tmp_path: Path) -> None:
    cap = tmp_path / "cap"; capability(cap)
    manifest = json.loads((cap / "manifest.json").read_text())
    (cap / "SHA256SUMS").write_text(f"{manifest['files']['plugin.py']}  plugin.py\n{release.sha256(cap / 'manifest.json')}  manifest.json\n")
    assert release.required_capability_files(cap, manifest) == manifest["files"]


def test_capability_runtime_prefix_must_match() -> None:
    assert release.declared_runtime_compatibility({"runtime": {"hermes_commit": "a9cb3d0af7"}}, "a9cb3d0af7bbbb") == "a9cb3d0af7"
    with pytest.raises(release.ReleaseError, match="mismatch"):
        release.declared_runtime_compatibility({"runtime": {"hermes_commit": "deadbee"}}, "a9cb3d0af7bbbb")


def test_exclusive_release_lock_refuses_shared_consumer_lock(tmp_path: Path) -> None:
    activity = tmp_path / "activity.lock"
    child = subprocess.Popen([sys.executable, "-c", "import fcntl,sys,time; f=open(sys.argv[1],'a+'); fcntl.flock(f,fcntl.LOCK_SH); print('locked', flush=True); time.sleep(5)", str(activity)], stdout=subprocess.PIPE, text=True)
    assert child.stdout and child.stdout.readline().strip() == "locked"
    with pytest.raises(release.ReleaseError, match="processing"):
        with release.ExclusiveActivityLock(activity):
            pass
    child.terminate(); child.wait(timeout=2)


def test_processing_rows_reads_only_processing(tmp_path: Path) -> None:
    import sqlite3
    inbox = tmp_path / "inbox.db"
    with sqlite3.connect(inbox) as db:
        db.execute("CREATE TABLE ingress_events(status TEXT)")
        db.executemany("INSERT INTO ingress_events VALUES(?)", [("pending",), ("processing",), ("processing",)])
    assert release.processing_rows(inbox) == 2


def test_systemctl_status_accepts_inactive_exit_three(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        def __init__(self, state, code): self.stdout, self.returncode = state + "\n", code
    monkeypatch.setattr(release.subprocess, "run", lambda argv, **kwargs: Result("inactive", 3) if "is-active" in argv else Result("linked", 0))
    assert release.systemctl_status("nightly.timer", "is-active") == {"state": "inactive", "returncode": 3}


def test_apply_flips_all_pointers_and_rolls_back_on_verify_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, home = tmp_path / "root", tmp_path / "home"
    old_runtime, old_cap = root / "runtime/releases/old", root / "capability/releases/old"
    runtime(old_runtime, "old"); capability(old_cap, "oldcap")
    (old_cap / "plugins/tgg").mkdir(parents=True)
    root.joinpath("runtime").mkdir(parents=True, exist_ok=True); root.joinpath("capability").mkdir(parents=True, exist_ok=True)
    release.replace_pointer(root / "runtime/current", old_runtime); release.replace_pointer(root / "capability/current", old_cap)
    release.replace_pointer(home / "runtime/capabilities/christopher-tgg/current", old_cap)
    release.replace_pointer(home / "plugins/tgg", old_cap / "plugins/tgg")
    (home / "runtime").mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("pa:\n  enabled: false\n")
    (home / "runtime/processing-gate.json").write_text('{"enabled": false}')
    rt, cap, bundle = tmp_path / "rt", tmp_path / "cap", tmp_path / "bundle"
    runtime(rt, "abcdef0"); capability(cap, "newcap"); (cap / "plugins/tgg").mkdir(parents=True)
    # Plugin content must be represented in the release manifest.
    (cap / "plugins/tgg/plugin.py").write_text("x=2\n")
    manifest = json.loads((cap / "manifest.json").read_text()); manifest["runtime"]["hermes_commit"] = "abcdef0"; manifest["files"]["plugins/tgg/plugin.py"] = release.sha256(cap / "plugins/tgg/plugin.py"); (cap / "manifest.json").write_text(json.dumps(manifest))
    assert release.main(["prepare", "--runtime", str(rt), "--runtime-manifest", str(runtime_manifest(rt)), "--capability", str(cap), "--out", str(bundle), "--provider", "p", "--model", "m", "--reasoning-effort", "r"]) == 0
    monkeypatch.setattr(release, "command", lambda argv: "inactive" if "is-active" in argv else "disabled")
    class Result:
        def __init__(self, state, code): self.stdout, self.returncode = state + "\n", code
    monkeypatch.setattr(release.subprocess, "run", lambda argv, **kwargs: Result("inactive", 3) if "is-active" in argv else Result("linked", 0))
    observed: dict[str, str | None] = {}
    def fail_verify(current_root, current_home, *_args):
        observed["runtime"] = release.pointer_target(current_root / "runtime/current")
        observed["plugin"] = release.pointer_target(current_home / "plugins/tgg")
        observed["unit"] = unit_path.read_text()
        raise release.ReleaseError("bad verify")
    monkeypatch.setattr(release, "focused_verify", fail_verify)
    unit_path = tmp_path / "christopher.service"; unit_path.write_text("[Service]\n# old\n")
    args = type("A", (), {"bundle": str(bundle), "root": str(root), "hermes_home": str(home), "systemd_unit": str(unit_path)})()
    with pytest.raises(release.ReleaseError, match="bad verify"):
        release.apply(args)
    assert release.pointer_target(root / "runtime/current") == str(old_runtime)
    assert release.pointer_target(home / "plugins/tgg") == str(old_cap / "plugins/tgg")
    assert unit_path.read_text() == "[Service]\n# old\n"
    approved = home / "runtime/capabilities/christopher-tgg/releases/newcap"
    assert observed == {"runtime": str(root / "runtime/releases/abcdef0"), "plugin": str(approved / "plugins/tgg"), "unit": "[Service]\n# abcdef0\n"}
    assert str(approved).startswith(str(home / "runtime/capabilities/christopher-tgg/releases"))
