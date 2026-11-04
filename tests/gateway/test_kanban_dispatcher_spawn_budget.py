"""Dispatcher respawn state is isolated by board and task card."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from gateway import kanban_watchers_dispatcher as dispatcher_module


def _settings() -> dispatcher_module._DispatcherSettings:
    return dispatcher_module._DispatcherSettings(
        interval=60,
        max_spawn=None,
        max_in_progress=None,
        failure_limit=3,
        stale_timeout_seconds=0,
        reconcile_orphans=True,
        default_assignee=None,
        max_in_progress_per_profile=None,
    )


def _dispatcher() -> dispatcher_module._KanbanDispatcher:
    return dispatcher_module._KanbanDispatcher(SimpleNamespace(), _settings())


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, "
        "outcome TEXT, ended_at INTEGER)"
    )
    return conn


def test_respawn_state_completion_resets_only_matching_card():
    conn = _conn()
    dispatcher = _dispatcher()
    dispatcher._store_respawn_state(
        "board-a", "noisy", conn, count=2, last_completed_run_id=None, cap_blocked=True,
    )
    dispatcher._store_respawn_state(
        "board-a", "sibling", conn, count=1, last_completed_run_id=None, cap_blocked=False,
    )
    conn.execute(
        "INSERT INTO task_runs (id, task_id, outcome, ended_at) "
        "VALUES (7, 'sibling', 'completed', 100)"
    )
    conn.commit()

    assert dispatcher._load_respawn_state("board-a", "noisy", conn) == (2, None, True)
    assert dispatcher._load_respawn_state("board-a", "sibling", conn) == (0, 7, False)
    assert conn.execute(
        "SELECT board, task_id, spawns_since_completion FROM dispatcher_respawn_state "
        "ORDER BY task_id"
    ).fetchall() == [("board-a", "noisy", 2), ("board-a", "sibling", 0)]


def test_respawn_state_isolated_between_boards_for_same_task_id():
    conn = _conn()
    dispatcher = _dispatcher()
    dispatcher._store_respawn_state(
        "board-a", "shared", conn, count=2, last_completed_run_id=None, cap_blocked=False,
    )
    dispatcher._store_respawn_state(
        "board-b", "shared", conn, count=1, last_completed_run_id=None, cap_blocked=False,
    )

    assert dispatcher._load_respawn_state("board-a", "shared", conn) == (2, None, False)
    assert dispatcher._load_respawn_state("board-b", "shared", conn) == (1, None, False)