"""Deliverable rank and age ordering for Kanban dispatcher/list visibility."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_deliverable_rank_inherits_from_open_dependent_and_orders_dispatch(kanban_home):
    now = int(time.time())
    with kbc.connect() as conn:
        conn.execute(
            "INSERT INTO kanban_deliverables(key, rank) VALUES (?, ?), (?, ?)",
            ("top", 20, "lower", 10),
        )
        old_parent = kb.create_task(conn, title="old parent", priority=0, assignee="alice")
        old_child = kb.create_task(conn, title="ranked dependent", priority=0, assignee="alice")
        newer = kb.create_task(conn, title="newer lower band", priority=1500, assignee="alice")
        assert kb.edit_task(conn, old_child, deliverable_key="top")
        assert kb.edit_task(conn, newer, deliverable_key="lower")
        assert kb.link_tasks(conn, old_parent, old_child) is True
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (now - 40 * 86400, old_parent))
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (now - 86400, newer))

        ordered = kb.list_tasks(conn, status="ready", order_by="priority")
        lane = kbd._lane_rows(conn, "ready")

    assert [task.id for task in ordered][:2] == [old_parent, newer]
    assert ordered[0].effective_deliverable_key == "top"
    assert ordered[0].deliverable_rank == 20
    assert ordered[0].age_bonus == 500
    assert [row["id"] for row in lane][:2] == [old_parent, newer]


def test_edit_deliverable_and_stats_expose_dispatch_rank_columns(kanban_home):
    with kbc.connect() as conn:
        conn.execute("INSERT INTO kanban_deliverables(key, rank) VALUES (?, ?)", ("ship", 7))
        task_id = kb.create_task(conn, title="ship it", assignee="alice")

    output = kc.run_slash(f"edit {task_id} --deliverable ship")
    assert f"Edited {task_id}" in output
    with kbc.connect() as conn:
        task = kb.get_task(conn, task_id)
        stats = kb.board_stats(conn)

    assert task.deliverable_key == "ship"
    assert any(row["id"] == task_id and row["deliverable_rank"] == 7 for row in stats["rank_columns"])
    assert "Dispatch rank columns" in kc.run_slash("stats")
