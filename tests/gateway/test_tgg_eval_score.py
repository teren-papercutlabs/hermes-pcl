from pathlib import Path

import pytest

openpyxl = pytest.importorskip("openpyxl")

from deploy.tgg.christopher.eval_score import load_closure, load_master, norm_job


def _save_master(path: Path) -> None:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for tab in ("Punggol", "Hougang", "Bedok"):
        ws = workbook.create_sheet(tab)
        ws.append(["Receipt", "Due", "Job No.", "Status", "Completion Date"])
    workbook["Punggol"].append(
        ["2026-01-01", "", "PG/JOB/2601/0001", "Completed", "2026-01-02"]
    )
    workbook["Hougang"].append(
        ["2026-01-01", "", "HG/JOB/2601/0002", "Pending", ""]
    )
    workbook.save(path)


def _save_closure(path: Path) -> None:
    workbook = openpyxl.Workbook()
    ws = workbook.active
    ws.title = "Tracker"
    ws.append(["Cost", "Address", "Close"])
    ws.append(
        [
            "HG/JOB/2601/0002",
            "fixture",
            "1/1/26 - Submit For Closure\n2/1/26 - Submit For WC Modification",
        ]
    )
    workbook.save(path)


def test_tgg_eval_score_reuses_master_zone_and_latest_closure_semantics(tmp_path):
    master_path = tmp_path / "master.xlsx"
    closure_path = tmp_path / "closure.xlsx"
    _save_master(master_path)
    _save_closure(closure_path)

    master = load_master(master_path)
    closure = load_closure(closure_path)

    pg = norm_job("PG/JOB/2601/0001")
    hg = norm_job("HG/JOB/2601/0002")
    assert master[pg]["zone"] == "PG"
    assert master[pg]["completed"] is True
    assert master[hg]["zone"] == "HG"
    assert master[hg]["completed"] is False
    assert closure[hg]["kind"] == "wc_mod"
    assert closure[hg]["completed"] is False
