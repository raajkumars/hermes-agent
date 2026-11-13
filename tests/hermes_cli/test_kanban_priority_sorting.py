"""Priority ordering and human-facing Kanban priority conventions."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban import build_parser
from hermes_cli.kanban_output import _fmt_task_line


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.mark.parametrize("status", ["ready", "review"])
def test_higher_priority_dispatches_and_lists_first(kanban_home, status):
    with kbc.connect() as conn:
        low = kb.create_task(conn, title="low", priority=-1000)
        high = kb.create_task(conn, title="high", priority=100000)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = ? WHERE id IN (?, ?)", (status, low, high))

        assert [row["id"] for row in kbd._lane_rows(conn, status)] == [high, low]
        assert [task.id for task in kb.list_tasks(conn, status=status)] == [high, low]
        assert [task.id for task in kb.list_tasks(conn, status=status, order_by="priority")] == [high, low]
        assert [task.id for task in kb.list_tasks(conn, status=status, order_by="priority-asc")] == [low, high]
        assert [task.id for task in kb.list_tasks(conn, status=status, order_by="priority-desc")] == [low, high]


def test_priority_asc_uses_effective_priority_before_limit(kanban_home):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="inherited", priority=0)
        child = kb.create_task(conn, title="urgent child", priority=100)
        medium = kb.create_task(conn, title="medium", priority=10)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id IN (?, ?, ?)",
                         (parent, child, medium))
        kb.link_tasks(conn, parent, child)

        assert [task.id for task in kb.list_tasks(conn, status="ready", order_by="priority-asc")] == [medium, parent]
        assert [task.id for task in kb.list_tasks(conn, status="ready", order_by="priority-asc", limit=1)] == [medium]


def test_text_list_line_displays_priority(kanban_home):
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title="urgent", priority=1500))

    assert task is not None
    assert "p1500" in _fmt_task_line(task)


def test_priority_help_is_unambiguous(capsys):
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())

    for argv in (["kanban", "create", "--help"], ["kanban", "edit", "--help"], ["kanban", "--help"]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
        assert "Higher numbers dispatch first; default 0." in capsys.readouterr().out

    with pytest.raises(SystemExit):
        parser.parse_args(["kanban", "list", "--help"])
    list_help = " ".join(capsys.readouterr().out.split())
    assert "priority = most urgent first (highest number)" in list_help
    assert "priority-asc" in list_help
    assert "legacy priority-desc" in list_help
