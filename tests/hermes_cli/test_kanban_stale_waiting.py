"""Regression for the fm #37 post-review-stall class of incident and the
"stale blocked child nobody revisits" class.

``kanban_db_dispatch.detect_stale_waiting`` escalates ``review``/``blocked``
tasks that have sat untouched past a configured threshold, WITHOUT mutating
their status (only a human/reviewer resolves those). It is idempotent per
entry into the status (one escalation, not one per tick) and re-arms on a
fresh re-entry into the status.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def conn(tmp_path: Path):
    db = kbc.connect(tmp_path / "kanban.db")
    try:
        yield db
    finally:
        db.close()


def _backdate_latest_event(conn, task_id: str, kind: str, seconds_ago: int) -> None:
    """Test-only: push an event's created_at into the past so elapsed-time
    checks don't need a real sleep."""
    row = conn.execute(
        "SELECT id FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1", (task_id, kind),
    ).fetchone()
    assert row is not None, f"no {kind} event for {task_id}"
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE id = ?",
            (int(time.time()) - seconds_ago, row["id"]),
        )


def test_disabled_by_default(conn):
    task_id = kb.create_task(conn, title="t", assignee="anika")
    kb.block_task(conn, task_id, reason="waiting on secrets", kind="needs_input")
    _backdate_latest_event(conn, task_id, "blocked", 999999)
    assert kbd.detect_stale_waiting(conn, timeout_seconds=0) == []


def test_escalates_a_stale_review(conn):
    task_id = kb.create_task(conn, title="t", assignee="anika")
    kb.claim_task(conn, task_id)
    assert kb.request_review(conn, task_id, summary="ready for review", reviewer="meera")
    _backdate_latest_event(conn, task_id, "review_requested", 5000)

    escalated = kbd.detect_stale_waiting(conn, timeout_seconds=3600)
    assert len(escalated) == 1
    assert escalated[0]["id"] == task_id
    assert escalated[0]["status"] == "review"
    assert escalated[0]["elapsed_seconds"] >= 5000

    events = [e.kind for e in kb.list_events(conn, task_id)]
    assert events.count("stale_waiting_escalated") == 1
    # Status itself must never be mutated by the escalation.
    task = kb.get_task(conn, task_id)
    assert task is not None and task.status == "review"


def test_escalates_a_stale_blocked_task(conn):
    task_id = kb.create_task(conn, title="t", assignee="anika")
    assert kb.block_task(conn, task_id, reason="need creds", kind="needs_input")
    _backdate_latest_event(conn, task_id, "blocked", 10000)

    escalated = kbd.detect_stale_waiting(conn, timeout_seconds=3600)
    assert [e["id"] for e in escalated] == [task_id]
    assert escalated[0]["status"] == "blocked"


def test_not_yet_stale_is_not_escalated(conn):
    task_id = kb.create_task(conn, title="t", assignee="anika")
    kb.block_task(conn, task_id, reason="need creds", kind="needs_input")
    # Fresh block event (created "now") is well under the threshold.
    assert kbd.detect_stale_waiting(conn, timeout_seconds=3600) == []


def test_idempotent_no_duplicate_escalation_across_ticks(conn):
    task_id = kb.create_task(conn, title="t", assignee="anika")
    kb.block_task(conn, task_id, reason="need creds", kind="needs_input")
    _backdate_latest_event(conn, task_id, "blocked", 10000)

    first = kbd.detect_stale_waiting(conn, timeout_seconds=3600)
    assert len(first) == 1
    second = kbd.detect_stale_waiting(conn, timeout_seconds=3600)
    assert second == [], "a duplicate ping every tick is noise, not a wakeup"

    events = [e.kind for e in kb.list_events(conn, task_id)]
    assert events.count("stale_waiting_escalated") == 1


def test_reentering_the_status_rearms_escalation(conn):
    task_id = kb.create_task(conn, title="t", assignee="anika")
    kb.block_task(conn, task_id, reason="need creds", kind="needs_input")
    _backdate_latest_event(conn, task_id, "blocked", 10000)
    assert len(kbd.detect_stale_waiting(conn, timeout_seconds=3600)) == 1

    assert kb.unblock_task(conn, task_id)
    # A different kind so the same-kind unblock-loop counter (BLOCK_RECURRENCE_LIMIT)
    # doesn't reroute this second block to triage instead of blocked.
    assert kb.block_task(conn, task_id, reason="unrelated new blocker", kind="transient")
    # Fresh entry: not stale yet even though the FIRST entry was ages ago.
    assert kbd.detect_stale_waiting(conn, timeout_seconds=3600) == []

    _backdate_latest_event(conn, task_id, "blocked", 10000)
    second = kbd.detect_stale_waiting(conn, timeout_seconds=3600)
    assert [e["id"] for e in second] == [task_id]


def test_running_and_done_tasks_are_never_escalated(conn):
    running_id = kb.create_task(conn, title="running", assignee="anika")
    kb.claim_task(conn, running_id)
    done_id = kb.create_task(conn, title="done", assignee="anika")
    assert kb.complete_task(conn, done_id, summary="shipped")

    assert kbd.detect_stale_waiting(conn, timeout_seconds=1) == []
