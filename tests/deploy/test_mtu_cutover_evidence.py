"""Cutover-evidence assembly tests.

Every fixture is synthetic or a tmp copy. No test reads or writes the real
``~/.hermes-mtu`` or ``~/pcl-run/hermes-mtu``.
"""
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MTU_ROOT = ROOT / "deploy/finexis/mtu"
SCRIPTS = MTU_ROOT / "scripts"
BASELINE = MTU_ROOT / "evidence/mtu-eval-replay-2026-08-02.json"
CORPUS = MTU_ROOT / "evals/mtu-eval-corpus-v1.json"
POLICY = MTU_ROOT / "eval-policy.yaml"
for path in (str(ROOT), str(SCRIPTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

from mtu_eval_policy import canonical_digest  # noqa: E402
from hermes_cli.pa_compose import compose_pa_constitution  # noqa: E402


def _module():
    path = SCRIPTS / "assemble_cutover_evidence.py"
    spec = importlib.util.spec_from_file_location("assemble_cutover_evidence", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


KIT = _module()


def _composed_bytes(tmp_path: Path) -> bytes:
    work = tmp_path / "compose-ref"
    work.mkdir(parents=True, exist_ok=True)
    compose_pa_constitution(
        MTU_ROOT, work / "c.yaml", work / "m.json", allow_unverified=True
    )
    return (work / "c.yaml").read_bytes()


def _live_home(tmp_path: Path, constitution: bytes) -> Path:
    live = tmp_path / "fake-live-home"
    live.mkdir(parents=True, exist_ok=True)
    (live / "mtu_constitution.yaml").write_bytes(constitution)
    shutil.copy2(MTU_ROOT / "config.yaml", live / "config.yaml")
    return live


def _green_report(constitution_sha: str) -> dict:
    """Baseline report, judge-resolved and canary-clean, bound to ``constitution_sha``.

    The committed baseline predates later corpus amendments (an eval case's
    expectation kind/text can change without a fresh replay existing yet), so
    assertions are re-labelled from the CURRENT corpus declarations by label
    before statuses are normalized — the fixture is corpus-consistent by
    construction rather than frozen to the baseline's corpus.
    """
    report = json.loads(BASELINE.read_text())
    corpus = json.loads(CORPUS.read_text())
    declared = {
        str(case.get("case_id")): {
            str(item.get("label")): item for item in case.get("expected") or []
        }
        for case in corpus.get("cases") or []
    }
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    report["corpus"]["source_digest"] = canonical_digest(corpus)
    report["execution"]["runtime"]["source_constitution_sha256"] = constitution_sha
    report["execution"]["runtime"]["copied_constitution_sha256"] = constitution_sha
    for case in report["cases"]:
        expected_by_label = declared.get(str(case.get("case_id")), {})
        for turn in case["turns"]:
            for assertion in turn["assertions"]:
                current = expected_by_label.get(str(assertion.get("label")))
                if current is not None:
                    assertion["kind"] = current.get("kind", assertion["kind"])
                    if "text" in current:
                        assertion["text"] = current["text"]
                    elif "text" in assertion and current.get("kind") not in {
                        "exact_present", "exact_absent",
                    }:
                        assertion.pop("text", None)
                if assertion["kind"] in {"must", "must_not"}:
                    assertion.update(
                        status="passed", passed=True, review_needed=False,
                        judge_why="fixture semantic pass",
                    )
                elif case.get("canary") and assertion.get("passed") is False:
                    assertion.update(status="passed", passed=True)
    return report


def _nightly(tmp_path: Path, status: str = "green") -> Path:
    path = tmp_path / "nightly-latest.json"
    path.write_text(json.dumps({
        "status": status,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "regression": {"status": status, "new_failure_count": 0},
    }))
    return path


def _write_report(tmp_path: Path, report: dict, name: str = "report.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(report))
    return path


def _snapshot(root: Path) -> dict:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def _assemble(tmp_path: Path, live: Path, report_path: Path, **overrides):
    kwargs = dict(
        live_home=live, report_path=report_path, out_dir=tmp_path / "out",
        mtu_root=MTU_ROOT, policy_path=POLICY, baseline_path=BASELINE, corpus_path=CORPUS,
        nightly_path=_nightly(tmp_path), no_nightly=False, allow_unverified=True,
        require_judge=False, require_full_corpus=True, max_age_hours=None,
    )
    kwargs.update(overrides)
    return KIT.assemble(**kwargs)


def _gate(evidence: dict, name: str) -> dict:
    return next(item for item in evidence["gates"] if item["name"] == name)


# --------------------------------------------------------------------------


def test_digest_match_package_is_green(tmp_path):
    composed = _composed_bytes(tmp_path)
    live = _live_home(tmp_path, composed)
    sha = hashlib.sha256(composed).hexdigest()
    report = _write_report(tmp_path, _green_report(sha))
    evidence, out = _assemble(tmp_path, live, report)

    assert evidence["verdict"]["ok"], evidence["verdict"]["failed_gates"]
    assert evidence["verdict"]["failed_gates"] == []
    parity = _gate(evidence, "constitution_digest_parity")
    assert parity["status"] == "green"
    assert parity["raw_match"] is True and parity["normalized_match"] is True
    assert _gate(evidence, "replay_constitution_binding")["status"] == "green"
    assert _gate(evidence, "replay_canaries")["canary_case_count"] == 10
    assert (out / "evidence.json").is_file() and (out / "evidence.md").is_file()
    text = (out / "evidence.md").read_text()
    assert "GREEN — cutover evidence complete" in text
    assert sha in text
    assert "43 selected / 43 declared" in text


def test_whitespace_only_difference_still_parity(tmp_path):
    composed = _composed_bytes(tmp_path)
    live = _live_home(tmp_path, composed.replace(b"\n", b"   \n", 1) + b"\n\n")
    report = _write_report(tmp_path, _green_report(hashlib.sha256(composed).hexdigest()))
    evidence, _ = _assemble(tmp_path, live, report)
    parity = _gate(evidence, "constitution_digest_parity")
    assert parity["raw_match"] is False
    assert parity["normalized_match"] is True and parity["status"] == "green"
    assert evidence["verdict"]["ok"]


def test_digest_mismatch_fails_and_is_named(tmp_path):
    composed = _composed_bytes(tmp_path)
    live = _live_home(tmp_path, composed + b"drifted_key: live_only\n")
    report = _write_report(tmp_path, _green_report(hashlib.sha256(composed).hexdigest()))
    evidence, out = _assemble(tmp_path, live, report)

    assert evidence["verdict"]["ok"] is False
    assert "constitution_digest_parity" in evidence["verdict"]["failed_gates"]
    parity = _gate(evidence, "constitution_digest_parity")
    assert parity["normalized_match"] is False and parity["diff_line_count"] > 0
    text = (out / "evidence.md").read_text()
    assert "RED — cutover evidence INCOMPLETE" in text
    assert "`constitution_digest_parity`" in text


def test_report_constitution_mismatch_fails_closed(tmp_path):
    composed = _composed_bytes(tmp_path)
    live = _live_home(tmp_path, composed)
    report = _green_report("0" * 64)
    evidence, out = _assemble(tmp_path, live, _write_report(tmp_path, report))

    assert evidence["verdict"]["ok"] is False
    binding = _gate(evidence, "replay_constitution_binding")
    assert binding["status"] == "red"
    assert "0" * 64 in binding["error"]
    assert "replay_constitution_binding" in evidence["verdict"]["failed_gates"]
    # digest parity itself is still green — the failure is the report's binding, named separately
    assert _gate(evidence, "constitution_digest_parity")["status"] == "green"
    assert "replay_constitution_binding" in (out / "evidence.md").read_text()


def test_corpus_binding_mismatch_fails(tmp_path):
    composed = _composed_bytes(tmp_path)
    live = _live_home(tmp_path, composed)
    report = _green_report(hashlib.sha256(composed).hexdigest())
    report["corpus"]["source_digest"] = "sha256:" + "1" * 64
    evidence, _ = _assemble(tmp_path, live, _write_report(tmp_path, report))
    assert "replay_corpus_binding" in evidence["verdict"]["failed_gates"]


def test_new_deterministic_failure_fails_regression(tmp_path):
    composed = _composed_bytes(tmp_path)
    live = _live_home(tmp_path, composed)
    report = _green_report(hashlib.sha256(composed).hexdigest())
    for case in report["cases"]:
        for turn in case["turns"]:
            for assertion in turn["assertions"]:
                if assertion["kind"] in {"exact_present", "exact_absent"} and assertion["passed"]:
                    assertion.update(passed=False, status="failed")
                    break
            else:
                continue
            break
        else:
            continue
        break
    evidence, _ = _assemble(tmp_path, live, _write_report(tmp_path, report))
    regression = _gate(evidence, "replay_regression")
    assert regression["status"] == "red" and regression["new_failure_count"] >= 1
    assert "replay_regression" in evidence["verdict"]["failed_gates"]


def test_compose_refusal_is_recorded_not_silently_escaped(tmp_path):
    """Without --allow-unverified the compose refusal must SURFACE in the package."""
    composed = _composed_bytes(tmp_path)
    live = _live_home(tmp_path, composed)
    report = _write_report(tmp_path, _green_report(hashlib.sha256(composed).hexdigest()))
    evidence, out = _assemble(tmp_path, live, report, allow_unverified=False)

    compose = _gate(evidence, "compose_reproducible")
    assert compose["status"] == "red" and compose["compose_refused"] is True
    assert "unverified compliance artifacts refuse composition" in compose["error"]
    assert evidence["inputs"]["allow_unverified"] is False
    assert evidence["verdict"]["ok"] is False
    assert "Compose REFUSED" in (out / "evidence.md").read_text()


def test_allow_unverified_is_recorded_in_output(tmp_path):
    composed = _composed_bytes(tmp_path)
    live = _live_home(tmp_path, composed)
    report = _write_report(tmp_path, _green_report(hashlib.sha256(composed).hexdigest()))
    evidence, out = _assemble(tmp_path, live, report, allow_unverified=True)

    compose = _gate(evidence, "compose_reproducible")
    assert compose["allow_unverified_requested"] is True
    assert compose["allow_unverified_recorded"] is True
    assert compose["unverified_compliance_count"] == len(compose["unverified_compliance"]) >= 1
    assert evidence["inputs"]["allow_unverified"] is True
    text = (out / "evidence.md").read_text()
    assert "`--allow-unverified` USED and recorded" in text
    for item in compose["unverified_compliance"]:
        assert item in text


def test_absent_nightly_fails_closed_and_waiver_is_recorded(tmp_path):
    composed = _composed_bytes(tmp_path)
    live = _live_home(tmp_path, composed)
    report = _write_report(tmp_path, _green_report(hashlib.sha256(composed).hexdigest()))

    evidence, _ = _assemble(
        tmp_path, live, report, nightly_path=tmp_path / "no-such-nightly.json"
    )
    assert "nightly_green" in evidence["verdict"]["failed_gates"]

    red = tmp_path / "red-nightly.json"
    red.write_text(json.dumps(
        {"status": "red", "recorded_at": datetime.now(timezone.utc).isoformat()}
    ))
    evidence, _ = _assemble(tmp_path, live, report, nightly_path=red)
    assert "nightly_green" in evidence["verdict"]["failed_gates"]

    evidence, _ = _assemble(tmp_path, live, report, no_nightly=True, nightly_path=None)
    assert _gate(evidence, "nightly_green")["status"] == "waived"
    assert evidence["verdict"]["ok"] is True


def test_stale_report_fails_freshness(tmp_path):
    composed = _composed_bytes(tmp_path)
    live = _live_home(tmp_path, composed)
    report = _green_report(hashlib.sha256(composed).hexdigest())
    report["generated_at"] = "2026-01-01T00:00:00+00:00"
    evidence, _ = _assemble(tmp_path, live, _write_report(tmp_path, report))
    assert "replay_freshness" in evidence["verdict"]["failed_gates"]


def test_write_into_live_home_is_refused(tmp_path):
    composed = _composed_bytes(tmp_path)
    live = _live_home(tmp_path, composed)
    report = _write_report(tmp_path, _green_report(hashlib.sha256(composed).hexdigest()))
    before = _snapshot(live)

    with pytest.raises(KIT.CutoverRefused) as excinfo:
        _assemble(tmp_path, live, report, out_dir=live / "evidence")
    assert "read-only live home" in str(excinfo.value)

    with pytest.raises(KIT.CutoverRefused):
        _assemble(tmp_path, live, report, out_dir=live)

    assert _snapshot(live) == before
    assert not (live / "evidence").exists()


def test_live_home_is_never_written_on_a_full_run(tmp_path):
    composed = _composed_bytes(tmp_path)
    live = _live_home(tmp_path, composed)
    report = _write_report(tmp_path, _green_report(hashlib.sha256(composed).hexdigest()))
    before = _snapshot(live)
    before_mtimes = {p.name: p.stat().st_mtime_ns for p in live.iterdir()}

    evidence, _ = _assemble(tmp_path, live, report)

    assert _snapshot(live) == before
    assert {p.name: p.stat().st_mtime_ns for p in live.iterdir()} == before_mtimes
    assert list(live.iterdir()) and evidence["inputs"]["live_home_written"] is False


def test_config_difference_is_reported_not_gating(tmp_path):
    composed = _composed_bytes(tmp_path)
    live = _live_home(tmp_path, composed)
    (live / "config.yaml").write_text(
        (MTU_ROOT / "config.yaml").read_text() + "\nlive_only_key: 1\n"
    )
    report = _write_report(tmp_path, _green_report(hashlib.sha256(composed).hexdigest()))
    evidence, out = _assemble(tmp_path, live, report)

    config = _gate(evidence, "config_digest_parity")
    assert config["gating"] is False and config["status"] == "differs"
    assert config["diff_line_count"] > 0
    assert evidence["verdict"]["ok"] is True
    assert "live_only_key" in (out / "evidence.md").read_text()


def test_cli_exit_codes(tmp_path):
    composed = _composed_bytes(tmp_path)
    live = _live_home(tmp_path, composed)
    report = _write_report(tmp_path, _green_report(hashlib.sha256(composed).hexdigest()))
    nightly = _nightly(tmp_path)

    def _run(report_path: Path, out_dir: Path):
        return subprocess.run(
            [
                sys.executable, str(SCRIPTS / "assemble_cutover_evidence.py"),
                "--live-home", str(live), "--report", str(report_path),
                "--nightly", str(nightly), "--allow-unverified",
                "--out-dir", str(out_dir),
            ],
            text=True, capture_output=True, check=False,
        )

    green = _run(report, tmp_path / "cli-green")
    assert green.returncode == 0, green.stdout + green.stderr
    assert json.loads(green.stdout)["ok"] is True

    bad_report = _write_report(tmp_path, _green_report("0" * 64), "bad.json")
    red = _run(bad_report, tmp_path / "cli-red")
    assert red.returncode == 1
    assert "replay_constitution_binding" in json.loads(red.stdout)["failed_gates"]

    refused = _run(report, live / "evidence")
    assert refused.returncode == 2
    assert json.loads(refused.stdout)["code"] == "MTU_CUTOVER_EVIDENCE_REFUSED"
    assert not (live / "evidence").exists()
