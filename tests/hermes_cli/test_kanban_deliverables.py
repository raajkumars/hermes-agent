"""Deliverable schema, read-time inheritance and ``kanban edit --deliverable``."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_deliverables as kd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(conn, title, **kw):
    return kb.create_task(conn, title=title, assignee="alice", **kw)


def test_legacy_board_gains_deliverable_schema_on_open(kanban_home):
    db = kb.kanban_db_path()
    with kbc.connect() as conn:
        task_id = _task(conn, "legacy")
    raw = sqlite3.connect(db)
    raw.execute("DROP TABLE kanban_deliverables")
    raw.execute("ALTER TABLE tasks DROP COLUMN deliverable_key")
    raw.commit()
    raw.close()
    kbc._INITIALIZED_PATHS.clear()
    with kbc.connect() as conn:
        kd.upsert_deliverable(conn, "ship", rank=5)
        assert kb.edit_task(conn, task_id, deliverable_key="ship")
        assert kb.get_task(conn, task_id).deliverable_key == "ship"


def _deliverable_schema(db):
    raw = sqlite3.connect(db)
    try:
        cols = [r[1] for r in raw.execute("PRAGMA table_info(tasks)") if r[1] == "deliverable_key"]
        table = raw.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='kanban_deliverables'"
        ).fetchone()
        band_cols = [(r[1], r[2], r[5]) for r in raw.execute("PRAGMA table_info(kanban_deliverables)")]
    finally:
        raw.close()
    return cols, table is not None, band_cols


def test_migration_is_idempotent_and_shape_matches_spec(kanban_home):
    db = kb.kanban_db_path()
    first = _deliverable_schema(db)
    kb.init_db()
    kb.init_db()
    assert _deliverable_schema(db) == first
    cols, has_table, band_cols = first
    assert cols == ["deliverable_key"] and has_table
    assert {name: (typ, pk) for name, typ, pk in band_cols} == {
        "key": ("TEXT", 1), "rank": ("INTEGER", 0), "owner": ("TEXT", 0),
        "dod": ("TEXT", 0), "epic_task_id": ("TEXT", 0),
    }
    with kbc.connect() as conn:
        assert kb.get_task(conn, _task(conn, "plain")).deliverable_key is None


def test_revert_drops_schema_refuses_data_loss_and_reapplies(kanban_home):
    db = kb.kanban_db_path()
    with kbc.connect() as conn:
        kd.upsert_deliverable(conn, "ship", rank=1)
        task_id = _task(conn, "keyed")
        kb.edit_task(conn, task_id, deliverable_key="ship")
        with pytest.raises(ValueError, match="refusing to drop"):
            kd.revert_deliverable_schema(conn)
        assert _deliverable_schema(db)[:2] == (["deliverable_key"], True)
        assert kd.revert_deliverable_schema(conn, discard_data=True) is True
        assert kd.revert_deliverable_schema(conn) is False  # reversal is idempotent too
    assert _deliverable_schema(db)[:2] == ([], False)
    kb.init_db()  # forward migration re-applies on next open
    assert _deliverable_schema(db)[:2] == (["deliverable_key"], True)
    with kbc.connect() as conn:
        assert kb.get_task(conn, task_id).deliverable_key is None


def test_nearest_open_dependent_wins_and_own_key_overrides(kanban_home):
    with kbc.connect() as conn:
        kd.upsert_deliverable(conn, "near", rank=1)
        kd.upsert_deliverable(conn, "far", rank=99)
        root = _task(conn, "root")
        mid = _task(conn, "mid", parents=[root])
        far = _task(conn, "far", parents=[mid])
        sibling = _task(conn, "near", parents=[root])
        kb.edit_task(conn, far, deliverable_key="far")
        kb.edit_task(conn, sibling, deliverable_key="near")

        assert kd.effective_deliverable_keys(conn, [root, mid]) == {root: "near", mid: "far"}

        kb.edit_task(conn, root, deliverable_key="far")
        assert kd.effective_deliverable_keys(conn, [root])[root] == "far"
        kb.edit_task(conn, root, deliverable_key="")  # clear -> inherits again
        assert kb.get_task(conn, root).deliverable_key is None
        assert kd.effective_deliverable_keys(conn, [root])[root] == "near"


def test_terminal_dependents_do_not_pass_their_key(kanban_home):
    with kbc.connect() as conn:
        kd.upsert_deliverable(conn, "ship", rank=3)
        root = _task(conn, "root")
        child = _task(conn, "child", parents=[root])
        kb.edit_task(conn, child, deliverable_key="ship")
        assert kd.effective_deliverable_keys(conn, [root])[root] == "ship"
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (child,))
        assert kd.effective_deliverable_keys(conn, [root])[root] is None


def test_cycle_and_dangling_link_resolve_without_raising(kanban_home, caplog):
    with kbc.connect() as conn:
        kd.upsert_deliverable(conn, "ship", rank=3)
        a = _task(conn, "a")
        b = _task(conn, "b")
        c = _task(conn, "c")
        conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?), (?, ?), (?, ?)",
                     (a, b, b, a, b, "t_missing"))
        assert kd.effective_deliverable_keys(conn, [a, b, c]) == {a: None, b: None, c: None}
        conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (b, c))
        kb.edit_task(conn, c, deliverable_key="ship")
        assert kd.effective_deliverable_keys(conn, [a, b]) == {a: "ship", b: "ship"}
    assert "dangling link" in caplog.text


def test_cli_edit_sets_validates_and_records_event(kanban_home):
    with kbc.connect() as conn:
        kd.upsert_deliverable(conn, "ship", rank=7)
        task_id = _task(conn, "ship it")

    assert f"Edited {task_id}" in kc.run_slash(f"edit {task_id} --deliverable ship")
    assert "unknown deliverable" in kc.run_slash(f"edit {task_id} --deliverable nope")
    with kbc.connect() as conn:
        assert kb.get_task(conn, task_id).deliverable_key == "ship"
        events = [e for e in kb.list_events(conn, task_id) if e.kind == "deliverable_set"]
    assert [(e.payload["old"], e.payload["new"]) for e in events] == [(None, "ship")]
