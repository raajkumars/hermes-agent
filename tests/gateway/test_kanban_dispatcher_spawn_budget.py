"""The gateway's spawn budget advances only for confirmed worker starts."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from gateway import kanban_watchers_dispatcher as dispatcher_module


def _settings(cap: int) -> dispatcher_module._DispatcherSettings:
    return dispatcher_module._DispatcherSettings(
        interval=60,
        max_spawn=None,
        max_in_progress=None,
        failure_limit=2,
        max_spawns_without_completion=cap,
        stale_timeout_seconds=3600,
        reconcile_orphans=True,
        default_assignee=None,
        max_in_progress_per_profile=None,
    )


def test_spawn_budget_persists_confirmed_starts_and_resets_after_completion(monkeypatch, tmp_path):
    db_path = tmp_path / "kanban.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, outcome TEXT, ended_at REAL)"
        )
    dispatch = dispatcher_module._KanbanDispatcher(SimpleNamespace(), _settings(cap=2))
    monkeypatch.setattr(dispatch, "board_db_fingerprint", lambda _slug: ("db", 1, 1))
    monkeypatch.setattr(
        dispatcher_module,
        "_kbc",
        lambda: SimpleNamespace(connect=lambda **_kwargs: sqlite3.connect(db_path)),
    )

    outcomes = iter([1, 0, 1, 1])
    calls = 0
    max_spawns = []

    def dispatch_once(_conn, **kwargs):
        nonlocal calls
        calls += 1
        max_spawns.append(kwargs["max_spawn"])
        started = next(outcomes)
        return SimpleNamespace(spawned=[("task", "ira", "workspace")] * started)

    monkeypatch.setattr(
        dispatcher_module,
        "_kbd",
        lambda: SimpleNamespace(
            dispatch_once=dispatch_once,
            DispatchResult=lambda **kwargs: SimpleNamespace(**kwargs),
        ),
    )

    dispatch.tick_once_for_board("board-a")
    dispatch.tick_once_for_board("board-a")  # Claim/process failure: no start.
    dispatch.tick_once_for_board("board-a")

    assert dispatch._spawns_since_completion == {"board-a": 2}

    restarted = dispatcher_module._KanbanDispatcher(SimpleNamespace(), _settings(cap=2))
    monkeypatch.setattr(restarted, "board_db_fingerprint", lambda _slug: ("db", 1, 1))
    capped = restarted.tick_once_for_board("board-a")

    assert restarted._spawns_since_completion == {"board-a": 2}
    assert getattr(capped, "respawn_cap_blocked", False) is True
    assert calls == 3
    assert max_spawns == [2, 1, 1]
    with sqlite3.connect(db_path) as conn:
        state = conn.execute(
            "SELECT spawns_since_completion, cap_blocked, updated_at "
            "FROM dispatcher_respawn_state WHERE board = 'board-a'"
        ).fetchone()
    assert state[0] == 2
    assert state[1] == 1
    assert state[2] > 0

    held = restarted.tick_once_for_board("board-a")
    assert getattr(held, "respawn_cap_blocked", False) is True
    assert calls == 3

    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO task_runs (outcome, ended_at) VALUES ('completed', 1)")
    resumed = restarted.tick_once_for_board("board-a")

    assert restarted._spawns_since_completion == {"board-a": 1}
    assert getattr(resumed, "respawn_cap_blocked", False) is False
    assert calls == 4


def test_zero_spawn_budget_never_calls_dispatch_until_a_completion(monkeypatch, tmp_path):
    db_path = tmp_path / "kanban.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, outcome TEXT, ended_at REAL)"
        )
    dispatch = dispatcher_module._KanbanDispatcher(SimpleNamespace(), _settings(cap=0))
    monkeypatch.setattr(dispatch, "board_db_fingerprint", lambda _slug: ("db", 1, 1))
    monkeypatch.setattr(
        dispatcher_module,
        "_kbc",
        lambda: SimpleNamespace(connect=lambda **_kwargs: sqlite3.connect(db_path)),
    )
    calls = 0

    def dispatch_once(_conn, **_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(spawned=[])

    monkeypatch.setattr(
        dispatcher_module,
        "_kbd",
        lambda: SimpleNamespace(
            dispatch_once=dispatch_once,
            DispatchResult=lambda **kwargs: SimpleNamespace(**kwargs),
        ),
    )

    held = dispatch.tick_once_for_board("board-a")

    assert getattr(held, "respawn_cap_blocked", False) is True
    assert calls == 0
    with sqlite3.connect(db_path) as conn:
        state = conn.execute(
            "SELECT spawns_since_completion, cap_blocked FROM dispatcher_respawn_state "
            "WHERE board = 'board-a'"
        ).fetchone()
    assert state == (0, 1)