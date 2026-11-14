"""Dependency gates for completed parent review verdicts."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kbc.connect() as connection:
        yield connection


def _complete(conn, title, *, metadata=None, summary="completed"):
    task_id = kb.create_task(conn, title=title, assignee="reviewer")
    assert kb.complete_task(conn, task_id, summary=summary, metadata=metadata)
    return task_id


def _wait_event(conn, task_id):
    return [event for event in kb.list_events(conn, task_id) if event.kind == "dependency_wait"][-1]


def test_done_nonapprove_verdict_holds_child_and_reports_reason(conn):
    parent = _complete(conn, "review", metadata={"verdict": "changes-required"})
    child = kb.create_task(conn, title="dependent", parents=[parent], assignee="worker")

    assert kb.get_task(conn, child).status == "todo"
    assert kb.unsatisfied_parents(conn, child) == [
        (parent, "done", "parent_verdict_not_approve", "changes_required")
    ]
    event = _wait_event(conn, child).payload
    assert {key: event[key] for key in ("reason", "parent", "verdict")} == {
        "reason": "parent_verdict_not_approve", "parent": parent, "verdict": "changes_required"
    }
    assert kb.recompute_ready(conn) == 0


@pytest.mark.parametrize("archive", [False, True])
def test_approved_verdict_releases_but_archived_rejection_holds(conn, archive):
    parent = _complete(conn, "review", metadata={"verdict": "LGTM"})
    child = kb.create_task(conn, title="dependent", parents=[parent], assignee="worker")
    assert kb.get_task(conn, child).status == "ready"

    rejected = _complete(conn, "rejected", metadata={"verdict": "reject"})
    held_child = kb.create_task(conn, title="held", parents=[rejected], assignee="worker")
    if archive:
        assert kb.archive_task(conn, rejected)
    assert kb.get_task(conn, held_child).status == "todo"
    assert kb.unsatisfied_parents(conn, held_child)[0][1:3] == (
        "archived" if archive else "done", "parent_verdict_not_approve"
    )


def test_rejection_summary_without_verdict_holds_child(conn):
    parent = _complete(conn, "review", summary="Changes requested: add regression coverage")
    child = kb.create_task(conn, title="dependent", parents=[parent], assignee="worker")

    assert kb.get_task(conn, child).status == "todo"
    assert kb.unsatisfied_parents(conn, child) == [
        (parent, "done", "parent_verdict_not_approve", "rejection_summary")
    ]


def test_no_verdict_nonreview_parent_keeps_legacy_release_behavior(conn):
    parent = _complete(conn, "implementation", summary="build and test complete")
    child = kb.create_task(conn, title="dependent", parents=[parent], assignee="worker")

    assert kb.get_task(conn, child).status == "ready"
    assert kb.unsatisfied_parents(conn, child) == []


def test_superseding_approved_parent_releases_held_child(conn):
    rejected = _complete(conn, "rejected review", metadata={"verdict": "changes required"})
    replacement = _complete(
        conn,
        "approved replacement",
        metadata={"verdict": "accepted", "supersedes": [rejected]},
    )
    child = kb.create_task(conn, title="dependent", parents=[rejected, replacement], assignee="worker")

    assert kb.get_task(conn, child).status == "ready"
    assert kb.unsatisfied_parents(conn, child) == []


@pytest.mark.parametrize("verdict, released", [("approved", True), ("changes required", False)])
def test_archived_replacement_supersedes_only_when_approved(conn, verdict, released):
    rejected = _complete(conn, "rejected review", metadata={"verdict": "reject"})
    replacement = _complete(
        conn, "archived replacement", metadata={"verdict": verdict, "supersedes": [rejected]},
    )
    child = kb.create_task(conn, title="dependent", parents=[rejected, replacement], assignee="worker")
    assert kb.get_task(conn, child).status == ("ready" if released else "todo")

    assert kb.archive_task(conn, replacement)
    assert kb.recompute_ready(conn) == 0
    assert kb.get_task(conn, child).status == ("ready" if released else "todo")
    blockers = {pid: reason for pid, _status, reason, _verdict in kb.unsatisfied_parents(conn, child)}
    if released:
        assert blockers == {}
    else:
        assert blockers == {
            rejected: "parent_verdict_not_approve", replacement: "parent_verdict_not_approve",
        }


def test_unlink_releases_held_child(conn):
    parent = _complete(conn, "rejected review", metadata={"verdict": "fail"})
    child = kb.create_task(conn, title="dependent", parents=[parent], assignee="worker")
    assert kb.get_task(conn, child).status == "todo"

    assert kb.unlink_tasks(conn, parent, child)
    assert kb.get_task(conn, child).status == "ready"


def test_recompute_demotes_existing_ready_child_with_audited_verdict_hold(conn):
    parent = _complete(conn, "review", metadata={"verdict": "rejected"})
    child = kb.create_task(conn, title="dependent", assignee="worker")
    assert kb.link_tasks(conn, parent, child) is True
    assert kb.get_task(conn, child).status == "todo"

    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
    conn.commit()
    assert kb.recompute_ready(conn) == 0
    assert kb.get_task(conn, child).status == "todo"
    event = _wait_event(conn, child)
    assert event.payload["reason"] == "parent_verdict_not_approve"
    assert event.payload["demoted"] is True


def test_reviewer_completion_requires_approved_structured_verdict(conn):
    task_id = kb.create_task(conn, title="reviewed work", assignee="reviewer")
    assert kb.request_review(conn, task_id)
    claimed = kb.claim_review_task(conn, task_id, claimer="reviewer")
    assert claimed is not None and claimed.current_run_id is not None

    with pytest.raises(kb.ReviewVerdictError):
        kb.complete_task(
            conn, task_id, summary="needs work", metadata={"verdict": "changes required"},
            expected_run_id=claimed.current_run_id,
        )
    assert kb.get_task(conn, task_id).status == "running"

    assert kb.request_changes(conn, task_id, reason="needs work", expected_run_id=claimed.current_run_id)
