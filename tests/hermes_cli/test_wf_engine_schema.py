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
            "template_id",
            "slug",
            "version",
            "content_hash",
            "spec",
            "created_at",
        ],
        "pk": ["template_id"],
        "unique": [{"slug", "version"}],
    },
    "wf_instance": {
        "columns": [
            "task_id",
            "entity_key",
            "template_id",
            "template_version",
            "corr",
            "vars",
            "state",
            "parked_since",
        ],
        "pk": ["task_id"],
        "unique": [{"entity_key"}],
    },
    "wf_wait": {
        "columns": [
            "id",
            "task_id",
            "step_key",
            "kind",
            "event_types",
            "schema_ref",
            "timer_at",
            "timer_action",
            "fires_used",
            "resume_token",
            "status",
            "created_at",
        ],
        "pk": ["id"],
        "unique": [],
    },
    "wf_event": {
        "columns": [
            "id",
            "source",
            "external_id",
            "event_type",
            "payload",
            "corr",
            "status",
            "matched_task_id",
            "match_method",
            "match_confidence",
            "created_at",
            "applied_at",
        ],
        "pk": ["id"],
        "unique": [{"source", "external_id"}],
    },
    "wf_transition": {
        "columns": ["task_id", "step_key", "to_step", "event_id", "applied_at"],
        "pk": ["task_id", "step_key", "event_id"],
        "unique": [],
    },
    "wf_approval": {
        "columns": [
            "id",
            "task_id",
            "step_key",
            "action",
            "payload",
            "status",
            "decided_by",
            "decided_at",
            "decision_diff",
            "resume_token",
            "created_at",
        ],
        "pk": ["id"],
        "unique": [],
    },
    "wf_outbox": {
        "columns": [
            "id",
            "task_id",
            "action",
            "payload",
            "status",
            "attempts",
            "created_at",
            "sent_at",
        ],
        "pk": ["id"],
        "unique": [],
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
    conn = _conn(tmp_path)
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
            assert [row[1] for row in columns] == expected["columns"]
            assert [row[1] for row in columns if row[5]] == expected["pk"]
            assert _unique_sets(conn, table) == expected["unique"]
    finally:
        conn.close()

    reopened = _conn(tmp_path)
    try:
        assert {
            row[0]
            for row in reopened.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }.issuperset(TABLES)
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
            "SELECT template_id, version, content_hash, spec, created_at "
            "FROM wf_template WHERE template_id='alpha@1'"
        ).fetchone()
        assert tuple(stored[:4]) == (
            "alpha@1",
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
    for name in ("MatchResult", "ApplyResult", "SweepResult"):
        assert inspect.isclass(getattr(wf_engine, name))
        assert hasattr(getattr(wf_engine, name), "__dataclass_fields__")


def test_engine_and_schema_identifiers_are_tenant_neutral():
    engine_source = open(wf_engine.__file__, encoding="utf-8").read().lower()
    schema_source = kanban_db.SCHEMA_SQL.lower()
    for noun in ("allied", "container_no", "job_no", "booking_ref"):
        assert noun not in engine_source
        assert noun not in schema_source
