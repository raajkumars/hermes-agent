"""Task-scoped respawn-cap accounting in the embedded dispatcher."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from gateway import kanban_watchers_dispatcher as dispatcher_module
from hermes_cli.kanban_db_dispatch import DispatchResult


def _dispatcher(cap: int) -> dispatcher_module._KanbanDispatcher:
    settings = dispatcher_module._DispatcherSettings(
        interval=60,
        max_spawn=None,
        max_in_progress=None,
        failure_limit=2,
        stale_timeout_seconds=0,
        reconcile_orphans=True,
        default_assignee=None,
        max_in_progress_per_profile=None,
        max_spawns_without_completion=cap,
    )
    return dispatcher_module._KanbanDispatcher(SimpleNamespace(), settings)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
    conn.execute(
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, outcome TEXT, ended_at INTEGER)"
    )
    conn.executemany(
        "INSERT INTO tasks (id, status) VALUES (?, 'running')",
        [("looping",), ("sibling",)],
    )
    return conn


def test_respawn_cap_blocks_only_spawned_card_with_machine_readable_reason(monkeypatch):
    conn = _conn()
    dispatcher = _dispatcher(cap=1)
    blocked: list[tuple[str, str]] = []

    def block_task(conn, task_id, *, reason, kind):
        blocked.append((task_id, reason))
        assert kind == "needs_input"
        conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (task_id,))
        return True

    monkeypatch.setattr(dispatcher_module, "_kb", lambda: SimpleNamespace(block_task=block_task))
    dispatcher._apply_respawn_cap(
        "board-a", conn, DispatchResult(spawned=[("looping", "ira", "/tmp/looping")]),
    )

    assert blocked == [("looping", "respawn cap: 1 starts without a completion")]
    assert dispatcher._load_respawn_state("board-a", "looping", conn) == (1, None, True)
    assert dispatcher._load_respawn_state("board-a", "sibling", conn) == (0, None, False)


def test_respawn_cap_setting_defaults_and_rejects_nonpositive_values():
    kb = SimpleNamespace(DEFAULT_FAILURE_LIMIT=2)

    assert dispatcher_module._resolve_dispatcher_settings({}, kb).max_spawns_without_completion == 20
    assert dispatcher_module._resolve_dispatcher_settings(
        {"max_spawns_without_completion": 3}, kb,
    ).max_spawns_without_completion == 3
    assert dispatcher_module._resolve_dispatcher_settings(
        {"max_spawns_without_completion": 0}, kb,
    ).max_spawns_without_completion == 20
