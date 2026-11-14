"""Approval-request records must never become ordinary Kanban work."""
from __future__ import annotations

import json
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
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _request_body() -> str:
    return json.dumps({
        "type": "qwick_approval_request",
        "v": 2,
        "scope_key": "test-only",
        "approver": "prime",
    })


def _adjudication_body(request_id: str) -> str:
    return json.dumps({
        "type": "qwick_approval_adjudication",
        "v": 2,
        "request_id": request_id,
        "approver": "prime",
    })


def _blocked_request(conn, *, assignee=None) -> str:
    task_id = kb.create_task(
        conn,
        title="Approval request",
        body=_request_body(),
        assignee=assignee,
        initial_status="blocked",
    )
    conn.execute(
        "UPDATE tasks SET block_kind = 'needs_input' WHERE id = ?",
        (task_id,),
    )
    kb._append_event(conn, task_id, "blocked", {
        "kind": "needs_input",
        "reason": "awaiting delegated approval adjudication",
    })
    return task_id


def test_ready_unassigned_request_is_not_default_assigned_or_spawned(kanban_home, all_assignees_spawnable):
    with kbc.connect() as conn:
        request_id = kb.create_task(conn, title="Approval request", body=_request_body())
        result = kbd.dispatch_once(
            conn, max_spawn=10, default_assignee="prime", dry_run=False,
        )
        task = kb.get_task(conn, request_id)

    assert result.spawned == []
    assert result.auto_assigned_default == []
    assert result.skipped_approval_request == [request_id]
    assert task is not None and task.assignee is None and task.status == "ready"


def test_ready_assigned_request_is_not_spawned(kanban_home, all_assignees_spawnable):
    with kbc.connect() as conn:
        request_id = kb.create_task(
            conn, title="Approval request", body=_request_body(), assignee="prime",
        )
        result = kbd.dispatch_once(conn, max_spawn=10, dry_run=True)

    assert result.spawned == []
    assert result.skipped_approval_request == [request_id]


def test_gateway_still_dispatches_adjudication_while_skipping_request(kanban_home, all_assignees_spawnable):
    with kbc.connect() as conn:
        request_id = kb.create_task(conn, title="Approval request", body=_request_body(), assignee="prime")
        adjudication_id = kb.create_task(
            conn,
            title="Delegated approval adjudication",
            body=_adjudication_body(request_id),
            assignee="prime",
        )
        result = kbd.dispatch_once(
            conn, max_spawn=10, dry_run=True, gateway_dispatcher=True,
        )

    assert [task_id for task_id, _assignee, _workspace in result.spawned] == [adjudication_id]
    assert result.skipped_approval_request == [request_id]
    assert result.skipped_gateway_only == []


def test_generic_unblock_answer_and_promote_refuse_approval_request(kanban_home):
    with kbc.connect() as conn:
        request_id = _blocked_request(conn)
        with pytest.raises(kb.ApprovalReleaseRefused, match="signed decide_approval path") as unblock_exc:
            kb.unblock_task(conn, request_id)
        assert unblock_exc.value.code == "approval_authority_only"
        ok, reason = kb.promote_task(conn, request_id, actor="triage")
        assert ok is False
        assert "signed decide_approval path" in (reason or "")
        with pytest.raises(kb.AnswerRefused, match="signed decide_approval path") as exc:
            kb.answer_blocked_task(conn, request_id, answerer="triage", body="release it")
        assert exc.value.code == "approval_authority_only"
        task = kb.get_task(conn, request_id)

    assert task is not None and task.status == "blocked"


def test_generic_release_refuses_adjudication_wait_even_for_non_request_card(kanban_home):
    with kbc.connect() as conn:
        task_id = kb.create_task(
            conn, title="Protected wait", body="ordinary body", initial_status="blocked",
        )
        kb._append_event(conn, task_id, "blocked", {
            "kind": "needs_input",
            "reason": "awaiting delegated approval adjudication",
        })
        with pytest.raises(kb.ApprovalReleaseRefused, match="delegated approval adjudication"):
            kb.unblock_task(conn, task_id)
        ok, reason = kb.promote_task(conn, task_id, actor="triage")
        task = kb.get_task(conn, task_id)

    assert ok is False
    assert "delegated approval adjudication" in (reason or "")
    assert task is not None and task.status == "blocked"


def _assert_still_blocked(task_id: str) -> None:
    with kbc.connect() as conn:
        task = kb.get_task(conn, task_id)
        unblocked = [e for e in kb.list_events(conn, task_id) if e.kind == "unblocked"]
    assert task is not None and task.status == "blocked" and task.assignee is None
    assert unblocked == []


def test_generic_actor_unblock_tool_returns_authorization_error(kanban_home, monkeypatch):
    """An orchestrator-scoped ``kanban_unblock`` (any generic actor, not the signed
    decide_approval path) is refused with the authorization reason, not a
    misleading "not blocked or unknown", and the card stays blocked."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    with kbc.connect() as conn:
        request_id = _blocked_request(conn)
    from tools import kanban_tools as kt

    out = json.loads(kt._handle_unblock({"task_id": request_id}))

    assert "error" in out and out.get("ok") is not True
    assert "signed decide_approval path" in out["error"]
    assert "not blocked or unknown" not in out["error"]
    _assert_still_blocked(request_id)


def test_generic_actor_unblock_cli_exits_nonzero_with_authorization_error(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    with kbc.connect() as conn:
        request_id = _blocked_request(conn)
    from hermes_cli import kanban as kc

    output = kc.run_slash(f"unblock {request_id}")

    assert f"refused to unblock {request_id}" in output
    assert "signed decide_approval path" in output
    assert "Unblocked" not in output
    _assert_still_blocked(request_id)


def test_generic_actor_dashboard_unblock_is_forbidden(kanban_home):
    import importlib.util
    import sys

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    plugin_file = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_plugin_approval_guard_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    with kbc.connect() as conn:
        request_id = _blocked_request(conn)

    r = TestClient(app).patch(f"/api/plugins/kanban/tasks/{request_id}", json={"status": "ready"})

    assert r.status_code == 403, r.text
    assert "signed decide_approval path" in r.json()["detail"]
    _assert_still_blocked(request_id)


def test_signed_decision_terminal_write_remains_possible(kanban_home):
    """qwick-guard's signed decision bypasses generic lifecycle helpers.

    The attested plugin records a terminal approval directly in its authority
    transaction; this guard only rejects generic queue releases.
    """
    with kbc.connect() as conn:
        request_id = _blocked_request(conn)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'done', assignee = 'prime', block_kind = NULL "
                "WHERE id = ? AND status = 'blocked'",
                (request_id,),
            )
        task = kb.get_task(conn, request_id)

    assert task is not None and task.status == "done" and task.assignee == "prime"
