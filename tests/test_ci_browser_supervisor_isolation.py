"""Keep stateful browser-supervisor modules out of the parallel unit job."""

from pathlib import Path


SUPERVISOR_MODULES = (
    "tests/tools/test_browser_supervisor.py",
    "tests/tools/test_browser_eval_supervisor_path.py",
)


def test_browser_supervisor_modules_are_isolated_in_serial_ci_job():
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tests.yml"
    ).read_text(encoding="utf-8")
    unit_job, browser_job = workflow.split("  browser-supervisor:", maxsplit=1)
    browser_job = browser_job.split("\n  e2e:", maxsplit=1)[0]

    for module in SUPERVISOR_MODULES:
        assert f"--ignore={module}" in unit_job
        assert module in browser_job

    assert "-n auto" in unit_job
    assert "-n 0" in browser_job
    assert "for attempt in 1 2" in browser_job
    assert browser_job.index(SUPERVISOR_MODULES[0]) < browser_job.index(
        SUPERVISOR_MODULES[1]
    )
