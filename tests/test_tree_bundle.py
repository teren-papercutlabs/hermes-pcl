from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile


REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLER = REPO_ROOT / "scripts" / "deploy" / "build_tree_bundle.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fixture_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "tree-bundle@example.invalid")
    _git(repo, "config", "user.name", "Tree Bundle Test")
    (repo / "package").mkdir()
    (repo / "package" / "module.py").write_text("VALUE = 'pinned'\n")
    executable = repo / "run"
    executable.write_text("#!/bin/sh\nprintf 'ok\\n'\n")
    executable.chmod(0o755)
    (repo / "module-link").symlink_to("package/module.py")
    _git(repo, "add", "package/module.py", "run", "module-link")
    _git(repo, "commit", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _build(repo: Path, commit: str, archive: Path, receipt: Path):
    return subprocess.run(
        [
            sys.executable,
            str(BUNDLER),
            "--repo",
            str(repo),
            "--commit",
            commit,
            "--archive",
            str(archive),
            "--receipt",
            str(receipt),
        ],
        capture_output=True,
        text=True,
    )


def test_bundle_is_the_complete_pinned_tree_and_ignores_worktree(
    tmp_path: Path,
) -> None:
    repo, commit = _fixture_repo(tmp_path)
    (repo / "package" / "module.py").write_text("VALUE = 'dirty'\n")
    (repo / "untracked.txt").write_text("must not ship\n")
    archive = tmp_path / "tree.tar"
    receipt = tmp_path / "tree.receipt.json"

    completed = _build(repo, commit, archive, receipt)

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["ok"] is True
    assert result["commit"] == commit
    with tarfile.open(archive, "r:") as bundle:
        names = {member.name for member in bundle if not member.isdir()}
        assert names == {"module-link", "package/module.py", "run"}
        module = bundle.extractfile("package/module.py")
        assert module is not None
        assert module.read() == b"VALUE = 'pinned'\n"

    recorded = json.loads(receipt.read_text())
    assert recorded["fileCount"] == 3
    assert recorded["source"]["commit"] == commit
    assert (
        recorded["archive"]["sha256"]
        == hashlib.sha256(archive.read_bytes()).hexdigest()
    )


def test_receipt_fingerprints_are_derived_from_archive_entries(tmp_path: Path) -> None:
    repo, commit = _fixture_repo(tmp_path)
    archive = tmp_path / "tree.tar"
    receipt = tmp_path / "tree.receipt.json"
    completed = _build(repo, commit, archive, receipt)
    assert completed.returncode == 0, completed.stderr

    recorded = json.loads(receipt.read_text())
    files = {entry["path"]: entry for entry in recorded["files"]}
    with tarfile.open(archive, "r:") as bundle:
        module = bundle.extractfile("package/module.py")
        assert module is not None
        assert (
            files["package/module.py"]["sha256"]
            == hashlib.sha256(module.read()).hexdigest()
        )
        executable = bundle.getmember("run")
        assert files["run"]["mode"] == f"100{executable.mode & 0o777:03o}"
        assert executable.mode & 0o111
        link = bundle.getmember("module-link")
        assert files["module-link"] == {
            "mode": "120000",
            "path": "module-link",
            "sha256": hashlib.sha256(link.linkname.encode()).hexdigest(),
            "size": len(link.linkname.encode()),
            "type": "symlink",
        }


def test_same_commit_produces_identical_archive_and_receipt(tmp_path: Path) -> None:
    repo, commit = _fixture_repo(tmp_path)
    first_archive = tmp_path / "first.tar"
    first_receipt = tmp_path / "first.json"
    second_archive = tmp_path / "second.tar"
    second_receipt = tmp_path / "second.json"

    assert _build(repo, commit, first_archive, first_receipt).returncode == 0
    assert _build(repo, commit, second_archive, second_receipt).returncode == 0

    assert first_archive.read_bytes() == second_archive.read_bytes()
    assert first_receipt.read_bytes() == second_receipt.read_bytes()


def test_symbolic_commit_is_refused_as_unpinned(tmp_path: Path) -> None:
    repo, _commit = _fixture_repo(tmp_path)
    completed = _build(repo, "HEAD", tmp_path / "tree.tar", tmp_path / "tree.json")

    assert completed.returncode == 1
    assert "branches, tags, and abbreviated ids are not pinned" in completed.stderr
    assert not (tmp_path / "tree.tar").exists()
    assert not (tmp_path / "tree.json").exists()


def test_existing_output_is_never_overwritten(tmp_path: Path) -> None:
    repo, commit = _fixture_repo(tmp_path)
    archive = tmp_path / "tree.tar"
    archive.write_bytes(b"keep")
    completed = _build(repo, commit, archive, tmp_path / "tree.json")

    assert completed.returncode == 1
    assert "refusing to overwrite existing output" in completed.stderr
    assert archive.read_bytes() == b"keep"
