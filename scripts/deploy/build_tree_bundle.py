#!/usr/bin/env python3
"""Build a whole-tree git archive and derive its fingerprint receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
from typing import BinaryIO


_PINNED_COMMIT = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


class TreeBundleError(RuntimeError):
    """Raised when the requested source cannot produce a safe tree bundle."""


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise TreeBundleError(f"git {' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _resolve_source(repo: Path, commit: str) -> tuple[Path, str, str]:
    repo = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
    if not _PINNED_COMMIT.fullmatch(commit):
        raise TreeBundleError(
            "--commit must be a complete 40- or 64-character object id; "
            "branches, tags, and abbreviated ids are not pinned"
        )
    resolved = _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
    if resolved.lower() != commit.lower():
        raise TreeBundleError(
            f"commit did not resolve exactly: requested={commit} resolved={resolved}"
        )
    tree = _git(repo, "rev-parse", f"{resolved}^{{tree}}")
    return repo, resolved.lower(), tree.lower()


def _sha256_stream(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    while chunk := handle.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _safe_member_path(name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or not name or ".." in path.parts:
        raise TreeBundleError(f"unsafe archive member path: {name!r}")
    normalized = path.as_posix()
    if normalized in {".", ""}:
        raise TreeBundleError(f"invalid archive member path: {name!r}")
    return normalized


def inventory_archive(archive: Path) -> list[dict[str, object]]:
    """Read the built archive and fingerprint every tracked file entry."""
    inventory: list[dict[str, object]] = []
    seen: set[str] = set()
    with tarfile.open(archive, "r:") as bundle:
        for member in bundle:
            path = _safe_member_path(member.name)
            if member.isdir():
                continue
            if path in seen:
                raise TreeBundleError(f"duplicate archive member: {path}")
            seen.add(path)
            if member.isfile():
                handle = bundle.extractfile(member)
                if handle is None:
                    raise TreeBundleError(f"archive member has no content: {path}")
                with handle:
                    sha256 = _sha256_stream(handle)
                kind = "file"
                mode = f"100{member.mode & 0o777:03o}"
                size = member.size
            elif member.issym():
                link_bytes = member.linkname.encode("utf-8", errors="surrogateescape")
                sha256 = hashlib.sha256(link_bytes).hexdigest()
                kind = "symlink"
                mode = "120000"
                size = len(link_bytes)
            else:
                raise TreeBundleError(
                    f"unsupported archive member type for {path}: {member.type!r}"
                )
            inventory.append({
                "path": path,
                "type": kind,
                "mode": mode,
                "size": size,
                "sha256": sha256,
            })
    return sorted(inventory, key=lambda entry: str(entry["path"]))


def build_tree_bundle(
    *, repo: Path, commit: str, archive: Path, receipt: Path
) -> dict[str, object]:
    repo, resolved_commit, tree = _resolve_source(repo.resolve(strict=True), commit)
    archive = archive.resolve()
    receipt = receipt.resolve()
    if archive == receipt:
        raise TreeBundleError("archive and receipt paths must be different")
    for output in (archive, receipt):
        if output.exists() or output.is_symlink():
            raise TreeBundleError(f"refusing to overwrite existing output: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)

    archive_tmp: Path | None = None
    receipt_tmp: Path | None = None
    try:
        archive_fd, archive_name = tempfile.mkstemp(
            prefix=f".{archive.name}.", suffix=".tmp", dir=archive.parent
        )
        os.close(archive_fd)
        archive_tmp = Path(archive_name)
        _git(
            repo,
            "archive",
            "--format=tar",
            f"--output={archive_tmp}",
            resolved_commit,
        )

        files = inventory_archive(archive_tmp)
        archive_sha256 = hashlib.sha256(archive_tmp.read_bytes()).hexdigest()
        payload: dict[str, object] = {
            "version": 1,
            "source": {
                "commit": resolved_commit,
                "tree": tree,
            },
            "archive": {
                "format": "tar",
                "sha256": archive_sha256,
                "size": archive_tmp.stat().st_size,
            },
            "fileCount": len(files),
            "files": files,
        }

        receipt_fd, receipt_name = tempfile.mkstemp(
            prefix=f".{receipt.name}.", suffix=".tmp", dir=receipt.parent
        )
        receipt_tmp = Path(receipt_name)
        with os.fdopen(receipt_fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(archive_tmp, archive)
        archive_tmp = None
        os.replace(receipt_tmp, receipt)
        receipt_tmp = None
        return payload
    finally:
        if archive_tmp is not None:
            archive_tmp.unlink(missing_ok=True)
        if receipt_tmp is not None:
            receipt_tmp.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument(
        "--receipt",
        type=Path,
        help="Receipt path (default: <archive>.receipt.json)",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    receipt = args.receipt or Path(f"{args.archive}.receipt.json")
    try:
        payload = build_tree_bundle(
            repo=args.repo,
            commit=args.commit,
            archive=args.archive,
            receipt=receipt,
        )
    except (TreeBundleError, OSError, tarfile.TarError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "archive": str(args.archive.resolve()),
                "receipt": str(receipt.resolve()),
                "commit": payload["source"]["commit"],
                "fileCount": payload["fileCount"],
                "archiveSha256": payload["archive"]["sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
