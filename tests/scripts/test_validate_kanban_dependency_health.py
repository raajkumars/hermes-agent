"""Post-deployment Kanban dependency health validation contract."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "observability" / "validate_kanban_dependency_health.py"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _load():
    spec = importlib.util.spec_from_file_location("validate_kanban_dependency_health", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_validation_hook_passes_without_stranded_dependencies(kanban_home, capsys):
    mod = _load()

    assert mod.main([]) == 0
    assert capsys.readouterr().out == "PASS: no dependency-stranded Kanban tasks detected.\n"


def test_validation_hook_fails_for_terminal_parent_todo_dependency(kanban_home, capsys):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="ira")
        kb.create_task(conn, title="child", assignee="ira", parents=[parent])
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))

    mod = _load()

    assert mod.main([]) == 1
    assert capsys.readouterr().out == "FAIL: 1 dependency-stranded Kanban task(s) detected.\n"