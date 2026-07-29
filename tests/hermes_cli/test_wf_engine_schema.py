"""Bare-SQLite schema and template registrar tests."""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_db
from hermes_cli import wf_engine


TABLES = {
    "wf_template": {
        "columns": [
            ("slug", "TEXT", 1, None),
            ("version", "INTEGER", 1, None),
            ("content_hash", "TEXT", 1, None),
            ("spec", "TEXT", 1, None),
            ("created_at", "INTEGER", 1, None),
        ],
        "pk": ["slug", "version"],
        "unique": [],
        "foreign_keys": [],
    },
    "wf_instance": {
        "columns": [
            ("task_id", "TEXT", 0, None),
            ("entity_key", "TEXT", 1, None),
            ("template_id", "TEXT", 1, None),
            ("template_version", "INTEGER", 1, None),
            ("corr", "TEXT", 1, None),
            ("vars", "TEXT", 0, None),
            ("state", "TEXT", 1, None),
            ("parked_since", "INTEGER", 0, None),
        ],
        "pk": ["task_id"],
        "unique": [{"entity_key"}],
        "foreign_keys": [("tasks", "task_id", "id")],
    },
    "wf_wait": {
        "columns": [
            ("id", "INTEGER", 0, None),
            ("task_id", "TEXT", 1, None),
            ("step_key", "TEXT", 1, None),
            ("kind", "TEXT", 1, None),
            ("event_types", "TEXT", 0, None),
            ("schema_ref", "TEXT", 0, None),
            ("timer_at", "INTEGER", 0, None),
            ("timer_action", "TEXT", 0, None),
            ("fires_used", "INTEGER", 0, "0"),
            ("resume_token", "TEXT", 0, None),
            ("status", "TEXT", 1, None),
            ("created_at", "INTEGER", 1, None),
        ],
        "pk": ["id"],
        "unique": [],
        "foreign_keys": [],
    },
    "wf_event": {
        "columns": [
            ("id", "INTEGER", 0, None),
            ("source", "TEXT", 1, None),
            ("external_id", "TEXT", 0, None),
            ("event_type", "TEXT", 0, None),
            ("payload", "TEXT", 0, None),
            ("corr", "TEXT", 0, None),
            ("status", "TEXT", 1, None),
            ("matched_task_id", "TEXT", 0, None),
            ("match_method", "TEXT", 0, None),
            ("match_confidence", "REAL", 0, None),
            ("created_at", "INTEGER", 1, None),
            ("applied_at", "INTEGER", 0, None),
        ],
        "pk": ["id"],
        "unique": [{"source", "external_id"}],
        "foreign_keys": [],
    },
    "wf_transition": {
        "columns": [
            ("task_id", "TEXT", 1, None),
            ("step_key", "TEXT", 1, None),
            ("to_step", "TEXT", 1, None),
            ("event_id", "INTEGER", 1, None),
            ("applied_at", "INTEGER", 1, None),
        ],
        "pk": ["task_id", "step_key", "event_id"],
        "unique": [],
        "foreign_keys": [("wf_event", "event_id", "id")],
    },
    "wf_approval": {
        "columns": [
            ("id", "INTEGER", 0, None),
            ("task_id", "TEXT", 1, None),
            ("step_key", "TEXT", 1, None),
            ("action", "TEXT", 1, None),
            ("payload", "TEXT", 1, None),
            ("status", "TEXT", 1, None),
            ("decided_by", "TEXT", 0, None),
            ("decided_at", "INTEGER", 0, None),
            ("decision_diff", "TEXT", 0, None),
            ("resume_token", "TEXT", 1, None),
            ("created_at", "INTEGER", 1, None),
        ],
        "pk": ["id"],
        "unique": [],
        "foreign_keys": [],
    },
    "wf_outbox": {
        "columns": [
            ("id", "INTEGER", 0, None),
            ("task_id", "TEXT", 0, None),
            ("action", "TEXT", 1, None),
            ("payload", "TEXT", 1, None),
            ("status", "TEXT", 1, None),
            ("attempts", "INTEGER", 0, "0"),
            ("created_at", "INTEGER", 1, None),
            ("sent_at", "INTEGER", 0, None),
        ],
        "pk": ["id"],
        "unique": [],
        "foreign_keys": [],
    },
}


def _conn(tmp_path):
    return kanban_db.connect(tmp_path / "board.sqlite")


def _unique_sets(conn, table):
    indexes = conn.execute(f"PRAGMA index_list({table})").fetchall()
    result = []
    for index in indexes:
        # SQLite exposes the implicit primary-key index in this list too;
        # UNIQUE assertions below cover declared UNIQUE constraints only.
        if not index[2] or (len(index) > 3 and index[3] == "pk"):
            continue
        columns = conn.execute(f"PRAGMA index_info({index[1]})").fetchall()
        result.append({column[2] for column in columns})
    return result


def test_schema_is_exact_and_reopen_is_idempotent(tmp_path):
    db_path = tmp_path / "board.sqlite"
    conn = kanban_db.connect(db_path)
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert set(TABLES).issubset(names)
        for table, expected in TABLES.items():
            columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
            assert [tuple(row[index] for index in (1, 2, 3, 4)) for row in columns] == expected["columns"]
            assert [row[1] for row in columns if row[5]] == expected["pk"]
            assert _unique_sets(conn, table) == expected["unique"]
            assert [
                (row[2], row[3], row[4])
                for row in conn.execute(f"PRAGMA foreign_key_list({table})")
            ] == expected["foreign_keys"]
        conn.execute(
            "INSERT INTO wf_template "
            "(slug, version, content_hash, spec, created_at) "
            "VALUES ('alpha', 1, 'hash', '{}', 1)"
        )
    finally:
        conn.close()

    # Force the migration pass instead of only exercising connect()'s
    # in-process initialization cache.
    kanban_db.init_db(db_path)
    reopened = kanban_db.connect(db_path)
    try:
        assert {
            row[0]
            for row in reopened.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }.issuperset(TABLES)
        assert reopened.execute("SELECT COUNT(*) FROM wf_template").fetchone()[0] == 1
    finally:
        reopened.close()


def test_register_template_hashes_canonical_content_and_versions(tmp_path):
    conn = _conn(tmp_path)
    try:
        spec = {"id": "alpha", "z": "é", "nested": {"b": 2, "a": 1}}
        expected_json = json.dumps(
            spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        expected_hash = hashlib.sha256(expected_json.encode("utf-8")).hexdigest()

        first = wf_engine.register_template(conn, spec)
        same = wf_engine.register_template(
            conn, {"nested": {"a": 1, "b": 2}, "z": "é", "id": "alpha"}
        )
        changed = wf_engine.register_template(
            conn, {"id": "alpha", "nested": {"a": 1, "b": 3}, "z": "é"}
        )
        independent = wf_engine.register_template(conn, {"id": "beta"})

        assert first == ("alpha@1", 1)
        assert same == first
        assert changed == ("alpha@2", 2)
        assert independent == ("beta@1", 1)
        assert conn.execute("SELECT COUNT(*) FROM wf_template").fetchone()[0] == 3
        stored = conn.execute(
            "SELECT slug, version, content_hash, spec, created_at "
            "FROM wf_template WHERE slug='alpha' AND version=1"
        ).fetchone()
        assert tuple(stored[:4]) == (
            "alpha",
            1,
            expected_hash,
            expected_json,
        )
        assert isinstance(stored[4], int)
        assert stored[4] <= int(time.time())
    finally:
        conn.close()


def test_register_template_requires_id(tmp_path):
    conn = _conn(tmp_path)
    try:
        with pytest.raises(ValueError):
            wf_engine.register_template(conn, {})
        with pytest.raises(ValueError):
            wf_engine.register_template(conn, {"id": ""})
    finally:
        conn.close()


def test_frozen_api_signatures_and_result_dataclasses():
    expected = {
        "register_template": ["conn", "spec"],
        "create_instance": [
            "conn",
            "template_id",
            "entity_key",
            "corr",
            "vars",
            "source_event_id",
        ],
        "ingest_event": [
            "conn",
            "source",
            "external_id",
            "payload",
            "corr",
            "event_type",
        ],
        "match_event": ["conn", "event_id"],
        "apply_event": ["conn", "event_id", "task_id", "expected_step"],
        "park": ["conn", "task_id", "step_key", "waits"],
        "advance": ["conn", "task_id", "to_step", "event_id"],
        "fire_due_timers": ["conn", "now"],
        "sweep": ["conn", "now"],
    }
    for name, parameter_names in expected.items():
        assert list(inspect.signature(getattr(wf_engine, name)).parameters) == parameter_names
    assert inspect.signature(wf_engine.apply_event).parameters["expected_step"].kind is inspect.Parameter.KEYWORD_ONLY
    assert inspect.signature(wf_engine.advance).parameters["to_step"].kind is inspect.Parameter.KEYWORD_ONLY
    assert inspect.signature(wf_engine.advance).parameters["event_id"].kind is inspect.Parameter.KEYWORD_ONLY
    for name in ("create_instance", "ingest_event"):
        parameters = inspect.signature(getattr(wf_engine, name)).parameters
        assert all(
            parameters[parameter].kind is inspect.Parameter.KEYWORD_ONLY
            for parameter in list(parameters)[1:]
        )
    for name in ("MatchResult", "ApplyResult", "SweepResult"):
        assert inspect.isclass(getattr(wf_engine, name))
        assert hasattr(getattr(wf_engine, name), "__dataclass_fields__")


def test_engine_and_schema_identifiers_are_tenant_neutral():
    with open(wf_engine.__file__, encoding="utf-8") as source_file:
        engine_source = source_file.read().lower()
    schema_source = kanban_db.SCHEMA_SQL.lower()
    for noun in ("allied", "container_no", "job_no", "booking_ref"):
        assert noun not in engine_source
        assert noun not in schema_source
