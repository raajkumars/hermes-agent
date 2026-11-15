"""#69283: kanban write guard prevents tests from writing to real ~/.hermes."""

from __future__ import annotations

import os

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


@pytest.mark.skipif(not os.path.isdir("/proc/self/fdinfo"), reason="requires Linux /proc fd metadata")
def test_write_txn_refuses_nonzero_offset_on_live_kanban_fd(tmp_path, monkeypatch):
    """A TLS BIO writing through a recycled SQLite fd advances its offset.

    The guard must fail before BEGIN IMMEDIATE, leaving the requested mutation
    uncommitted instead of silently publishing data after foreign I/O touched
    the board descriptor.
    """
    _allow_tmp_kanban_db(monkeypatch, tmp_path)
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    db_path = home / "kanban.db"

    with kbc.connect_closing(db_path) as conn:
        foreign_fd = os.open(db_path, os.O_RDWR)
        try:
            os.lseek(foreign_fd, 29, os.SEEK_SET)
            with pytest.raises(kbc.KanbanDbFdReuseError, match="non-zero file offset"):
                with kbc.write_txn(conn):
                    conn.execute(
                        "INSERT INTO tasks (id, title, status, created_at) "
                        "VALUES ('t-fd-reuse', 'must not commit', 'ready', 1)"
                    )
        finally:
            os.close(foreign_fd)

        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE id = 't-fd-reuse'").fetchone()[0] == 0
