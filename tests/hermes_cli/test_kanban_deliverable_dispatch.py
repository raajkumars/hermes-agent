"""Dispatcher WIP guard for ranked deliverable work.

A ranked ready card represents an active deliverable.  Ordinary cards begin
new initiatives, so they must not consume a profile's dispatch capacity while
that profile has runnable ranked work.  Interactive-session cards are follow-up
work rather than new initiatives and remain responsive.
"""
from __future__ import annotations

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


def _ready_task(conn, *, title: str, assignee: str = "ira", rank: int = 0,
                deliverable_key: str | None = None, session_id: str | None = None) -> str:
    task_id = kb.create_task(conn, title=title, assignee=assignee)
    conn.execute(
        "UPDATE tasks SET status='ready', deliverable_key=?, rank=?, session_id=? WHERE id=?",
        (deliverable_key, rank, session_id, task_id),
    )
    return task_id


def _fake_spawn(task, workspace, board=None):
    return 42


def test_ranked_work_blocks_same_profile_new_initiative(
    kanban_home, all_assignees_spawnable,
):
    with kbc.connect() as conn:
        ranked = _ready_task(
            conn, title="finish release", deliverable_key="faabzi-1.0.9", rank=100,
        )
        unranked = _ready_task(conn, title="start a new idea")
        result = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True, max_spawn=10)

    assert [task_id for task_id, *_ in result.spawned] == [ranked]
    assert result.skipped_wip_new_initiative == [unranked]
    assert kbd.describe_suppression([result]) == "skipped_wip_new_initiative=1"


def test_unranked_work_spawns_when_no_ranked_work_exists(
    kanban_home, all_assignees_spawnable,
):
    with kbc.connect() as conn:
        unranked = _ready_task(conn, title="start a new idea")
        result = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True, max_spawn=10)

    assert [task_id for task_id, *_ in result.spawned] == [unranked]
    assert result.skipped_wip_new_initiative == []


def test_session_originated_card_bypasses_new_initiative_wip(
    kanban_home, all_assignees_spawnable,
):
    with kbc.connect() as conn:
        ranked = _ready_task(
            conn, title="finish release", deliverable_key="faabzi-1.0.9", rank=100,
        )
        session_follow_up = _ready_task(
            conn, title="interactive follow-up", session_id="session-live-1",
        )
        result = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True, max_spawn=10)

    assert [task_id for task_id, *_ in result.spawned] == [ranked, session_follow_up]
    assert result.skipped_wip_new_initiative == []


def test_default_wip_allows_one_unranked_start_fleet_wide(
    kanban_home, all_assignees_spawnable,
):
    with kbc.connect() as conn:
        ranked = _ready_task(
            conn, title="finish release", deliverable_key="faabzi-1.0.9", rank=100,
        )
        first = _ready_task(conn, title="first idea", assignee="ira")
        second = _ready_task(conn, title="second idea", assignee="prime")
        result = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=True, max_spawn=10)

    spawned_ids = [task_id for task_id, *_ in result.spawned]
    assert spawned_ids[0] == ranked
    assert set(spawned_ids[1:]) | set(result.skipped_wip_new_initiative) == {first, second}
    assert len(spawned_ids) == 2
    assert len(result.skipped_wip_new_initiative) == 1
