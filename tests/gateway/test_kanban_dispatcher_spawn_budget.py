"""The dispatcher respawn cap is durable and scoped to one task card."""

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


def _db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, "
        "outcome TEXT NOT NULL, ended_at REAL)"
    )
    conn.executemany("INSERT INTO tasks VALUES (?, 'running')", [("noisy",), ("sibling",)])
    conn.commit()
    return conn


def _block_task(conn, task_id, **_kwargs):
    with conn:
        return conn.execute(
            "UPDATE tasks SET status = 'blocked' WHERE id = ? AND status IN ('ready', 'running')",
            (task_id,),
        ).rowcount == 1


def _wire_dispatcher(monkeypatch, db_path, starts):
    kb = SimpleNamespace(block_task=_block_task)
    dispatcher = dispatcher_module._KanbanDispatcher(kb, _settings(cap=3))
    monkeypatch.setattr(dispatcher, "board_db_fingerprint", lambda _slug: ("db", 1, 1))
    monkeypatch.setattr(
        dispatcher_module,
        "_kbc",
        lambda: SimpleNamespace(connect=lambda **_kwargs: sqlite3.connect(db_path)),
    )

    def dispatch_once(_conn, **_kwargs):
        return SimpleNamespace(spawned=[(task_id, "ira", "workspace") for task_id in next(starts)])

    monkeypatch.setattr(
        dispatcher_module,
        "_kbd",
        lambda: SimpleNamespace(dispatch_once=dispatch_once),
    )
    return dispatcher


def test_noisy_card_hits_durable_cap_without_freezing_sibling(monkeypatch, tmp_path):
    db_path = tmp_path / "kanban.db"
    _db(db_path).close()
    dispatcher = _wire_dispatcher(
        monkeypatch, db_path, iter([["noisy"], ["sibling"], ["noisy"], ["sibling"], ["noisy"], ["sibling"]]),
    )

    for _ in range(5):
        dispatcher.tick_once_for_board("board-a")

    with sqlite3.connect(db_path) as conn:
        noisy = conn.execute("SELECT status FROM tasks WHERE id = 'noisy'").fetchone()
        sibling = conn.execute("SELECT status FROM tasks WHERE id = 'sibling'").fetchone()
        states = dict(conn.execute(
            "SELECT task_id, spawns_since_completion FROM dispatcher_respawn_state"
        ).fetchall())
    assert noisy == ("blocked",)
    assert sibling == ("running",)
    assert states == {"noisy": 3, "sibling": 2}

    # A fresh dispatcher must retain the noisy card's block but still let the
    # unrelated sibling receive another confirmed start.
    restarted = _wire_dispatcher(monkeypatch, db_path, iter([["sibling"]]))
    resumed = restarted.tick_once_for_board("board-a")
    assert resumed.spawned == [("sibling", "ira", "workspace")]
    assert getattr(resumed, "respawn_cap_blocked", False) is True
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT spawns_since_completion, cap_blocked FROM dispatcher_respawn_state "
            "WHERE task_id = 'sibling'"
        ).fetchone() == (3, 1)


def test_completion_or_operator_unblock_resets_only_that_cards_window(monkeypatch, tmp_path):
    db_path = tmp_path / "kanban.db"
    conn = _db(db_path)
    conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = 'noisy'")
    conn.execute(
        "CREATE TABLE dispatcher_respawn_state (task_id TEXT PRIMARY KEY, "
        "spawns_since_completion INTEGER NOT NULL, last_completed_run_id INTEGER, "
        "cap_blocked INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO dispatcher_respawn_state VALUES ('noisy', 3, NULL, 1, 1)"
    )
    conn.commit()
    conn.close()

    dispatcher = _wire_dispatcher(monkeypatch, db_path, iter([["noisy"], ["noisy"]]))
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = 'noisy'")
        conn.commit()
    dispatcher.tick_once_for_board("board-a")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT spawns_since_completion, cap_blocked FROM dispatcher_respawn_state "
            "WHERE task_id = 'noisy'"
        ).fetchone() == (1, 0)
        conn.execute(
            "INSERT INTO task_runs (task_id, outcome, ended_at) VALUES ('noisy', 'completed', 1)"
        )
        conn.commit()
    dispatcher.tick_once_for_board("board-a")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT spawns_since_completion, cap_blocked FROM dispatcher_respawn_state "
            "WHERE task_id = 'noisy'"
        ).fetchone() == (1, 0)
