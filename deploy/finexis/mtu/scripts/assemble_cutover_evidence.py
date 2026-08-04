#!/usr/bin/env python3
"""Assemble the MTU cutover evidence package.

One command produces the package teren pre-authorized live cutover on:
composed-constitution digest parity against the live home, plus a replay
report validated exactly the way the deploy gate validates it.

The live home is READ-ONLY here. Every write path is checked against it
before anything is written; a write path that resolves inside the live home
refuses the whole run.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MTU_ROOT = Path(__file__).resolve().parents[1]
# parents: [0] scripts, [1] mtu, [2] finexis, [3] deploy, [4] repo root.
# (deploy_guarded.py's own REPO_ROOT uses parents[3]; it resolves hermes_cli only
# because callers run from the repo root. This script does not rely on cwd.)
REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = Path(__file__).resolve().parent
TEMPLATE = SCRIPTS_DIR / "cutover-evidence.md.tmpl"

for _path in (str(REPO_ROOT), str(SCRIPTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from mtu_eval_policy import (  # noqa: E402
    DEFAULT_POLICY, canonical_digest, compare_baseline, load_policy, parse_time,
    read_json, validate_report_shape,
)
from deploy_guarded import _canaries_clean  # noqa: E402  (import, never duplicate)
from hermes_cli.pa_compose import PaComposeError, compose_pa_constitution  # noqa: E402

DIFF_PREVIEW_LINES = 40


class CutoverRefused(RuntimeError):
    """Raised when the run cannot be attempted safely at all."""


# --------------------------------------------------------------------------
# live-home protection
# --------------------------------------------------------------------------

def _resolve_live_home(raw: Path) -> Path:
    live = Path(raw).expanduser().resolve()
    if not live.is_dir():
        raise CutoverRefused(f"live home is not a directory: {live}")
    return live


def assert_outside_live_home(candidate: Path, live_home: Path, *, label: str) -> Path:
    """Refuse any write target that resolves into (or onto) the live home."""
    resolved = Path(candidate).expanduser()
    resolved = (resolved if resolved.is_absolute() else Path.cwd() / resolved).resolve()
    if resolved == live_home or live_home in resolved.parents:
        raise CutoverRefused(
            f"{label} resolves inside the read-only live home "
            f"({resolved} under {live_home}); the cutover kit never writes there"
        )
    return resolved


# --------------------------------------------------------------------------
# digest helpers
# --------------------------------------------------------------------------

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_whitespace(data: bytes) -> bytes:
    """S1 parity normalization: strip per-line trailing whitespace and edge blank lines."""
    text = data.decode("utf-8", errors="replace")
    lines = [line.rstrip() for line in text.splitlines()]
    return ("\n".join(lines).strip("\n") + "\n").encode("utf-8")


def _diff_summary(left: bytes, right: bytes, left_name: str, right_name: str) -> dict[str, Any]:
    lines = list(difflib.unified_diff(
        left.decode("utf-8", errors="replace").splitlines(),
        right.decode("utf-8", errors="replace").splitlines(),
        fromfile=left_name, tofile=right_name, lineterm="",
    ))
    return {
        "diff_line_count": len(lines),
        "diff_preview": lines[:DIFF_PREVIEW_LINES],
        "diff_truncated": len(lines) > DIFF_PREVIEW_LINES,
    }


def _section(name: str, status: str, *, gating: bool = True, **detail: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "gating": gating, **detail}


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------

def compose_section(
    *, mtu_root: Path, work_dir: Path, allow_unverified: bool
) -> tuple[dict[str, Any], bytes | None]:
    output = work_dir / "composed_constitution.yaml"
    manifest = work_dir / "composed_manifest.json"
    try:
        result = compose_pa_constitution(
            mtu_root, output, manifest, allow_unverified=allow_unverified
        )
    except PaComposeError as exc:
        return (
            _section(
                "compose_reproducible", "red",
                allow_unverified_requested=allow_unverified,
                compose_refused=True, error=str(exc),
            ),
            None,
        )
    composed = output.read_bytes()
    unverified = list(result.get("unverified_compliance") or [])
    return (
        _section(
            "compose_reproducible", "green",
            allow_unverified_requested=allow_unverified,
            allow_unverified_recorded=bool(result.get("allow_unverified")),
            compose_refused=False,
            unverified_compliance=unverified,
            unverified_compliance_count=len(unverified),
            source_count=result.get("source_count"),
            composed_source_count=result.get("composed_source_count"),
            sources_sha256=result.get("sources_sha256"),
            composed_sha256=result.get("constitution_sha256"),
            manifest_path=str(manifest),
        ),
        composed,
    )


def constitution_digest_section(
    *, composed: bytes | None, live_home: Path
) -> tuple[dict[str, Any], str | None]:
    live_path = live_home / "mtu_constitution.yaml"
    if composed is None:
        return _section(
            "constitution_digest_parity", "red",
            error="no composed constitution to compare (compose gate is red)",
            live_path=str(live_path),
        ), None
    if not live_path.is_file():
        return _section(
            "constitution_digest_parity", "red",
            error=f"live constitution is absent: {live_path}",
            composed_sha256=_sha256_bytes(composed),
        ), _sha256_bytes(composed)
    live = live_path.read_bytes()
    composed_raw = _sha256_bytes(composed)
    live_raw = _sha256_bytes(live)
    composed_norm_bytes = normalize_whitespace(composed)
    live_norm_bytes = normalize_whitespace(live)
    composed_norm = _sha256_bytes(composed_norm_bytes)
    live_norm = _sha256_bytes(live_norm_bytes)
    parity = composed_norm == live_norm
    detail: dict[str, Any] = {
        "live_path": str(live_path),
        "composed_sha256": composed_raw,
        "live_sha256": live_raw,
        "composed_normalized_sha256": composed_norm,
        "live_normalized_sha256": live_norm,
        "raw_match": composed_raw == live_raw,
        "normalized_match": parity,
        "standard": "S1 parity: composed output reproduces the live constitution, whitespace-normalized",
    }
    if not parity:
        detail.update(_diff_summary(
            composed_norm_bytes, live_norm_bytes, "composed (normalized)", "live (normalized)",
        ))
    return _section(
        "constitution_digest_parity", "green" if parity else "red", **detail
    ), composed_raw


def config_digest_section(*, mtu_root: Path, live_home: Path) -> dict[str, Any]:
    tree_path = mtu_root / "config.yaml"
    live_path = live_home / "config.yaml"
    detail: dict[str, Any] = {
        "deploy_tree_path": str(tree_path),
        "live_path": str(live_path),
        "note": "config differences are REPORTED, never gating; the executor eyeballs the diff",
    }
    if not tree_path.is_file() or not live_path.is_file():
        detail["error"] = "one or both config.yaml files are absent"
        return _section("config_digest_parity", "unknown", gating=False, **detail)
    tree = tree_path.read_bytes()
    live = live_path.read_bytes()
    detail.update({
        "deploy_tree_sha256": _sha256_bytes(tree),
        "live_sha256": _sha256_bytes(live),
        "raw_match": _sha256_bytes(tree) == _sha256_bytes(live),
        "normalized_match": _sha256_bytes(normalize_whitespace(tree))
        == _sha256_bytes(normalize_whitespace(live)),
    })
    if not detail["raw_match"]:
        detail.update(_diff_summary(live, tree, "live config.yaml", "deploy-tree config.yaml"))
    return _section(
        "config_digest_parity", "green" if detail["raw_match"] else "differs",
        gating=False, **detail,
    )


def replay_sections(
    *, report: dict[str, Any], report_path: Path, corpus: dict[str, Any], corpus_path: Path,
    baseline: dict[str, Any], baseline_path: Path, composed_sha256: str | None,
    require_judge: bool, require_full_corpus: bool, max_age_hours: float,
) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    runtime = (report.get("execution") or {}).get("runtime") or {}
    report_corpus = report.get("corpus") or {}

    # freshness
    try:
        age_hours = (
            datetime.now(timezone.utc) - parse_time(str(report["generated_at"]))
        ).total_seconds() / 3600
        fresh = age_hours <= max_age_hours
        sections.append(_section(
            "replay_freshness", "green" if fresh else "red",
            generated_at=report["generated_at"], age_hours=round(age_hours, 2),
            maximum_age_hours=max_age_hours,
        ))
    except Exception as exc:  # missing or unparseable generated_at
        sections.append(_section(
            "replay_freshness", "red",
            error=f"replay report has no usable generated_at: {exc}",
            generated_at=report.get("generated_at"),
            maximum_age_hours=max_age_hours,
        ))

    # corpus binding
    expected_corpus_digest = canonical_digest(corpus)
    actual_corpus_digest = report_corpus.get("source_digest")
    sections.append(_section(
        "replay_corpus_binding",
        "green" if actual_corpus_digest == expected_corpus_digest else "red",
        corpus_path=str(corpus_path),
        deploy_tree_corpus_digest=expected_corpus_digest,
        report_corpus_digest=actual_corpus_digest,
        declared_case_count=report_corpus.get("declared_case_count"),
        selected_case_count=report_corpus.get("selected_case_count"),
        selected_tags=sorted(str(tag) for tag in report_corpus.get("tags") or []),
    ))

    # constitution binding — fail closed on mismatch
    report_constitution = runtime.get("source_constitution_sha256")
    binding_ok = bool(composed_sha256) and report_constitution == composed_sha256
    binding: dict[str, Any] = {
        "composed_sha256": composed_sha256,
        "report_source_constitution_sha256": report_constitution,
        "report_copied_constitution_sha256": runtime.get("copied_constitution_sha256"),
        "runtime_mode": runtime.get("mode"),
        "live_home_written": runtime.get("live_home_written"),
    }
    if not binding_ok:
        binding["error"] = (
            "replay report ran under constitution "
            f"{report_constitution!r}, not the composed constitution {composed_sha256!r}; "
            "the package fails closed — re-run the replay against the composed constitution"
        )
    sections.append(_section(
        "replay_constitution_binding", "green" if binding_ok else "red", **binding
    ))

    # report shape
    defects = validate_report_shape(report, corpus, require_judge=require_judge)
    sections.append(_section(
        "replay_report_shape", "green" if not defects else "red",
        report_path=str(report_path), require_judge=require_judge,
        defect_count=len(defects), defects=defects[:20],
        defects_truncated=len(defects) > 20,
    ))

    # regression vs accepted baseline
    regression = compare_baseline(report, baseline, require_full_corpus=require_full_corpus)
    summary = report.get("deterministic_summary") or {}
    regression_detail = {key: value for key, value in regression.items() if key != "status"}
    sections.append(_section(
        "replay_regression", "green" if regression["status"] == "green" else "red",
        baseline_path=str(baseline_path), require_full_corpus=require_full_corpus,
        regression_status=regression["status"], deterministic_summary=summary,
        **regression_detail,
    ))

    # canaries
    canary_cases = [case for case in report.get("cases") or [] if case.get("canary")]
    clean = _canaries_clean(report)
    sections.append(_section(
        "replay_canaries", "green" if clean else "red",
        canary_case_count=len(canary_cases),
        canary_case_ids=sorted(str(case.get("case_id")) for case in canary_cases),
        note="a report with zero canary cases is RED — absent canaries are not clean canaries",
    ))

    # judge evidence (gating only when demanded)
    judge = report.get("judge_summary") or {}
    judge_ok = judge.get("status") == "passed"
    sections.append(_section(
        "judge_evidence",
        "green" if judge_ok else ("red" if require_judge else "not_gating"),
        gating=require_judge, judge_summary=judge,
        note="MTU judge calibration is pending; judge evidence is recorded, and gates only under --require-judge",
    ))
    return sections


def nightly_section(
    *, nightly_path: Path | None, waived: bool, max_age_hours: float
) -> dict[str, Any]:
    if waived:
        return _section(
            "nightly_green", "waived", gating=False,
            note="--no-nightly was passed; the executor accepted the package without nightly evidence",
        )
    if nightly_path is None or not nightly_path.is_file():
        return _section(
            "nightly_green", "red",
            error=f"nightly summary is absent: {nightly_path}",
            note="teren's pre-authorization names a green nightly; absent evidence fails closed",
        )
    nightly = read_json(nightly_path)
    try:
        age_hours = (
            datetime.now(timezone.utc) - parse_time(str(nightly.get("recorded_at") or ""))
        ).total_seconds() / 3600
    except Exception:
        age_hours = max_age_hours + 1
    green = nightly.get("status") == "green" and age_hours <= max_age_hours
    return _section(
        "nightly_green", "green" if green else "red",
        nightly_path=str(nightly_path), nightly_status=nightly.get("status"),
        recorded_at=nightly.get("recorded_at"), age_hours=round(age_hours, 2),
        maximum_age_hours=max_age_hours,
        regression=nightly.get("regression"),
    )


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _md_table(rows: list[list[str]], header: list[str], aligns: list[str]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(aligns) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _code(value: Any) -> str:
    return f"`{value}`" if value not in (None, "") else "_absent_"


def render_markdown(evidence: dict[str, Any]) -> str:
    template = TEMPLATE.read_text(encoding="utf-8")
    sections = evidence["gates"]
    compose = next(item for item in sections if item["name"] == "compose_reproducible")
    digest = next(item for item in sections if item["name"] == "constitution_digest_parity")
    config = next(item for item in sections if item["name"] == "config_digest_parity")
    binding = next(item for item in sections if item["name"] == "replay_constitution_binding")
    corpus = next(item for item in sections if item["name"] == "replay_corpus_binding")
    regression = next(item for item in sections if item["name"] == "replay_regression")
    canaries = next(item for item in sections if item["name"] == "replay_canaries")

    boundary = [
        f"- Assembled at: `{evidence['assembled_at']}` by `assemble_cutover_evidence.py`.",
        f"- Deploy tree: `{evidence['inputs']['mtu_root']}` at commit `{evidence['inputs']['deploy_tree_commit']}`.",
        f"- Live home (READ-ONLY, zero writes): `{evidence['inputs']['live_home']}`.",
        f"- Replay report: `{evidence['inputs']['report']}`.",
        f"- Accepted baseline: `{evidence['inputs']['baseline']}`.",
        f"- Corpus: `{evidence['inputs']['corpus']}`, canonical digest {_code(corpus.get('deploy_tree_corpus_digest'))}.",
        f"- Compose escape: `--allow-unverified` "
        + ("USED and recorded" if evidence["inputs"]["allow_unverified"] else "not used"),
    ]
    if compose.get("compose_refused"):
        boundary.append(
            f"- Compose REFUSED: {compose.get('error')} — this package does not silently pass the escape."
        )
    elif compose.get("unverified_compliance"):
        boundary.append(
            "- Unverified compliance artifacts carried under the recorded escape: "
            + ", ".join(f"`{item}`" for item in compose["unverified_compliance"])
            + f" ({compose['unverified_compliance_count']} of {compose['source_count']} typed sources)."
        )

    digest_rows = [
        ["composed constitution (raw)", _code(digest.get("composed_sha256"))],
        ["live constitution (raw)", _code(digest.get("live_sha256"))],
        ["composed constitution (normalized)", _code(digest.get("composed_normalized_sha256"))],
        ["live constitution (normalized)", _code(digest.get("live_normalized_sha256"))],
        ["replay report ran under", _code(binding.get("report_source_constitution_sha256"))],
        ["deploy-tree config.yaml", _code(config.get("deploy_tree_sha256"))],
        ["live config.yaml", _code(config.get("live_sha256"))],
    ]

    gate_rows = [
        [
            f"`{item['name']}`",
            "gating" if item.get("gating") else "reported",
            item["status"],
            str(item.get("error") or item.get("note") or "")[:160],
        ]
        for item in sections
    ]

    counts = evidence["counts"]
    populations = [
        f"- Typed sources composed: **{counts['composed_source_count']} of {counts['source_count']}** "
        f"({counts['unverified_compliance_count']} unverified compliance artifacts).",
        f"- Corpus cases: **{counts['selected_case_count']} selected / {counts['declared_case_count']} declared**.",
        f"- Deterministic assertions: **{counts['assertions_passed']} passed / {counts['assertions_failed']} failed "
        f"/ {counts['assertion_count']} total**.",
        f"- Deterministic failures vs accepted baseline: **{counts['current_failure_count']} current / "
        f"{counts['accepted_failure_count']} accepted / {counts['new_failure_count']} new**.",
        f"- Canary cases: **{counts['canary_case_count']}**, status **{canaries['status']}**.",
        f"- Gate sections: **{counts['gates_green']} green / {counts['gates_red']} red / "
        f"{counts['gates_total']} total** ({counts['gates_gating']} gating).",
    ]

    if evidence["verdict"]["ok"]:
        verdict = (
            "**GREEN — cutover evidence complete.** Every gating section passed: composed constitution "
            "reproduces the live constitution under the S1 parity standard, and the supplied replay report "
            "validates under the deploy gate's own checks against the composed constitution."
        )
    else:
        failed = ", ".join(f"`{name}`" for name in evidence["verdict"]["failed_gates"])
        verdict = (
            f"**RED — cutover evidence INCOMPLETE. Failed gates: {failed}.**\n\n"
            + "\n".join(
                f"- `{item['name']}`: {item.get('error') or item.get('note') or 'gate is not green'}"
                for item in sections
                if item.get("gating") and item["status"] != "green"
            )
        )

    if regression.get("new_failure_keys"):
        verdict += "\n\nNew deterministic failures:\n" + "\n".join(
            f"- `{key}`" for key in regression["new_failure_keys"][:20]
        )

    replacements = {
        "{{DATE}}": evidence["assembled_at"][:10],
        "{{RUN_BOUNDARY}}": "\n".join(boundary),
        "{{DIGESTS_TABLE}}": _md_table(digest_rows, ["Artifact", "sha256"], ["---", "---"]),
        "{{GATES_TABLE}}": _md_table(
            gate_rows, ["Gate", "Role", "Status", "Note"], ["---", "---", "---", "---"]
        ),
        "{{POPULATIONS}}": "\n".join(populations),
        "{{VERDICT}}": verdict,
        "{{CONFIG_NOTE}}": _config_note(config),
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


def _config_note(config: dict[str, Any]) -> str:
    if config["status"] == "green":
        return "Deploy-tree `config.yaml` and live `config.yaml` are byte-identical. Nothing to eyeball."
    if config["status"] == "unknown":
        return f"Config comparison unavailable: {config.get('error')}."
    lines = [
        f"Deploy-tree and live `config.yaml` DIFFER ({config.get('diff_line_count')} unified-diff lines; "
        "not gating — the executor eyeballs this).",
        "",
        "```diff",
        *config.get("diff_preview", []),
        *(["... (diff truncated)"] if config.get("diff_truncated") else []),
        "```",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

def assemble(
    *, live_home: Path, report_path: Path, out_dir: Path, mtu_root: Path, policy_path: Path,
    baseline_path: Path | None, corpus_path: Path | None, nightly_path: Path | None,
    no_nightly: bool, allow_unverified: bool, require_judge: bool,
    require_full_corpus: bool, max_age_hours: float | None,
) -> tuple[dict[str, Any], Path]:
    live = _resolve_live_home(live_home)
    out = assert_outside_live_home(out_dir, live, label="--out-dir")
    report_path = Path(report_path).expanduser().resolve()
    mtu_root = Path(mtu_root).expanduser().resolve()
    assert_outside_live_home(mtu_root, live, label="--mtu-root (compose source)")

    policy = load_policy(Path(policy_path).expanduser().resolve())
    corpus = (
        Path(corpus_path).expanduser().resolve()
        if corpus_path else (mtu_root / policy["corpus"]["path"]).resolve()
    )
    baseline = (
        Path(baseline_path).expanduser().resolve()
        if baseline_path else (mtu_root / policy["corpus"]["accepted_baseline"]).resolve()
    )
    max_age = float(
        max_age_hours if max_age_hours is not None else policy["nightly"]["maximum_age_hours"]
    )
    if nightly_path is None and not no_nightly:
        default_nightly = Path(policy["nightly"]["state_dir"]).expanduser() / "latest.json"
        nightly_path = default_nightly
    if nightly_path is not None:
        nightly_path = Path(nightly_path).expanduser().resolve()

    work = Path(tempfile.mkdtemp(prefix="mtu-cutover-"))
    try:
        assert_outside_live_home(work, live, label="compose work dir")
        gates: list[dict[str, Any]] = []
        compose_gate, composed = compose_section(
            mtu_root=mtu_root, work_dir=work, allow_unverified=allow_unverified
        )
        gates.append(compose_gate)
        digest_gate, composed_sha = constitution_digest_section(
            composed=composed, live_home=live
        )
        gates.append(digest_gate)
        gates.append(config_digest_section(mtu_root=mtu_root, live_home=live))

        report = read_json(report_path)
        corpus_data = read_json(corpus)
        baseline_data = read_json(baseline)
        gates.extend(replay_sections(
            report=report, report_path=report_path, corpus=corpus_data, corpus_path=corpus,
            baseline=baseline_data, baseline_path=baseline, composed_sha256=composed_sha,
            require_judge=require_judge, require_full_corpus=require_full_corpus,
            max_age_hours=max_age,
        ))
        gates.append(nightly_section(
            nightly_path=nightly_path, waived=no_nightly, max_age_hours=max_age
        ))

        failed = [item["name"] for item in gates if item.get("gating") and item["status"] != "green"]
        summary = report.get("deterministic_summary") or {}
        regression = next(item for item in gates if item["name"] == "replay_regression")
        report_corpus = report.get("corpus") or {}
        counts = {
            "source_count": compose_gate.get("source_count"),
            "composed_source_count": compose_gate.get("composed_source_count"),
            "unverified_compliance_count": compose_gate.get("unverified_compliance_count", 0),
            "declared_case_count": report_corpus.get("declared_case_count"),
            "selected_case_count": report_corpus.get("selected_case_count"),
            "assertion_count": summary.get("assertion_count"),
            "assertions_passed": summary.get("passed"),
            "assertions_failed": summary.get("failed"),
            "current_failure_count": regression.get("current_failure_count"),
            "accepted_failure_count": regression.get("accepted_failure_count"),
            "new_failure_count": regression.get("new_failure_count"),
            "canary_case_count": next(
                item for item in gates if item["name"] == "replay_canaries"
            )["canary_case_count"],
            "gates_total": len(gates),
            "gates_gating": sum(1 for item in gates if item.get("gating")),
            "gates_green": sum(1 for item in gates if item["status"] == "green"),
            "gates_red": sum(1 for item in gates if item["status"] == "red"),
        }
        evidence = {
            "schema_version": 1,
            "kind": "mtu_cutover_evidence",
            "assembled_at": datetime.now(timezone.utc).isoformat(),
            "inputs": {
                "mtu_root": str(mtu_root),
                "deploy_tree_commit": _git_commit(mtu_root),
                "live_home": str(live),
                "live_home_written": False,
                "report": str(report_path),
                "baseline": str(baseline),
                "corpus": str(corpus),
                "policy": str(Path(policy_path).expanduser().resolve()),
                "nightly": str(nightly_path) if nightly_path else None,
                "allow_unverified": allow_unverified,
                "require_judge": require_judge,
                "require_full_corpus": require_full_corpus,
                "maximum_age_hours": max_age,
            },
            "counts": counts,
            "gates": gates,
            "verdict": {"ok": not failed, "failed_gates": failed},
        }
        out.mkdir(parents=True, exist_ok=True)
        (out / "evidence.json").write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (out / "evidence.md").write_text(render_markdown(evidence), encoding="utf-8")
        return evidence, out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _git_commit(path: Path) -> str:
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
        )
        return proc.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-home", required=True, type=Path,
                        help="deployed MTU home; READ-ONLY, never written")
    parser.add_argument("--report", required=True, type=Path, help="replay report JSON")
    parser.add_argument("--out-dir", type=Path, help="evidence output dir (default evidence/cutover-<UTC date>)")
    parser.add_argument("--mtu-root", type=Path, default=MTU_ROOT)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--nightly", type=Path)
    parser.add_argument("--no-nightly", action="store_true",
                        help="assemble without nightly evidence; recorded in the package")
    parser.add_argument("--allow-unverified", action="store_true",
                        help="carry unverified compliance artifacts; RECORDED in the package")
    parser.add_argument("--require-judge", action="store_true")
    parser.add_argument("--no-require-full-corpus", action="store_true")
    parser.add_argument("--max-age-hours", type=float)
    args = parser.parse_args()

    out_dir = args.out_dir or (
        Path(args.mtu_root).expanduser() / "evidence"
        / ("cutover-" + datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    )
    try:
        evidence, out = assemble(
            live_home=args.live_home, report_path=args.report, out_dir=out_dir,
            mtu_root=args.mtu_root, policy_path=args.policy, baseline_path=args.baseline,
            corpus_path=args.corpus, nightly_path=args.nightly, no_nightly=args.no_nightly,
            allow_unverified=args.allow_unverified, require_judge=args.require_judge,
            require_full_corpus=not args.no_require_full_corpus,
            max_age_hours=args.max_age_hours,
        )
    except CutoverRefused as exc:
        print(json.dumps(
            {"ok": False, "code": "MTU_CUTOVER_EVIDENCE_REFUSED", "error": str(exc)},
            sort_keys=True,
        ))
        raise SystemExit(2)
    print(json.dumps({
        "ok": evidence["verdict"]["ok"],
        "evidence_dir": str(out),
        "failed_gates": evidence["verdict"]["failed_gates"],
    }, sort_keys=True))
    raise SystemExit(0 if evidence["verdict"]["ok"] else 1)


if __name__ == "__main__":
    main()
