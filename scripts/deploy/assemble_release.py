#!/usr/bin/env python3
"""Build and atomically select a coherent Python application release.

Virtual environments are not relocatable.  This assembler deliberately creates
the environment only after the source archive has reached its final release
path, then proves console-script and import bindings before moving ``current``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
from typing import Sequence


_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class AssemblyError(RuntimeError):
    """Raised when a candidate release is not safe to promote."""


def _run(command: Sequence[str], *, cwd: Path | None = None) -> None:
    # Keep stdout as a machine-readable receipt channel. Build/install detail
    # remains visible to an operator on stderr.
    subprocess.run(command, cwd=cwd, check=True, stdout=sys.stderr, stderr=sys.stderr)


def _extract_archive(archive: Path, release_dir: Path) -> None:
    if sys.version_info < (3, 11, 4):
        raise AssemblyError(
            "safe tar extraction requires Python 3.11.4 or newer "
            f"(running {sys.version.split()[0]})"
        )
    with tarfile.open(archive, "r:*") as bundle:
        # The data filter rejects absolute paths, parent traversal, devices, and
        # other members inappropriate for an application source release.
        bundle.extractall(release_dir, filter="data")


def _venv_bin(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def _read_shebang(entrypoint: Path) -> str:
    with entrypoint.open("rb") as handle:
        return handle.readline().decode("utf-8", errors="strict").rstrip("\r\n")


def _verify_entrypoints(bin_dir: Path, python: Path, names: Sequence[str]) -> dict[str, str]:
    expected = f"#!{python}"
    verified: dict[str, str] = {}
    for name in names:
        entrypoint = bin_dir / name
        if not entrypoint.is_file():
            raise AssemblyError(f"required entrypoint is missing: {entrypoint}")
        shebang = _read_shebang(entrypoint)
        if shebang != expected:
            raise AssemblyError(
                f"entrypoint {entrypoint} binds {shebang!r}; expected {expected!r}"
            )
        verified[name] = shebang
    return verified


def _verify_modules(python: Path, release_dir: Path, names: Sequence[str]) -> dict[str, str]:
    probe = """
import importlib
import json
import pathlib
import sys

release = pathlib.Path(sys.argv[1]).resolve()
venv = (release / ".venv").resolve()
result = {}
for name in sys.argv[2:]:
    module = importlib.import_module(name)
    origin = getattr(module, "__file__", None)
    if not origin:
        raise RuntimeError(f"module {name!r} has no filesystem origin")
    resolved = pathlib.Path(origin).resolve()
    try:
        resolved.relative_to(release)
    except ValueError as exc:
        raise RuntimeError(
            f"module {name!r} loaded from {resolved}, outside selected release {release}"
        ) from exc
    try:
        resolved.relative_to(venv)
    except ValueError:
        pass
    else:
        raise RuntimeError(
            f"module {name!r} loaded from environment copy {resolved}, "
            f"not selected release source {release}"
        )
    result[name] = str(resolved)
print(json.dumps(result, sort_keys=True))
"""
    completed = subprocess.run(
        [str(python), "-c", probe, str(release_dir), *names],
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise AssemblyError(f"module-origin verification failed: {detail}")
    return json.loads(completed.stdout)


def _promote(current: Path, release_dir: Path) -> str | None:
    previous: str | None = None
    if current.is_symlink():
        previous = str(current.resolve(strict=False))
    elif current.exists():
        raise AssemblyError(f"current path exists but is not a symlink: {current}")

    temporary = current.with_name(f".{current.name}.next-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(release_dir)
    os.replace(temporary, current)
    return previous


def assemble_release(
    *,
    archive: Path,
    app_root: Path,
    release_id: str,
    bootstrap_python: str,
    extras: Sequence[str],
    modules: Sequence[str],
    entrypoints: Sequence[str],
) -> dict[str, object]:
    if not _RELEASE_ID.fullmatch(release_id):
        raise AssemblyError(f"invalid release id: {release_id!r}")
    if not modules:
        raise AssemblyError("at least one --module is required")
    if not entrypoints:
        raise AssemblyError("at least one --entrypoint is required")

    archive = archive.resolve(strict=True)
    app_root = app_root.resolve(strict=True)
    archive_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
    releases_dir = app_root / "releases"
    releases_dir.mkdir(parents=True, exist_ok=True)
    release_dir = releases_dir / release_id
    if release_dir.exists() or release_dir.is_symlink():
        raise AssemblyError(f"release already exists: {release_dir}")

    release_dir.mkdir()
    promoted = False
    try:
        _extract_archive(archive, release_dir)
        venv_dir = release_dir / ".venv"
        if venv_dir.exists() or venv_dir.is_symlink():
            raise AssemblyError("source archive must not contain a .venv")
        _run([bootstrap_python, "-m", "venv", str(venv_dir)])
        bin_dir = _venv_bin(venv_dir)
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        install_target = str(release_dir)
        if extras:
            install_target += f"[{','.join(extras)}]"
        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "--editable",
                install_target,
            ],
            cwd=release_dir,
        )

        verified_entrypoints = _verify_entrypoints(bin_dir, python, entrypoints)
        verified_modules = _verify_modules(python, release_dir, modules)
        current = app_root / "current"
        previous = _promote(current, release_dir)
        promoted = True
        return {
            "ok": True,
            "release": str(release_dir),
            "current": str(current),
            "previous": previous,
            "archive_sha256": archive_sha256,
            "python": str(python),
            "entrypoints": verified_entrypoints,
            "modules": verified_modules,
        }
    finally:
        if not promoted:
            shutil.rmtree(release_dir, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--app-root", required=True, type=Path)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--python", default=sys.executable, dest="bootstrap_python")
    parser.add_argument("--extra", action="append", default=[], dest="extras")
    parser.add_argument("--module", action="append", default=[], dest="modules")
    parser.add_argument("--entrypoint", action="append", default=[], dest="entrypoints")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        receipt = assemble_release(
            archive=args.archive,
            app_root=args.app_root,
            release_id=args.release_id,
            bootstrap_python=args.bootstrap_python,
            extras=args.extras,
            modules=args.modules,
            entrypoints=args.entrypoints,
        )
    except (AssemblyError, OSError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
