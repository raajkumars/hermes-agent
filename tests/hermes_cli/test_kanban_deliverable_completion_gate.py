"""Deliverable epics close only on their owner's evidence, and regressions reopen them."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_deliverables as kd


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect() as connection:
        yield connection


def _running_task(conn, title: str) -> str:
    task_id = kb.create_task(conn, title=title, assignee="ira")
    assert kb.claim_task(conn, task_id, claimer=kb._claimer_id()) is not None
    return task_id


def test_only_deliverable_owner_can_complete_epic_with_dod_evidence(conn, monkeypatch):
    epic = _running_task(conn, "deliverable epic")
    kd.upsert_deliverable(conn, "ship", rank=1, owner="ira", epic_task_id=epic)
    assert kb.edit_task(conn, epic, deliverable_key="ship")

    monkeypatch.setattr(kb, "_actor_profile", lambda: "other")
    with pytest.raises(kb.DeliverableCompletionError, match="may only be completed"):
        kb.complete_task(
            conn, epic, summary="done", metadata={"dod_check": "passed", "dod_evidence": ["https://ci.example/run"]},
        )
    assert kb.get_task(conn, epic).status == "running"

    monkeypatch.setattr(kb, "_actor_profile", lambda: "ira")
    with pytest.raises(kb.DodNotVerifiedError, match="dod_check"):
        kb.complete_task(conn, epic, summary="done", metadata={"dod_evidence": ["https://ci.example/run"]})
    with pytest.raises(kb.DodNotVerifiedError, match="dod_evidence"):
        kb.complete_task(conn, epic, summary="done", metadata={"dod_check": "passed"})
    assert kb.complete_task(
        conn, epic, summary="done", metadata={"dod_check": "passed", "dod_evidence": ["https://ci.example/run", "abc123"]},
    ) is True
    assert kb.get_task(conn, epic).status == "done"


def test_member_dod_regression_reopens_linked_epic(conn, monkeypatch):
    monkeypatch.setattr(kb, "_actor_profile", lambda: "ira")
    member = kb.create_task(conn, title="member", assignee="ira")
    epic = kb.create_task(conn, title="epic", assignee="ira")
    kd.upsert_deliverable(conn, "ship", rank=1, owner="ira", epic_task_id=epic)
    assert kb.edit_task(conn, member, deliverable_key="ship")
    assert kb.edit_task(conn, epic, deliverable_key="ship")
    assert kb.link_tasks(conn, member, epic)

    assert kb.claim_task(conn, member, claimer=kb._claimer_id()) is not None
    assert kb.complete_task(conn, member, summary="initial member proof") is True
    assert kb.claim_task(conn, epic, claimer=kb._claimer_id()) is not None
    assert kb.complete_task(
        conn, epic, summary="all done", metadata={"dod_check": "passed", "dod_evidence": ["https://ci.example/green"]},
    ) is True
    assert kb.get_task(conn, epic).status == "done"

    # A later member run discovers the regression after the epic was closed.
    conn.execute("UPDATE tasks SET status = 'ready', completed_at = NULL WHERE id = ?", (member,))
    conn.commit()
    assert kb.claim_task(conn, member, claimer=kb._claimer_id()) is not None
    assert kb.complete_task(conn, member, summary="regression found", metadata={"dod_regressed": True}) is True
    # Invalidation retracts the completed epic; normal readiness recomputation may then reopen it as ready.
    assert kb.get_task(conn, epic).status == "ready"
    events = kb.list_events(conn, epic)
    assert any(event.kind == "descendant_invalidated" for event in events)