"""The context-guard fixture reaches collection-time create_task aliases."""

from __future__ import annotations

from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db import create_task
from hermes_cli.kanban_db_connect import connect


def test_fixture_body_reaches_direct_create_task_import(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    with connect() as conn:
        task_id = create_task(conn, title="unrelated fixture scaffolding")
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.body == "test fixture card"
