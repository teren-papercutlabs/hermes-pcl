import importlib.util
import os
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "deploy/finexis/mtu/scripts/run_eval_corpus.py"
)


@pytest.fixture(autouse=True)
def _restore_eval_mode_env(monkeypatch):
    """_stage_runtime stamps HERMES_EVAL_MODE into os.environ (correct for the
    dedicated runner process); without this, the stamp leaks into every later
    test in the same pytest worker — CI's background-review suite saw eval
    mode ON and asserted the review spawn that eval mode suppresses."""
    monkeypatch.setenv("HERMES_EVAL_MODE", "0")
    yield


def _module():
    spec = importlib.util.spec_from_file_location("mtu_eval_runner", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_stage_runtime_rewrites_constitution_to_disposable_home(tmp_path):
    module = _module()
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: test-model\n"
        "pa:\n  enabled: true\n  constitution_path: /live/constitution.yaml\n"
    )
    (source / "mtu_constitution.yaml").write_text(
        "id: test\n"
        "agent_name: Test\n"
        "identity: {role: test}\n"
        "client: {name: test}\n"
        "job_briefs:\n"
        "  default:\n"
        "    title: Test\n"
        "    purpose: Test\n"
        "    knowledge: [foo.txt]\n"
    )
    (source / "foo.txt").write_text("knowledge\n")
    (source / "SOUL.md").write_text("test\n")
    (source / ".env").write_text("OPENAI_API_KEY=test-only\n")
    target = tmp_path / "copy"

    manifest = module._stage_runtime(source, target)

    assert str(target / "mtu_constitution.yaml") in (target / "config.yaml").read_text()
    assert "/live/constitution.yaml" not in (target / "config.yaml").read_text()
    assert manifest["mode"] == "disposable_copy"
    assert manifest["live_home_written"] is False
    assert oct((target / ".env").stat().st_mode & 0o777) == "0o600"


def test_stage_runtime_overlays_candidate_but_borrows_only_live_secrets(tmp_path):
    module = _module()
    source = tmp_path / "source"
    source.mkdir()
    constitution = (
        "id: test\n"
        "agent_name: Test\n"
        "identity: {role: test}\n"
        "client: {name: %s}\n"
        "job_briefs:\n"
        "  default:\n"
        "    title: Test\n"
        "    purpose: Test\n"
        "    knowledge: [foo.txt]\n"
    )
    for name, content in {
        "config.yaml": "model:\n  default: installed\npa: {}\n",
        "mtu_constitution.yaml": constitution % "installed",
        "SOUL.md": "installed\n",
        ".env": "OPENAI_API_KEY=test-only\n",
        "foo.txt": "knowledge\n",
    }.items():
        (source / name).write_text(content)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    for name, content in {
        "config.yaml": "model:\n  default: candidate\npa: {}\n",
        "mtu_constitution.yaml": constitution % "candidate",
        "SOUL.md": "candidate\n",
        # Knowledge is part of what a candidate IS (compliance artifacts,
        # case-record config), so it is synced from the candidate tree, never
        # from the installed runtime.
        "foo.txt": "candidate knowledge\n",
    }.items():
        (candidate / name).write_text(content)

    target = tmp_path / "copy"
    manifest = module._stage_runtime(source, target, candidate)

    assert "candidate" in (target / "config.yaml").read_text()
    assert "name: candidate" in (target / "mtu_constitution.yaml").read_text()
    assert (target / "SOUL.md").read_text() == "candidate\n"
    assert (target / ".env").read_text() == "OPENAI_API_KEY=test-only\n"
    assert manifest["candidate_deploy_dir"] == str(candidate)
    assert (target / "knowledge/foo.txt").read_text() == "candidate knowledge\n"


def test_stage_runtime_refuses_live_mtu_target(tmp_path, monkeypatch):
    module = _module()
    source = tmp_path / "source"
    source.mkdir()
    for name, content in {
        "config.yaml": "pa: {}\n",
        "mtu_constitution.yaml": "version: 1\n",
        "SOUL.md": "test\n",
        ".env": "OPENAI_API_KEY=test-only\n",
    }.items():
        (source / name).write_text(content)

    with pytest.raises(ValueError, match="must not be ~/.hermes-mtu"):
        module._stage_runtime(source, module.LIVE_MTU_HOME)


def test_deterministic_failures_make_runner_fail_closed():
    module = _module()
    assert module._deterministic_exit_code({
        "deterministic_summary": {"failed": 0},
    }) == 0
    assert module._deterministic_exit_code({
        "deterministic_summary": {"failed": 1},
    }) == 1


def test_deploy_guarded_repo_root_resolves_hermes_cli():
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "deploy/finexis/mtu/scripts/deploy_guarded.py"
    spec = importlib.util.spec_from_file_location("dg_reporoot_check", script)
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.path.insert(0, str(script.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(script.parent))
    assert (module.REPO_ROOT / "hermes_cli").is_dir(), (
        f"REPO_ROOT must be the repo root (cwd-independent import of hermes_cli); got {module.REPO_ROOT}"
    )


def test_stage_runtime_sources_knowledge_from_candidate_not_live_home(tmp_path):
    """Pre-cutover live home has no knowledge/; the candidate tree under test does."""
    module = _module()
    constitution = (
        "id: test\n"
        "agent_name: Test\n"
        "identity: {role: test}\n"
        "client: {name: test}\n"
        "job_briefs:\n"
        "  default:\n"
        "    title: Test\n"
        "    purpose: Test\n"
        "    knowledge: [foo.txt]\n"
    )
    source = tmp_path / "live-home"
    source.mkdir()
    for name, content in {
        "config.yaml": "model:\n  default: installed\npa: {}\n",
        "mtu_constitution.yaml": constitution,
        "SOUL.md": "installed\n",
        ".env": "OPENAI_API_KEY=test-only\n",
    }.items():
        (source / name).write_text(content)
    # deliberately NO knowledge/ dir in the live home
    candidate = tmp_path / "candidate"
    (candidate / "knowledge").mkdir(parents=True)
    for name, content in {
        "config.yaml": "model:\n  default: candidate\npa: {}\n",
        "mtu_constitution.yaml": constitution,
        "SOUL.md": "candidate\n",
    }.items():
        (candidate / name).write_text(content)
    (candidate / "knowledge" / "foo.txt").write_text("candidate knowledge\n")

    target = tmp_path / "copy"
    module._stage_runtime(source, target, candidate)

    assert (target / "knowledge" / "foo.txt").read_text() == "candidate knowledge\n"


def test_stage_runtime_turns_on_eval_mode(tmp_path, monkeypatch):
    """A replay must not let the agent rewrite itself mid-corpus.

    The 2026-08-03 nightly created a 'bor-drafting' skill during the run, so
    every case after it scored an agent the deploy tree does not contain.
    """
    import yaml

    from agent.eval_mode import EVAL_MODE_ENV, self_modification_allowed

    monkeypatch.delenv(EVAL_MODE_ENV, raising=False)
    assert self_modification_allowed() is True

    module = _module()
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: test-model\n"
        "pa:\n  enabled: true\n  constitution_path: /live/constitution.yaml\n"
    )
    (source / "mtu_constitution.yaml").write_text(
        "id: test\n"
        "agent_name: Test\n"
        "identity: {role: test}\n"
        "client: {name: test}\n"
        "job_briefs:\n"
        "  default:\n"
        "    title: Test\n"
        "    purpose: Test\n"
        "    knowledge: [foo.txt]\n"
    )
    (source / "foo.txt").write_text("knowledge\n")
    (source / "SOUL.md").write_text("test\n")
    (source / ".env").write_text("OPENAI_API_KEY=test-only\n")

    manifest = module._stage_runtime(source, tmp_path / "copy")

    staged = yaml.safe_load((tmp_path / "copy" / "config.yaml").read_text())
    assert staged["agent"]["eval_mode"] is True
    assert manifest["eval_mode"] is True
    assert os.environ[EVAL_MODE_ENV] == "1"
    assert self_modification_allowed() is False
