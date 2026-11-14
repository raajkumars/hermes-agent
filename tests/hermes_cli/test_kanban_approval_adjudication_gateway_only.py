"""Approval-adjudication cards must be spawned by the gateway alone."""
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


def _adjudication_body() -> str:
    return json.dumps({
        "type": "qwick_approval_adjudication",
        "v": 2,
        "request_id": "t_approval_example",
        "approver": "prime",
    })


def test_non_gateway_dispatch_skips_adjudication_cards(kanban_home, all_assignees_spawnable):
    """CLI/dashboard/sweep dispatches must not create an authority-invalid worker."""
    with kbc.connect() as conn:
        ordinary = kb.create_task(conn, title="ordinary", assignee="prime", body="ordinary work")
        adjudication = kb.create_task(
            conn, title="Delegated approval adjudication", assignee="prime", body=_adjudication_body(),
        )
        result = kbd.dispatch_once(conn, dry_run=True, max_spawn=10)

    assert [task_id for task_id, _assignee, _workspace in result.spawned] == [ordinary]
    assert result.skipped_gateway_only == [adjudication]


def test_gateway_dispatch_claims_adjudication_cards(kanban_home, all_assignees_spawnable):
    """The embedded gateway opts in, so the authority sees its expected parent."""
    with kbc.connect() as conn:
        adjudication = kb.create_task(
            conn, title="Delegated approval adjudication", assignee="prime", body=_adjudication_body(),
        )
        result = kbd.dispatch_once(
            conn, dry_run=True, max_spawn=10, gateway_dispatcher=True,
        )

    assert [task_id for task_id, _assignee, _workspace in result.spawned] == [adjudication]
