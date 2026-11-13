"""create_task refuses a card that nothing can act on (overlay 0050).

A card with no body, no parent and no creating task has no spec and no context
to infer one from. On 2026-10-21 seven such cards were created in one burst
with no created_by; each cost a workspace, a dispatch slot and a full worker
run, then sat blocked forever. The guard lives in ``kanban_db.create_task`` so
every surface (CLI, dashboard, REST, tool, direct callers) is covered, not only
the ``kanban_create`` tool.

Both directions are asserted: a guard that refuses everything would pass the
refusal tests alone, so the acceptance cases are what prove it is targeted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect

pytestmark = pytest.mark.kanban_context_guard


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


@pytest.mark.parametrize("body", [None, "", "   \n\t "])
def test_refuses_card_with_no_body_parent_or_creator(kanban_home, body):
    with connect() as conn:
        before = _count(conn)
        with pytest.raises(ValueError) as exc:
            kb.create_task(conn, title="engineering-0", body=body, assignee="prime")
        assert _count(conn) == before, "a refused card must not be inserted"
    msg = str(exc.value)
    assert "refusing to create 'engineering-0'" in msg
    assert "has nothing for a worker to act on" in msg
    # The caller is told all three ways out.
    assert "body describing the work and how it will be verified" in msg
    assert "link it to a parent" in msg
    assert "triage" in msg


def test_card_with_body_is_created(kanban_home):
    with connect() as conn:
        tid = kb.create_task(conn, title="real work", body="Do X; verify with Y.")
        assert kb.get_task(conn, tid).body == "Do X; verify with Y."


def test_triage_card_without_body_is_created(kanban_home):
    with connect() as conn:
        tid = kb.create_task(conn, title="needs a spec", triage=True)
        assert kb.get_task(conn, tid).status == "triage"


def test_card_with_parent_and_no_body_is_created(kanban_home):
    with connect() as conn:
        parent = kb.create_task(conn, title="parent", body="spec lives here")
        child = kb.create_task(conn, title="child", parents=[parent])
        assert kb.get_task(conn, child) is not None


def test_card_with_creator_task_and_no_body_is_created(kanban_home):
    with connect() as conn:
        creator = kb.create_task(conn, title="creator", body="spec lives here")
        child = kb.create_task(conn, title="spawned", creator_task_id=creator)
        assert kb.get_task(conn, child) is not None
