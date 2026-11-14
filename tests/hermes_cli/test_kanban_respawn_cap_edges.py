"""Edge cases for the per-card respawn cap (kanban.max_spawns_without_completion).

Complements ``test_kanban_respawn_cap.py``: the exact cap boundary, missing
card ids, a block that loses its race, a second window after an unblock, and
the gateway's config parsing (an invalid value must never disable the cap).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from gateway import kanban_watchers_dispatcher as kwd
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
        return None
    return fake_spawn


def _end_run_back_to_ready(conn, task_id: str, *, age: int = 0) -> None:
    now = int(time.time())
    with kb.write_txn(conn):
        run_id = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
        assert run_id is not None, "card was expected to hold a live run"
        conn.execute(
            "UPDATE task_runs SET status = 'crashed', outcome = 'crashed', ended_at = ?, "
            "started_at = started_at - ? WHERE id = ?",
            (now, age, run_id),
        )
        conn.execute(
            "UPDATE tasks SET status = 'ready', current_run_id = NULL, claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL WHERE id = ?",
            (task_id,),
        )


def _tick(conn, spawns, cap):
    return kbd.dispatch_once(conn, spawn_fn=_spawner(spawns), max_spawns_without_completion=cap)


def _spawned(res) -> list:
    return [t for (t, _a, _w) in res.spawned]


def test_below_cap_spawns_and_exact_cap_blocks(kanban_home, all_assignees_spawnable):
    """Starts 1..cap all spawn; the start that would be cap+1 is blocked."""
    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
        for n in range(1, 4):
            res = _tick(conn, spawns, cap=3)
            assert _spawned(res) == [tid], f"start {n} of 3 must spawn"
            assert res.respawn_cap_blocked == []
            assert kbd.count_spawns_without_completion(conn, tid) == n
            _end_run_back_to_ready(conn, tid)
        res = _tick(conn, spawns, cap=3)
        task = kb.get_task(conn, tid)

    assert len(spawns) == 3
    assert res.respawn_cap_blocked == [tid]
    assert task.status == "blocked"


@pytest.mark.parametrize("missing", ["t_does_not_exist", "", None])
def test_missing_card_id_counts_zero_and_is_never_blocked(kanban_home, all_assignees_spawnable, missing):
    spawns: list = []
    with kbc.connect() as conn:
        # Other cards' history must not leak into a missing id's count.
        tid = kb.create_task(conn, title="t", assignee="alice")
        _tick(conn, spawns, cap=5)
        _end_run_back_to_ready(conn, tid)

        assert kbd.count_spawns_without_completion(conn, missing) == 0
        result = kbd.DispatchResult()
        assert kbd._apply_respawn_cap(conn, missing, result, cap=1, dry_run=False) is False
        assert result.respawn_cap_blocked == []
        assert kb.get_task(conn, tid).status == "ready"


def test_lost_block_race_still_never_spawns_past_cap(kanban_home, all_assignees_spawnable, monkeypatch):
    """If block_task loses a race, the card is not reported blocked but is not spawned either."""
    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
        _tick(conn, spawns, cap=1)
        _end_run_back_to_ready(conn, tid)
        monkeypatch.setattr(kb, "block_task", lambda *a, **k: False)
        res = _tick(conn, spawns, cap=1)
        task = kb.get_task(conn, tid)

    assert len(spawns) == 1
    assert _spawned(res) == []
    assert res.respawn_cap_blocked == []
    assert task.status == "ready"


def test_unblock_opens_a_fresh_full_window(kanban_home, all_assignees_spawnable):
    """After an unblock the card gets exactly ``cap`` new starts, then blocks again."""
    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
        for _ in range(2):
            _tick(conn, spawns, cap=2)
            _end_run_back_to_ready(conn, tid, age=200)
        _tick(conn, spawns, cap=2)
        assert kb.get_task(conn, tid).status == "blocked"
        assert kb.unblock_task(conn, tid)
        # Place the unblock strictly between the old and the new runs.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_events SET created_at = created_at - 100 "
                "WHERE task_id = ? AND kind = 'unblocked'", (tid,),
            )

        for _ in range(2):
            res = _tick(conn, spawns, cap=2)
            assert _spawned(res) == [tid]
            _end_run_back_to_ready(conn, tid)
        res = _tick(conn, spawns, cap=2)
        reasons = [e.payload.get("reason") for e in kb.list_events(conn, tid) if e.kind == "blocked"]

    assert len(spawns) == 4
    assert res.respawn_cap_blocked == [tid]
    assert reasons[-1] == "respawn cap: 2 starts without completion"


def test_completion_then_reopen_is_not_capped(kanban_home, all_assignees_spawnable):
    """Starts before a completion never count toward a later window.

    A completed card back in ``ready`` is held by the separate prior-success
    respawn guard; this asserts only that the cap itself does not fire.
    """
    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
        _tick(conn, spawns, cap=2)
        _end_run_back_to_ready(conn, tid, age=200)
        _tick(conn, spawns, cap=2)
        assert kb.complete_task(conn, tid, summary="done")
        # Place the completion after both earlier runs and before the reopen.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET started_at = started_at - 200 WHERE task_id = ?", (tid,),
            )
            conn.execute(
                "UPDATE task_events SET created_at = created_at - 100 "
                "WHERE task_id = ? AND kind = 'completed'", (tid,),
            )
            conn.execute(
                "UPDATE task_runs SET ended_at = ended_at - 100 "
                "WHERE task_id = ? AND outcome = 'completed'", (tid,),
            )
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
        assert kbd.count_spawns_without_completion(conn, tid) == 0
        res = _tick(conn, spawns, cap=2)

        status = kb.get_task(conn, tid).status

    assert res.respawn_cap_blocked == []
    assert status != "blocked"


def _reset_by_unblock(conn, tid):
    assert kb.unblock_task(conn, tid)
    return "unblocked"


def _reset_by_completion(conn, tid):
    assert kb.complete_task(conn, tid, summary="done")
    # Reopen: back to ready so the next window can start.
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))
    return "completed"


def _window_after_reset(conn, reset) -> tuple:
    """Drive a card into the cap, apply ``reset``, then record the cap's
    verdict for every start of the next window plus the one past it.

    Starts are real claims (``claim_task``) and the verdict is read straight
    from :func:`kbd._apply_respawn_cap`, so the prior-success guard that
    holds a reopened completed card cannot mask the cap's own behaviour.
    """
    cap = 2
    spawns: list = []
    tid = kb.create_task(conn, title="t", assignee="alice")
    for _ in range(cap):
        _tick(conn, spawns, cap=cap)
        _end_run_back_to_ready(conn, tid, age=300)
    _tick(conn, spawns, cap=cap)
    assert kb.get_task(conn, tid).status == "blocked"

    kind = reset(conn, tid)
    # Same-second claims after a reset are deliberately uncounted; place the
    # reset strictly between the old window and the new one.
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_events SET created_at = created_at - 100 WHERE task_id = ? AND kind = ?",
            (tid, kind),
        )
        conn.execute(
            "UPDATE task_runs SET ended_at = ended_at - 100 WHERE task_id = ? AND outcome = 'completed'",
            (tid,),
        )

    trace = []
    for _ in range(cap + 1):
        result = kbd.DispatchResult()
        count = kbd.count_spawns_without_completion(conn, tid)
        capped = kbd._apply_respawn_cap(conn, tid, result, cap=cap, dry_run=False)
        trace.append((count, capped, list(result.respawn_cap_blocked) == [tid]))
        if capped:
            break
        assert kb.claim_task(conn, tid) is not None
        _end_run_back_to_ready(conn, tid)
    # Final status is deliberately not compared: a re-block after an operator
    # unblock is escalated by the separate unblock-loop detector.
    reasons = [e.payload.get("reason") for e in kb.list_events(conn, tid) if e.kind == "blocked"]
    return trace, reasons[-1]


def test_completion_resets_the_cap_exactly_like_unblock(kanban_home, all_assignees_spawnable):
    """Completion is a reset point with the same effect as an operator unblock:
    count back to 0, a full window of ``cap`` starts allowed, then the cap fires again."""
    with kbc.connect() as conn:
        via_unblock = _window_after_reset(conn, _reset_by_unblock)
        via_completion = _window_after_reset(conn, _reset_by_completion)

    expected_trace = [(0, False, False), (1, False, False), (2, True, True)]
    assert via_unblock == (expected_trace, "respawn cap: 2 starts without completion")
    assert via_completion == via_unblock


def test_count_above_cap_blocks_with_the_real_count(kanban_home, all_assignees_spawnable):
    """A count already PAST the cap (operator lowered the cap mid-window) blocks
    before claim, exactly like count == cap, and the reason states the real count."""
    spawns: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="t", assignee="alice")
        for _ in range(4):
            _tick(conn, spawns, cap=10)
            _end_run_back_to_ready(conn, tid)
        assert kbd.count_spawns_without_completion(conn, tid) == 4
        claims_before = [e for e in kb.list_events(conn, tid) if e.kind == "claimed"]

        res = _tick(conn, spawns, cap=2)
        task = kb.get_task(conn, tid)
        claims_after = [e for e in kb.list_events(conn, tid) if e.kind == "claimed"]
        reasons = [e.payload.get("reason") for e in kb.list_events(conn, tid) if e.kind == "blocked"]

    assert len(spawns) == 4
    assert _spawned(res) == []
    assert res.respawn_cap_blocked == [tid]
    assert len(claims_after) == len(claims_before)
    assert task.status == "blocked"
    assert reasons == ["respawn cap: 4 starts without completion"]


@pytest.mark.parametrize(
    ("raw", "expected", "warns"),
    [
        ("absent", kbd.DEFAULT_MAX_SPAWNS_WITHOUT_COMPLETION, False),
        (5, 5, False),
        ("7", 7, False),
        (0, 0, True),  # explicit disable is honoured but announced
        (-1, kbd.DEFAULT_MAX_SPAWNS_WITHOUT_COMPLETION, True),
        ("lots", kbd.DEFAULT_MAX_SPAWNS_WITHOUT_COMPLETION, True),
        (None, kbd.DEFAULT_MAX_SPAWNS_WITHOUT_COMPLETION, True),
    ],
)
def test_gateway_setting_never_silently_disables_the_cap(kanban_home, caplog, raw, expected, warns):
    cfg = {} if raw == "absent" else {"max_spawns_without_completion": raw}
    with caplog.at_level(logging.WARNING):
        settings = kwd._resolve_dispatcher_settings(cfg, kbd)
    cap_warnings = [r for r in caplog.records if "max_spawns_without_completion" in r.getMessage()]

    assert settings.max_spawns_without_completion == expected
    assert bool(cap_warnings) is warns
