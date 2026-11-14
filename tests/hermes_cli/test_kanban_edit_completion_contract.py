"""`kanban edit --completion-contract`: rebind through the shared create-time validator."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_pr_acceptance import validate_contract


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _placeholder_blocked_card():
    """Reproduce t_c47946eb: blocked, needs_input, contract persisted as OWNER/REPO.

    create_task now rejects the placeholder, so the legacy row is written directly.
    """
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="pacing reference", assignee="vaidya")
        conn.execute(
            "UPDATE tasks SET status='blocked', block_kind='needs_input', "
            "completion_contract='OWNER/REPO' WHERE id=?", (tid,))
        conn.commit()
    return tid


def _contract(tid):
    with kbc.connect_closing() as conn:
        return kb.get_task(conn, tid).completion_contract


def test_rebind_placeholder_to_local_only(kanban_home):
    tid = _placeholder_blocked_card()
    assert _contract(tid) == "OWNER/REPO"
    out = kc.run_slash(f"edit {tid} --completion-contract local-only")
    assert _contract(tid) == "local-only"
    assert f"Edited {tid}" in out
    with kbc.connect_closing() as conn:
        status = kb.get_task(conn, tid).status
        edited = [e for e in kb.list_events(conn, tid) if e.kind == "edited"]
    assert status == "blocked"  # rebinding does not unblock or complete
    assert edited[-1].payload["completion_contract"] == {"old": "OWNER/REPO", "new": "local-only"}


def test_rebind_to_real_repo(kanban_home):
    tid = _placeholder_blocked_card()
    kc.run_slash(f"edit {tid} --completion-contract qwickapps/aos")
    assert _contract(tid) == "qwickapps/aos"


@pytest.mark.parametrize("bad", ["not a contract", "owner", "http://x/y", "OWNER/REPO", "owner/repo", "Owner/Repo"])
def test_invalid_value_rejected_by_shared_validator(kanban_home, bad):
    tid = _placeholder_blocked_card()
    with pytest.raises(ValueError):
        validate_contract(bad)  # the very function create_task uses
    out = kc.run_slash(f"edit {tid} --completion-contract '{bad}'")
    assert _contract(tid) == "OWNER/REPO"  # unchanged
    assert "kanban edit:" in out and "completion_contract" in out


def test_placeholder_cannot_be_reintroduced_on_a_good_card(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", completion_contract="acme/repo")
    kc.run_slash(f"edit {tid} --completion-contract OWNER/REPO")
    assert _contract(tid) == "acme/repo"
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match="OWNER/REPO"):
            kb.edit_task(conn, tid, completion_contract="OWNER/REPO")


@pytest.mark.parametrize("status", ["running", "done", "archived"])
def test_rebind_refused_in_illegitimate_state(kanban_home, status):
    tid = _placeholder_blocked_card()
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        conn.commit()
        with pytest.raises(ValueError, match="cannot rebind"):
            kb.edit_task(conn, tid, completion_contract="local-only")
    assert _contract(tid) == "OWNER/REPO"


def test_pr_bound_contract_cannot_be_replaced(kanban_home):
    pr = "https://github.com/acme/repo/pull/7"
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="x", completion_contract=pr)
        with pytest.raises(ValueError, match="binds once"):
            kb.edit_task(conn, tid, completion_contract="local-only")
    assert _contract(tid) == pr


def test_edit_requires_some_field(kanban_home):
    tid = _placeholder_blocked_card()
    assert "--completion-contract" in kc.run_slash(f"edit {tid}")
