"""The standalone kanban dispatcher must honour the global ESTOP."""
from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

import pytest

from agent import estop
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_ops


@pytest.fixture
def kanban_home(monkeypatch):
    # The test suite protects the real Hermes root; use an explicit sibling
    # temp directory rather than TMPDIR, which can itself be under that root.
    root = Path(tempfile.mkdtemp(prefix="kanban_cli_estop_", dir="/tmp"))
    home = root / ".hermes"
    (home / "profiles" / "default").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: root)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    try:
        yield home
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _args(max_spawn: int) -> argparse.Namespace:
    return argparse.Namespace(dry_run=False, max=max_spawn, failure_limit=2, json=False)


def test_cli_dispatch_estop_blocks_spawns_but_keeps_reclaim_working(kanban_home, monkeypatch, capsys):
    """Pause converts a positive ``--max`` into a reclaim-only tick, not a no-op."""
    spawned = []
    monkeypatch.setattr(kbd, "_default_spawn", lambda *args, **kwargs: spawned.append(args[0].id) or 4242)

    with kbc.connect() as conn:
        ready = kb.create_task(conn, title="ready", body="must not start", assignee="default")

    estop.engage("release swap")
    try:
        assert kanban_ops._cmd_dispatch(_args(12)) == 0
        assert spawned == []
        assert "Paused (ESTOP): ran reclaim-only; no new workers were started" in capsys.readouterr().out
        with kbc.connect() as conn:
            ready_task = kb.get_task(conn, ready)
            assert ready_task is not None and ready_task.status == "ready"
            stale = kb.create_task(conn, title="stale", body="must be reclaimed", assignee="default")
            assert kb.claim_task(conn, stale) is not None
            conn.execute("UPDATE tasks SET claim_expires = 0 WHERE id = ?", (stale,))

        # Release-swap callers use --max 0 specifically to reclaim without starting work.
        assert kanban_ops._cmd_dispatch(_args(0)) == 0
        assert "Reclaimed:    1" in capsys.readouterr().out
        with kbc.connect() as conn:
            stale_task = kb.get_task(conn, stale)
            assert stale_task is not None and stale_task.status == "ready"
    finally:
        estop.disengage()

    # Removing ESTOP leaves the ordinary CLI path unchanged.
    assert kanban_ops._cmd_dispatch(_args(12)) == 0
    assert ready in spawned
