"""Dependency gates for completed parent review verdicts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


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


def _approved_review(conn, title, *, metadata=None, summary="approved"):
    task_id = kb.create_task(conn, title=title, assignee="reviewer")
    assert kb.request_review(conn, task_id)
    claimed = kb.claim_review_task(conn, task_id, claimer="reviewer")
    assert claimed is not None and claimed.current_run_id is not None
    metadata = {"verdict": "approved", **(metadata or {})}
    assert kb.complete_task(conn, task_id, summary=summary, metadata=metadata,
                            expected_run_id=claimed.current_run_id)
    return task_id


def _legacy_rejected_review(conn, title, *, metadata=None, summary="changes requested"):
    """Persist the terminal legacy state the guard was introduced to contain.

    Current review completion rejects non-approvals and routes them through
    ``request_changes``. Older board rows could have a completed reviewer run
    with a rejecting handoff, which must remain held safely.
    """
    task_id = kb.create_task(conn, title=title, assignee="reviewer")
    assert kb.request_review(conn, task_id)
    claimed = kb.claim_review_task(conn, task_id, claimer="reviewer")
    assert claimed is not None and claimed.current_run_id is not None
    conn.execute(
        "UPDATE task_runs SET status='completed', outcome='completed', summary=?, metadata=?, ended_at=started_at "
        "WHERE id=?",
        (summary, json.dumps({"verdict": "changes required"} if metadata is None else metadata), claimed.current_run_id),
    )
    conn.execute(
        "UPDATE tasks SET status='done', current_run_id=NULL, claim_lock=NULL, claim_expires=NULL "
        "WHERE id=?", (task_id,),
    )
    conn.commit()
    return task_id


def _wait_event(conn, task_id):
    return [event for event in kb.list_events(conn, task_id) if event.kind == "dependency_wait"][-1]


def test_legacy_rejected_review_holds_child_and_reports_reason(conn):
    parent = _legacy_rejected_review(conn, "review", metadata={"verdict": "changes-required"})
    child = kb.create_task(conn, title="dependent", parents=[parent], assignee="worker")

    assert kb.get_task(conn, child).status == "blocked"
    assert kb.unsatisfied_parents(conn, child) == [
        (parent, "done", "parent_verdict_not_approve", "changes_required")
    ]
    event = _wait_event(conn, child).payload
    assert {key: event[key] for key in ("reason", "parent", "verdict")} == {
        "reason": "parent_verdict_not_approve", "parent": parent, "verdict": "changes_required"
    }
    assert kb.recompute_ready(conn) == 0


@pytest.mark.parametrize(
    ("metadata", "summary"),
    [
        ({"verdict": "already_satisfied"}, "completed"),
        ({"verdict": "implementation_complete"}, "completed"),
        ({"verdict": "no_op"}, "completed"),
        ({"verdict": "verified"}, "completed"),
        ({"verdict": "merged"}, "completed"),
        (None, "Rejected test input and added coverage"),
    ],
)
def test_nonreview_handoffs_never_hold_children(conn, metadata, summary):
    parent = _complete(conn, "implementation", metadata=metadata, summary=summary)
    child = kb.create_task(conn, title="dependent", parents=[parent], assignee="worker")

    assert kb.get_task(conn, child).status == "ready"
    assert kb.unsatisfied_parents(conn, child) == []


@pytest.mark.parametrize("archive", [False, True])
def test_approved_verdict_releases_but_archived_rejection_holds(conn, archive):
    parent = _approved_review(conn, "review", metadata={"verdict": "LGTM"})
    child = kb.create_task(conn, title="dependent", parents=[parent], assignee="worker")
    assert kb.get_task(conn, child).status == "ready"

    rejected = _legacy_rejected_review(conn, "rejected", metadata={"verdict": "reject"})
    held_child = kb.create_task(conn, title="held", parents=[rejected], assignee="worker")
    if archive:
        assert kb.archive_task(conn, rejected)
    assert kb.get_task(conn, held_child).status == "blocked"
    assert kb.unsatisfied_parents(conn, held_child)[0][1:3] == (
        "archived" if archive else "done", "parent_verdict_not_approve"
    )


def test_rejection_summary_without_verdict_holds_child(conn):
    parent = _legacy_rejected_review(conn, "review", metadata={}, summary="Changes requested: add regression coverage")
    child = kb.create_task(conn, title="dependent", parents=[parent], assignee="worker")

    assert kb.get_task(conn, child).status == "blocked"
    assert kb.unsatisfied_parents(conn, child) == [
        (parent, "done", "parent_verdict_not_approve", "rejection_summary")
    ]


def test_superseding_approved_review_releases_held_child(conn):
    rejected = _legacy_rejected_review(conn, "rejected review", metadata={"verdict": "changes required"})
    replacement = _approved_review(conn, "approved replacement", metadata={"supersedes": [rejected]})
    child = kb.create_task(conn, title="dependent", parents=[rejected, replacement], assignee="worker")

    assert kb.get_task(conn, child).status == "ready"
    assert kb.unsatisfied_parents(conn, child) == []


@pytest.mark.parametrize("archive", [False, True])
def test_rejecting_replacement_does_not_release_held_child(conn, archive):
    rejected = _legacy_rejected_review(conn, "rejected review", metadata={"verdict": "reject"})
    replacement = _legacy_rejected_review(
        conn, "rejected replacement", metadata={"verdict": "changes required", "supersedes": [rejected]})
    child = kb.create_task(conn, title="dependent", parents=[rejected, replacement], assignee="worker")

    assert kb.get_task(conn, child).status == "blocked"
    if archive:
        assert kb.archive_task(conn, replacement)
    assert kb.recompute_ready(conn) == 0
    assert kb.get_task(conn, child).status == "blocked"
    blockers = {pid: reason for pid, _status, reason, _verdict in kb.unsatisfied_parents(conn, child)}
    assert blockers == {
        rejected: "parent_verdict_not_approve", replacement: "parent_verdict_not_approve",
    }


def test_unlink_releases_held_child(conn):
    parent = _legacy_rejected_review(conn, "rejected review", metadata={"verdict": "fail"})
    child = kb.create_task(conn, title="dependent", parents=[parent], assignee="worker")
    assert kb.get_task(conn, child).status == "blocked"

    assert kb.unlink_tasks(conn, parent, child)
    assert kb.get_task(conn, child).status == "ready"


def test_recompute_demotes_existing_ready_child_with_audited_verdict_hold(conn):
    parent = _legacy_rejected_review(conn, "review", metadata={"verdict": "rejected"})
    child = kb.create_task(conn, title="dependent", assignee="worker")
    assert kb.link_tasks(conn, parent, child) is True
    assert kb.get_task(conn, child).status == "blocked"

    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
    conn.commit()
    assert kb.recompute_ready(conn) == 0
    assert kb.get_task(conn, child).status == "blocked"
    event = _wait_event(conn, child)
    assert event.payload["reason"] == "parent_verdict_not_approve"
    assert event.payload["demoted"] is True


def test_list_parent_verdict_holds_reports_only_actual_review_holds(conn):
    held_parent = _legacy_rejected_review(conn, "held review", metadata={"verdict": "reject"})
    held_child = kb.create_task(conn, title="held child", parents=[held_parent], assignee="worker")
    nonreview_parent = _complete(conn, "verified implementation", metadata={"verdict": "verified"})
    released_child = kb.create_task(conn, title="released child", parents=[nonreview_parent], assignee="worker")

    assert kb.list_parent_verdict_holds(conn) == [{
        "task_id": held_child, "parent_id": held_parent, "verdict": "reject",
    }]
    assert kb.get_task(conn, released_child).status == "ready"


def test_dispatch_periodically_reports_current_parent_verdict_holds(conn):
    parent = _legacy_rejected_review(conn, "held review", metadata={"verdict": "reject"})
    child = kb.create_task(conn, title="held child", parents=[parent], assignee="worker")

    result = kbd.dispatch_once(conn, spawn_fn=lambda *_args: None, max_spawn=0)

    assert result.parent_verdict_holds == [{
        "task_id": child, "parent_id": parent, "verdict": "reject",
    }]


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
