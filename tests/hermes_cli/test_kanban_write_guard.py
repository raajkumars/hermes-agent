"""#69283: kanban write guard prevents tests from writing to real ~/.hermes."""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db
from hermes_cli import kanban_db_connect as kbc


def _allow_tmp_kanban_db(monkeypatch, tmp_path) -> None:
    """The runner's scratch directory lives below /opt/data, the real-root guard."""
    import tests.conftest as _conftest

    monkeypatch.setattr(_conftest, "_REAL_KANBAN_ROOT", tmp_path / "real-kanban-root")


def test_connect_succeeds_under_test_home(tmp_path, monkeypatch):
    """When HERMES_HOME is a temp dir, kanban connect succeeds normally."""
    _allow_tmp_kanban_db(monkeypatch, tmp_path)
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    conn = kbc.connect()
    try:
        assert str(kanban_db.kanban_db_path()).startswith(str(home))
    finally:
        conn.close()


def test_connect_raises_when_kanban_home_is_real_root(monkeypatch):
    """When kanban paths resolve to the REAL root, connect raises RuntimeError."""
    import tests.conftest as _conftest

    monkeypatch.setattr(
        kanban_db, "kanban_home", lambda: _conftest._REAL_KANBAN_ROOT
    )
    monkeypatch.setattr(
        kanban_db,
        "kanban_db_path",
        lambda board=None: _conftest._REAL_KANBAN_ROOT / "kanban.db",
    )
    with pytest.raises(RuntimeError, match="kanban_write_guard"):
        kbc.connect()


def test_connect_raises_for_explicit_db_path_under_real_root():
    """Explicit db_path pointing under the real root is also refused."""
    import tests.conftest as _conftest

    with pytest.raises(RuntimeError, match="kanban_write_guard"):
        kbc.connect(_conftest._REAL_KANBAN_ROOT / "kanban.db")


@pytest.mark.linux_only
def test_write_txn_allows_unrelated_nonzero_offset_on_same_db(tmp_path, monkeypatch):
    """A backup/inspection FD must not be mistaken for SQLite descriptor reuse."""
    _allow_tmp_kanban_db(monkeypatch, tmp_path)
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db_path = home / "kanban.db"

    with kbc.connect_closing(db_path) as conn:
        with db_path.open("r+b") as foreign_file:
            foreign_file.seek(29)
            with kbc.write_txn(conn):
                conn.execute(
                    "INSERT INTO tasks (id, title, status, created_at) "
                    "VALUES ('t-unrelated-fd', 'must commit', 'ready', 1)"
                )

        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE id = 't-unrelated-fd'").fetchone()[0] == 1


@pytest.mark.linux_only
def test_write_txn_refuses_live_db_with_overwritten_header(tmp_path, monkeypatch):
    """Foreign I/O that overwrites the live SQLite header cannot be committed."""
    _allow_tmp_kanban_db(monkeypatch, tmp_path)
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db_path = home / "kanban.db"

    with kbc.connect_closing(db_path) as conn:
        changes_before = conn.total_changes
        with db_path.open("r+b") as foreign_file:
            foreign_file.write(b"X")
            foreign_file.flush()
            with pytest.raises(kbc.KanbanDbHeaderCorruptionError, match="header magic changed"):
                with kbc.write_txn(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, title, status, created_at) "
                        "VALUES ('t-overwritten-header', 'must not commit', 'ready', 1)"
                    )

        assert conn.total_changes == changes_before
