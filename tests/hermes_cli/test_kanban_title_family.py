"""Title-family duplicate protection for Kanban cards."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db_graph import decompose_triage_task


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_title_family_normalizes_volatile_identifiers():
    assert kb.title_family("Fix t_a1b2c3d4 regression #42 at abcdef123") == (
        "fix task regression pr at sha"
    )
    assert kb.title_family("Fix t_ffffffff regression #7 at deadbee") == (
        "fix task regression pr at sha"
    )
    assert kb.title_family("One two three four five six seven eight nine") == (
        "one two three four five six seven"
    )


def test_create_rejects_unlinked_duplicate_family(kanban_home):
    with kbc.connect_closing() as conn:
        existing = kb.create_task(conn, title="Repair t_a1b2c3d4 queue #12 at deadbee")
        with pytest.raises(kb.DuplicateFamilyError) as exc:
            kb.create_task(conn, title="Repair t_ffffffff queue #99 at abcdef1")

    assert exc.value.task_ids == (existing,)
    assert existing in str(exc.value)


def test_linked_create_allows_same_family(kanban_home):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent")
        kb.create_task(conn, title="Repair t_a1b2c3d4 queue #12 at deadbee")
        child = kb.create_task(
            conn, title="Repair t_ffffffff queue #99 at abcdef1", parents=(parent,),
        )

    assert child


def test_supersedes_archives_old_card_and_records_links(kanban_home):
    with kbc.connect_closing() as conn:
        old = kb.create_task(conn, title="Repair t_a1b2c3d4 queue #12 at deadbee")
        new = kb.create_task(
            conn, title="Repair t_ffffffff queue #99 at abcdef1", supersedes=old,
        )
        old_task = kb.get_task(conn, old)
        old_comments = kb.list_comments(conn, old)
        new_comments = kb.list_comments(conn, new)

    assert old_task.status == "archived"
    assert any(new in comment.body for comment in old_comments)
    assert any(old in comment.body for comment in new_comments)


def test_cli_duplicate_returns_exit_two_and_existing_id(kanban_home, capsys):
    with kbc.connect_closing() as conn:
        existing = kb.create_task(conn, title="Repair t_a1b2c3d4 queue #12 at deadbee")

    args = argparse.Namespace(
        kanban_action="create", title="Repair t_ffffffff queue #99 at abcdef1",
        body=None, body_file=None, workspace=None, branch=None, max_runtime=None,
        max_retries=None, parent=[], assignee=None, created_by="test", project=None,
        tenant=None, priority=0, triage=False, idempotency_key=None, skills=[],
        model_override=None, provider_override=None, goal_mode=False, goal_max_turns=None,
        completion_contract=None, supersedes=None, initial_status="running", json=False,
        board=None,
    )
    assert kc.kanban_command(args) == 2
    assert existing in capsys.readouterr().err


def test_decompose_reuses_existing_family_card(kanban_home):
    with kbc.connect_closing() as conn:
        existing = kb.create_task(conn, title="Implement t_a1b2c3d4 sync #12 at deadbee")
        root = kb.create_task(conn, title="decompose", triage=True)
        child_ids = decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[{
                "title": "Implement t_ffffffff sync #99 at abcdef1",
                "assignee": "worker",
                "parents": [],
            }],
        )
        rows = conn.execute("SELECT id FROM tasks WHERE title_family = ?", (
            kb.title_family("Implement t_a1b2c3d4 sync #12 at deadbee"),
        )).fetchall()

    assert child_ids == [existing]
    assert [row["id"] for row in rows] == [existing]
