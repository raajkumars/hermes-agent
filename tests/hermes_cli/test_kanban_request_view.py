"""Request-level Kanban views must describe delivery, not only card status."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _view(conn, task_id: str) -> dict:
    task = kb.get_task(conn, task_id)
    assert task is not None
    return kb.compute_request_view(conn, task)


def test_done_parent_with_open_dependent_child_is_delivery_pending(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="coordinate", assignee="anika")
        child = kb.create_task(conn, title="review", assignee="reviewer", parents=[parent])
        assert kb.complete_task(conn, parent, summary="implementation handed off")

        view = _view(conn, parent)

    assert view["verified_outcome"] == "orchestration_done_delivery_pending"
    assert view["open_dependent_children"] == [{"id": child, "status": "ready"}]
    assert view["next_action"] == f"waiting on dependent children: {child} (ready)"


def test_terminal_children_do_not_block_delivered_outcome(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="coordinate", assignee="anika")
        child = kb.create_task(conn, title="qa", assignee="reviewer", parents=[parent])
        assert kb.complete_task(conn, parent, summary="released")
        claimed = kb.claim_task(conn, child, claimer="reviewer")
        assert claimed is not None
        assert kb.complete_task(conn, child, summary="qa passed", expected_run_id=claimed.current_run_id)

        view = _view(conn, parent)

    assert view["verified_outcome"] == "delivered"
    assert view["open_dependent_children"] == []
    assert view["next_action"] is None


def test_progress_and_wait_age_are_evidence_backed(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="review me", assignee="meera")
        claimed = kb.claim_task(conn, task_id, claimer="meera")
        assert claimed is not None
        assert kb.request_review(conn, task_id, summary="worker handoff", reviewer="meera", expected_run_id=claimed.current_run_id)
        row = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? AND kind = 'review_requested' "
            "ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
        assert row is not None
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET created_at = ? WHERE id = ?", (int(time.time()) - 120, row["id"]))
        kb.add_comment(conn, task_id, author="operator", body="latest verified note")
        with kb.write_txn(conn):
            conn.execute("UPDATE task_comments SET created_at = ? WHERE task_id = ?", (int(time.time()) + 1, task_id))

        view = _view(conn, task_id)

    assert view["verified_outcome"] == "waiting"
    assert view["last_verified_progress"] == "latest verified note"
    assert view["blocker_age_seconds"] >= 120
    assert view["next_action"] == "waiting on reviewer (meera)"


def test_tool_and_cli_json_expose_same_request_view(kanban_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tools import kanban_tools as kt

    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="queued", assignee="anika")
        task = kb.get_task(conn, task_id)
        assert task is not None
        summary = kt._task_summary_dict(kb, conn, task)

    assert summary["owner"] == "anika"
    assert summary["verified_outcome"] == "in_progress"
    assert summary["next_action"] == "queued; will run once a worker/dispatcher picks it up"

    list_args = argparse.Namespace(
        assignee=None, mine=False, status=None, tenant=None, session=None, archived=False,
        sort=None, workflow_template_id=None, step=None, json=True,
    )
    assert kanban_cli._cmd_list(list_args) == 0
    listed = json.loads(capsys.readouterr().out)
    row = next(item for item in listed if item["id"] == task_id)
    assert row["request_view"]["verified_outcome"] == "in_progress"

    show_args = argparse.Namespace(task_id=task_id, json=True, run_state=None)
    assert kanban_cli._cmd_show(show_args) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["request_view"]["owner"] == "anika"


def test_changes_requested_child_keeps_done_parent_delivery_pending(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="coordinate", assignee="anika")
        child = kb.create_task(conn, title="review", assignee="anika", parents=[parent])
        assert kb.complete_task(conn, parent, summary="implementation handed off")
        implementation = kb.claim_task(conn, child, claimer="anika:implementation")
        assert implementation is not None
        assert kb.request_review(conn, child, summary="ready for review", reviewer="meera", expected_run_id=implementation.current_run_id)
        review = kb.claim_review_task(conn, child, claimer="meera:review")
        assert review is not None
        changed, implementer = kb.request_changes(conn, child, reason="needs revision", expected_run_id=review.current_run_id)
        assert changed and implementer == "anika"

        view = _view(conn, parent)

    assert view["verified_outcome"] == "orchestration_done_delivery_pending"
    assert view["open_dependent_children"] == [{"id": child, "status": "ready"}]


def test_terminal_rejecting_review_child_keeps_done_parent_delivery_pending(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="coordinate", assignee="anika")
        child = kb.create_task(conn, title="review", assignee="meera", parents=[parent])
        assert kb.complete_task(conn, parent, summary="implementation handed off")
        claimed = kb.claim_task(conn, child, claimer="meera")
        assert claimed is not None
        assert kb.complete_task(
            conn, child, summary="review requested changes", metadata={"verdict": "request_changes"},
            expected_run_id=claimed.current_run_id,
        )

        view = _view(conn, parent)

    assert view["verified_outcome"] == "orchestration_done_delivery_pending"
    assert view["open_dependent_children"] == [
        {"id": child, "status": "done", "verdict": "request_changes"}
    ]
    assert view["next_action"] == f"review {child} requested changes"


def test_terminal_approved_child_does_not_block_delivery(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="coordinate", assignee="anika")
        child = kb.create_task(conn, title="review", assignee="meera", parents=[parent])
        assert kb.complete_task(conn, parent, summary="implementation handed off")
        claimed = kb.claim_task(conn, child, claimer="meera")
        assert claimed is not None
        assert kb.complete_task(
            conn, child, summary="review approved", metadata={"verdict": "approved"},
            expected_run_id=claimed.current_run_id,
        )

        view = _view(conn, parent)

    assert view["verified_outcome"] == "delivered"
    assert view["open_dependent_children"] == []
