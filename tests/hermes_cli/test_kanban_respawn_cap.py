"""Per-card respawn cap (kanban.max_spawns_without_completion).

The cap is enforced BEFORE claim/spawn, so it can never end a live run or
detach a live worker; only an operator unblock or a completion resets it.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

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


def _spawner(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return None  # no PID: the crash detector leaves the run alone
    return fake_spawn


def _end_run_back_to_ready(conn, task_id: str, *, outcome: str = "crashed", age: int = 0) -> None:
    """Simulate a worker that exited without completing; the card is ready again.

    ``age`` backdates the run so it sorts before a later reset point.
    """
    now = int(time.time())
    with kb.write_txn(conn):
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
        assert run_id is not None, "card was expected to hold a live run"
        conn.execute(
            "UPDATE task_runs SET status = ?, outcome = ?, ended_at = ?, started_at = started_at - ? "
            "WHERE id = ?",
            (outcome, outcome, now, age, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status = 'ready', current_run_id = NULL, claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL WHERE id = ?",
            (task_id,),
        )


def _backdate_events(conn, task_id: str, kinds: tuple, seconds: int) -> None:
    marks = ", ".join("?" for _ in kinds)
    with kb.write_txn(conn):
        conn.execute(
            f"UPDATE task_events SET created_at = created_at - ? WHERE task_id = ? AND kind IN ({marks})",
            (seconds, task_id, *kinds),
        )


def _loop(conn, task_id, spawns, *, cap, starts, age=0):
    for _ in range(starts):
        res = kbd.dispatch_once(conn, spawn_fn=_spawner(spawns), max_spawns_without_completion=cap)
        assert task_id in [t for (t, _a, _w) in res.spawned]
        _end_run_back_to_ready(conn, task_id, age=age)


def test_cap_blocks_before_claim_with_spec_reason(kanban_home, all_assignees_spawnable):
    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="looping", assignee="alice")
        _loop(conn, tid, spawns, cap=3, starts=3)
        claims_before = [e for e in kb.list_events(conn, tid) if e.kind == "claimed"]

        res = kbd.dispatch_once(conn, spawn_fn=_spawner(spawns), max_spawns_without_completion=3)
        task = kb.get_task(conn, tid)
        claims_after = [e for e in kb.list_events(conn, tid) if e.kind == "claimed"]
        reasons = [e.payload.get("reason") for e in kb.list_events(conn, tid) if e.kind == "blocked"]
        # The block's own synthesized handoff run is not a worker start.
        starts_after_block = kbd.count_spawns_without_completion(conn, tid)

    assert res.respawn_cap_blocked == [tid]
    assert not res.spawned
    assert len(spawns) == 3  # the 4th start never happened
    assert len(claims_after) == len(claims_before)  # blocked before any claim
    assert starts_after_block == 3
    assert task.status == "blocked"
    assert task.block_kind == "needs_input"
    assert reasons == ["respawn cap: 3 starts without completion"]


def test_cap_never_touches_a_live_run(kanban_home, all_assignees_spawnable):
    """At the cap, a card that is already running keeps its run and worker."""
    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="live", assignee="alice")
        _loop(conn, tid, spawns, cap=2, starts=1)
        # Second start reaches the cap; the worker is live now.
        kbd.dispatch_once(conn, spawn_fn=_spawner(spawns), max_spawns_without_completion=2)
        live = kb.get_task(conn, tid)
        assert live.status == "running" and live.current_run_id is not None
        # Further ticks while it runs: the cap must not block or end the live run.
        res = kbd.dispatch_once(conn, spawn_fn=_spawner(spawns), max_spawns_without_completion=2)
        after = kb.get_task(conn, tid)
        run = conn.execute(
            "SELECT ended_at, outcome FROM task_runs WHERE id = ?", (live.current_run_id,),
        ).fetchone()

    assert res.respawn_cap_blocked == []
    assert after.status == "running"
    assert after.current_run_id == live.current_run_id
    assert run["ended_at"] is None and run["outcome"] is None


def test_cap_is_per_card(kanban_home, all_assignees_spawnable):
    spawns: list = []
    with kbc.connect() as conn:
        noisy = kb.create_task(conn, title="noisy", assignee="alice")
        _loop(conn, noisy, spawns, cap=2, starts=2)
        quiet = kb.create_task(conn, title="quiet", assignee="bob")
        res = kbd.dispatch_once(conn, spawn_fn=_spawner(spawns), max_spawns_without_completion=2)

    assert res.respawn_cap_blocked == [noisy]
    assert [t for (t, _a, _w) in res.spawned] == [quiet]


def test_unblock_resets_the_window(kanban_home, all_assignees_spawnable):
    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
        _loop(conn, tid, spawns, cap=2, starts=2, age=100)
        kbd.dispatch_once(conn, spawn_fn=_spawner(spawns), max_spawns_without_completion=2)
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.unblock_task(conn, tid)
        assert kbd.count_spawns_without_completion(conn, tid) == 0
        res = kbd.dispatch_once(conn, spawn_fn=_spawner(spawns), max_spawns_without_completion=2)

    assert [t for (t, _a, _w) in res.spawned] == [tid]


def test_completion_resets_the_window(kanban_home, all_assignees_spawnable):
    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
        _loop(conn, tid, spawns, cap=5, starts=3, age=100)
        assert kbd.count_spawns_without_completion(conn, tid) == 3
        kbd.dispatch_once(conn, spawn_fn=_spawner(spawns), max_spawns_without_completion=5)
        assert kb.complete_task(conn, tid, summary="done")
        assert kbd.count_spawns_without_completion(conn, tid) == 0


def test_requeue_without_unblock_does_not_reset(kanban_home, all_assignees_spawnable):
    """Reassignment / re-promotion are not reset points."""
    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
        _loop(conn, tid, spawns, cap=3, starts=2, age=100)
        kb.assign_task(conn, tid, "bob")
        assert kbd.count_spawns_without_completion(conn, tid) == 2


@pytest.mark.parametrize("outcome", ["spawn_failed", "rate_limited"])
def test_non_starts_are_not_counted(kanban_home, all_assignees_spawnable, outcome):
    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=_spawner(spawns), max_spawns_without_completion=1)
        _end_run_back_to_ready(conn, tid, outcome=outcome)
        assert kbd.count_spawns_without_completion(conn, tid) == 0


def test_zero_disables_and_dry_run_does_not_mutate(kanban_home, all_assignees_spawnable):
    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
        _loop(conn, tid, spawns, cap=0, starts=2)
        dry = kbd.dispatch_once(conn, dry_run=True, max_spawns_without_completion=2)
        assert dry.respawn_cap_blocked == [tid]
        assert kb.get_task(conn, tid).status == "ready"
        off = kbd.dispatch_once(conn, spawn_fn=_spawner(spawns), max_spawns_without_completion=0)

    assert [t for (t, _a, _w) in off.spawned] == [tid]
