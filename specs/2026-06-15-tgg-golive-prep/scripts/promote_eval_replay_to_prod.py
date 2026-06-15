#!/usr/bin/env python3
"""Promote the canonical TGG eval-replay tenant DB into prod.

Default is dry-run only. Execution is a client-production bulk mutation and must
only run after Teren's explicit go for this batch.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PROD_DB = Path("/home/pclaw/.systems-pcl/data/tenants/tgg.db")
EVAL_DB = Path("/home/pclaw/.systems-pcl/eval-data/tenants/tgg.db")
BACKUP_DIR = Path("/home/pclaw/.systems-pcl/data/backups")
EXPECTED_PROD_BASE = {"cases": 3068, "case_observations": 669}
EXPECTED_EVAL_READY = {"cases": 3089, "case_observations": 727}
REQUIRED_OBS_SOURCE = "whatsapp"
FORBIDDEN_OBS_SOURCE = "wa_demo_port"


def query_scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    return conn.execute(sql, params).fetchone()[0]


def snapshot(db: Path) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    tables = ["cases", "case_observations", "bridge_message_log", "message_log"]
    counts = {t: query_scalar(conn, f"SELECT COUNT(*) FROM {t}") for t in tables}
    case_sources = [dict(value=r[0], n=r[1]) for r in conn.execute("SELECT source, COUNT(*) FROM cases GROUP BY source ORDER BY COUNT(*) DESC")]
    obs_sources = [dict(value=r[0], n=r[1]) for r in conn.execute("SELECT source, COUNT(*) FROM case_observations GROUP BY source ORDER BY COUNT(*) DESC")]
    wa_demo_port_obs = query_scalar(conn, "SELECT COUNT(*) FROM case_observations WHERE source=?", (FORBIDDEN_OBS_SOURCE,))
    whatsapp_obs = query_scalar(conn, "SELECT COUNT(*) FROM case_observations WHERE source=?", (REQUIRED_OBS_SOURCE,))
    integrity = query_scalar(conn, "PRAGMA integrity_check")
    case_id_tables = []
    for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        table = r[0]
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
        if "case_id" in cols:
            orphan = query_scalar(
                conn,
                f"SELECT COUNT(*) FROM {table} t LEFT JOIN cases c ON c.id=t.case_id WHERE t.case_id IS NOT NULL AND c.id IS NULL",
            )
            case_id_tables.append({"table": table, "orphans": orphan})
    conn.close()
    return {
        "path": str(db),
        "exists": db.exists(),
        "size": db.stat().st_size if db.exists() else None,
        "mtime_utc": dt.datetime.fromtimestamp(db.stat().st_mtime, dt.timezone.utc).isoformat() if db.exists() else None,
        "wal_exists": Path(str(db) + "-wal").exists(),
        "wal_size": Path(str(db) + "-wal").stat().st_size if Path(str(db) + "-wal").exists() else 0,
        "integrity_check": integrity,
        "counts": counts,
        "case_sources": case_sources,
        "obs_sources": obs_sources,
        "wa_demo_port_obs": wa_demo_port_obs,
        "whatsapp_obs": whatsapp_obs,
        "case_id_orphans": case_id_tables,
    }


def sqlite_backup(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


def validate(prod: dict[str, Any], eval_: dict[str, Any], *, allow_prod_count_drift: bool = False) -> list[str]:
    failures: list[str] = []
    for label, snap in (("prod", prod), ("eval", eval_)):
        if snap["integrity_check"] != "ok":
            failures.append(f"{label} integrity_check={snap['integrity_check']}")
        if snap["wa_demo_port_obs"]:
            failures.append(f"{label} still has {snap['wa_demo_port_obs']} wa_demo_port observations")
        if snap["whatsapp_obs"] != snap["counts"]["case_observations"]:
            failures.append(f"{label} observations are not all source=whatsapp")
        orphans = [x for x in snap["case_id_orphans"] if x["orphans"]]
        if orphans:
            failures.append(f"{label} case_id orphans present: {orphans}")
    if not allow_prod_count_drift:
        for table, expected in EXPECTED_PROD_BASE.items():
            if prod["counts"].get(table) != expected:
                failures.append(f"prod {table}={prod['counts'].get(table)} expected {expected}")
    for table, expected in EXPECTED_EVAL_READY.items():
        if eval_["counts"].get(table) != expected:
            failures.append(f"eval {table}={eval_['counts'].get(table)} expected {expected}")
    return failures



def _table_columns(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return list(conn.execute(f"PRAGMA table_info({table})"))


def promote_tables(eval_snapshot: Path, prod_db: Path) -> None:
    """Replace only the canonical case tables, preserving prod-only schema/tables."""
    conn = sqlite3.connect(str(prod_db))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(f"ATTACH DATABASE '{eval_snapshot}' AS evalsrc")
        for table in ("cases", "case_observations"):
            prod_cols_info = _table_columns(conn, table)
            eval_cols = [r["name"] for r in conn.execute(f"PRAGMA evalsrc.table_info({table})")]
            prod_cols = [r["name"] for r in prod_cols_info]
            missing = [c for c in eval_cols if c not in prod_cols]
            if missing:
                raise RuntimeError(f"prod {table} missing eval columns: {missing}")
            extra_required = [
                r["name"] for r in prod_cols_info
                if r["name"] not in eval_cols and int(r["notnull"] or 0) and r["dflt_value"] is None and not int(r["pk"] or 0)
            ]
            if extra_required:
                raise RuntimeError(f"prod {table} has required extra columns not in eval: {extra_required}")
        # Source-ref completeness: eval observations must have evidence rows already in prod bridge_message_log.
        missing_refs = conn.execute(
            """SELECT COUNT(*) FROM evalsrc.case_observations o
               WHERE o.source_ref IS NOT NULL AND o.source_ref != ''
               AND NOT EXISTS (SELECT 1 FROM main.bridge_message_log b WHERE b.source_ref=o.source_ref)"""
        ).fetchone()[0]
        if missing_refs:
            raise RuntimeError(f"{missing_refs} eval observations do not have source_ref rows in prod bridge_message_log")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM case_observations")
        conn.execute("DELETE FROM cases")
        for table in ("cases", "case_observations"):
            eval_cols = [r["name"] for r in conn.execute(f"PRAGMA evalsrc.table_info({table})")]
            cols = ", ".join('"' + c.replace('"', '""') + '"' for c in eval_cols)
            conn.execute(f"INSERT INTO main.{table} ({cols}) SELECT {cols} FROM evalsrc.{table}")
            try:
                seq = conn.execute(f"SELECT COALESCE(MAX(id),0) FROM main.{table}").fetchone()[0]
                conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET seq=excluded.seq", (table, seq))
            except sqlite3.OperationalError:
                pass
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.execute("DETACH DATABASE evalsrc")
        except Exception:
            pass
        conn.close()

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prod-db", default=str(PROD_DB))
    ap.add_argument("--eval-db", default=str(EVAL_DB))
    ap.add_argument("--backup-dir", default=str(BACKUP_DIR))
    ap.add_argument("--execute", action="store_true", help="replace prod cases/observations from eval; requires --teren-go")
    ap.add_argument("--teren-go", help="verbatim/trace token for Teren's explicit per-batch authorization")
    ap.add_argument("--allow-prod-count-drift", action="store_true", help="dry-run/execute may proceed if prod base count drifted after this prep")
    args = ap.parse_args()

    prod_db = Path(args.prod_db)
    eval_db = Path(args.eval_db)
    backup_dir = Path(args.backup_dir)
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prod_before = snapshot(prod_db)
    eval_before = snapshot(eval_db)
    failures = validate(prod_before, eval_before, allow_prod_count_drift=args.allow_prod_count_drift)
    report: dict[str, Any] = {
        "checked_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "execute" if args.execute else "dry-run",
        "prod_before": prod_before,
        "eval_before": eval_before,
        "validation_failures": failures,
        "planned_action": "backup prod, snapshot eval, replace prod cases + case_observations from eval while preserving prod schema/bridge_message_log/aux tables, verify counts/provenance/source refs/orphans",
        "backup_path": str(backup_dir / f"tgg-golive-promote-before-{now}.db"),
        "eval_snapshot_path": str(backup_dir / f"tgg-golive-eval-snapshot-{now}.db"),
    }
    if failures:
        print(json.dumps(report, indent=2))
        return 2
    if not args.execute:
        print(json.dumps(report, indent=2))
        return 0
    if not args.teren_go:
        report["execution_blocked"] = "--teren-go is required for the prod write"
        print(json.dumps(report, indent=2))
        return 3

    backup_path = Path(report["backup_path"])
    eval_snapshot_path = Path(report["eval_snapshot_path"])
    sqlite_backup(prod_db, backup_path)
    sqlite_backup(eval_db, eval_snapshot_path)
    promote_tables(eval_snapshot_path, prod_db)
    prod_after = snapshot(prod_db)
    report["prod_after"] = prod_after
    report["teren_go"] = args.teren_go
    report["executed_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    post_failures = validate(prod_after, eval_before, allow_prod_count_drift=True)
    for table in ("cases", "case_observations"):
        if prod_after["counts"][table] != eval_before["counts"][table]:
            post_failures.append(f"post {table}={prod_after['counts'][table]} did not match eval {eval_before['counts'][table]}")
    report["post_validation_failures"] = post_failures
    manifest = backup_dir / f"tgg-golive-promote-manifest-{now}.json"
    manifest.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 4 if post_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
