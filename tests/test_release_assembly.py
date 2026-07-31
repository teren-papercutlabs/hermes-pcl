from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import subprocess
import sys
import tarfile
import textwrap


REPO_ROOT = Path(__file__).resolve().parents[1]
ASSEMBLER = REPO_ROOT / "scripts" / "deploy" / "assemble_release.py"


def _load_assembler():
    spec = importlib.util.spec_from_file_location("assemble_release", ASSEMBLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture_archive(tmp_path: Path, *, include_stale_venv: bool = False) -> Path:
    source = tmp_path / "fixture-source"
    package = source / "fixturepkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 'fixture'\n", encoding="utf-8")
    (package / "cli.py").write_text(
        "from pathlib import Path\n"
        "def main():\n"
        "    print(Path(__file__).resolve())\n",
        encoding="utf-8",
    )
    (source / "setup.py").write_text(
        textwrap.dedent(
            """
            from setuptools import find_packages, setup

            setup(
                name="release-binding-fixture",
                version="1.0.0",
                packages=find_packages(),
                entry_points={"console_scripts": ["fixture-cli=fixturepkg.cli:main"]},
            )
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    if include_stale_venv:
        stale = source / ".venv" / "bin"
        stale.mkdir(parents=True)
        (stale / "fixture-cli").write_text("#!/old/release/.venv/bin/python\n")
    archive = tmp_path / "fixture.tar"
    with tarfile.open(archive, "w") as bundle:
        for path in source.rglob("*"):
            bundle.add(path, arcname=path.relative_to(source))
    return archive


def _assemble(archive: Path, app_root: Path, release_id: str) -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            str(ASSEMBLER),
            "--archive",
            str(archive),
            "--app-root",
            str(app_root),
            "--release-id",
            release_id,
            "--python",
            sys.executable,
            "--module",
            "fixturepkg",
            "--entrypoint",
            "fixture-cli",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_release_path_change_rebinds_entrypoint_and_modules(tmp_path: Path) -> None:
    archive = _fixture_archive(tmp_path)
    app_root = tmp_path / "app"
    app_root.mkdir()

    first = _assemble(archive, app_root, "release-a")
    second = _assemble(archive, app_root, "release-b")

    release_a = Path(str(first["release"]))
    release_b = Path(str(second["release"]))
    current = app_root / "current"
    assert current.resolve() == release_b
    assert second["previous"] == str(release_a)

    entrypoint = release_b / ".venv" / "bin" / "fixture-cli"
    shebang = entrypoint.read_text(encoding="utf-8").splitlines()[0]
    assert shebang == f"#!{release_b / '.venv' / 'bin' / 'python'}"
    assert str(release_a) not in shebang

    executed = subprocess.run([str(entrypoint)], check=True, capture_output=True, text=True)
    loaded_path = Path(executed.stdout.strip())
    assert loaded_path.is_relative_to(release_b)
    assert not loaded_path.is_relative_to(release_a)

    module_path = Path(str(second["modules"]["fixturepkg"]))
    assert module_path.is_relative_to(release_b)


def test_failed_candidate_does_not_move_current(tmp_path: Path) -> None:
    archive = _fixture_archive(tmp_path)
    app_root = tmp_path / "app"
    app_root.mkdir()
    first = _assemble(archive, app_root, "release-a")

    failed = subprocess.run(
        [
            sys.executable,
            str(ASSEMBLER),
            "--archive",
            str(archive),
            "--app-root",
            str(app_root),
            "--release-id",
            "release-b",
            "--python",
            sys.executable,
            "--module",
            "fixturepkg",
            "--entrypoint",
            "missing-cli",
        ],
        capture_output=True,
        text=True,
    )

    assert failed.returncode == 1
    assert (app_root / "current").resolve() == Path(str(first["release"]))
    assert not (app_root / "releases" / "release-b").exists()


def test_stale_release_entrypoint_is_rejected(tmp_path: Path) -> None:
    assembler = _load_assembler()
    old_python = tmp_path / "release-a" / ".venv" / "bin" / "python"
    bin_dir = tmp_path / "release-b" / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    entrypoint = bin_dir / "fixture-cli"
    entrypoint.write_text(f"#!{old_python}\n", encoding="utf-8")

    try:
        assembler._verify_entrypoints(bin_dir, bin_dir / "python", ["fixture-cli"])
    except assembler.AssemblyError as exc:
        assert str(old_python) in str(exc)
    else:
        raise AssertionError("stale release interpreter was accepted")


def test_environment_module_copy_is_rejected(tmp_path: Path) -> None:
    assembler = _load_assembler()
    release = tmp_path / "release"
    release.mkdir()
    venv = release / ".venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    python = venv / "bin" / "python"
    site_packages = subprocess.run(
        [
            str(python),
            "-c",
            "import site; print(site.getsitepackages()[0])",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (Path(site_packages) / "environment_only.py").write_text(
        "VALUE = 'environment copy'\n",
        encoding="utf-8",
    )

    try:
        assembler._verify_modules(python, release, ["environment_only"])
    except assembler.AssemblyError as exc:
        assert "environment copy" in str(exc)
    else:
        raise AssertionError("module copied into the release environment was accepted")


def test_archive_with_virtual_environment_is_rejected(tmp_path: Path) -> None:
    archive = _fixture_archive(tmp_path, include_stale_venv=True)
    app_root = tmp_path / "app"
    app_root.mkdir()

    failed = subprocess.run(
        [
            sys.executable,
            str(ASSEMBLER),
            "--archive",
            str(archive),
            "--app-root",
            str(app_root),
            "--release-id",
            "release-a",
            "--python",
            sys.executable,
            "--module",
            "fixturepkg",
            "--entrypoint",
            "fixture-cli",
        ],
        capture_output=True,
        text=True,
    )

    assert failed.returncode == 1
    assert "source archive must not contain a .venv" in failed.stderr
    assert not (app_root / "current").exists()
    assert not (app_root / "releases" / "release-a").exists()
