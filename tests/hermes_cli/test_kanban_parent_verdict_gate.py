"""Regression coverage for completion-verdict dependency gates."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from hermes_cli import kanban_db
from hermes_cli import kanban_db_connect


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kanban_db.kanban_db_path(board="default")
    kanban_db._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kanban_db.init_db()
    with kanban_db_connect.connect() as connection:
        yield connection


def _task(conn: sqlite3.Connection, title: str, *, parents: list[str] | None = None) -> str:
    return kanban_db.create_task(conn, title=title, assignee="worker", parents=parents or [])


def _complete(conn: sqlite3.Connection, task_id: str, verdict: str | None = None) -> None:
    metadata = {"verdict": verdict} if verdict is not None else None
    assert kanban_db.complete_task(conn, task_id, summary="tested", metadata=metadata)


def test_rejecting_parent_verdict_blocks_child_and_records_detail(conn):
    parent = _task(conn, "parent")
    _complete(conn, parent, "changes_requested")

    child = _task(conn, "child", parents=[parent])

    assert kanban_db.get_task(conn, child).status == "blocked"
    holds = kanban_db.unsatisfied_parents(conn, child)
    assert holds == [(parent, "done", "parent_verdict_not_approve", "changes_requested")]
    event = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'dependency_wait' ORDER BY id DESC LIMIT 1",
        (child,),
    ).fetchone()
    assert '"reason": "parent_verdict_not_approve"' in event["payload"]
    assert '"verdict": "changes_requested"' in event["payload"]


def test_approving_parent_verdict_releases_child_and_legacy_parent_still_releases(conn):
    approved = _task(conn, "approved")
    legacy = _task(conn, "legacy")
    _complete(conn, approved, "APPROVED")
    _complete(conn, legacy)

    child = _task(conn, "child", parents=[approved, legacy])

    assert kanban_db.get_task(conn, child).status == "ready"
    assert kanban_db.unsatisfied_parents(conn, child) == []


def test_recompute_blocks_ready_child_when_a_parent_completion_is_later_rejected(conn):
    parent = _task(conn, "parent")
    _complete(conn, parent, "approved")
    child = _task(conn, "child", parents=[parent])
    assert kanban_db.get_task(conn, child).status == "ready"

    conn.execute("UPDATE task_runs SET metadata = ? WHERE task_id = ?", ('{"verdict": "rejected"}', parent))
    kanban_db.recompute_ready(conn)

    assert kanban_db.get_task(conn, child).status == "blocked"
    assert kanban_db.unsatisfied_parents(conn, child)[0][2:] == ("parent_verdict_not_approve", "rejected")


def test_rejecting_free_text_summary_is_a_conservative_gate(conn):
    parent = _task(conn, "parent")
    _complete(conn, parent)
    conn.execute("UPDATE task_runs SET summary = 'Changes requested: please fix the implementation' WHERE task_id = ?", (parent,))

    child = _task(conn, "child", parents=[parent])

    assert kanban_db.get_task(conn, child).status == "blocked"
    assert kanban_db.unsatisfied_parents(conn, child)[0][2] == "parent_verdict_not_approve"


def test_link_to_rejected_terminal_parent_blocks_ready_child(conn):
    parent = _task(conn, "parent")
    _complete(conn, parent, "rejected")
    child = _task(conn, "child")

    assert kanban_db.link_tasks(conn, parent, child)
    assert kanban_db.get_task(conn, child).status == "blocked"
