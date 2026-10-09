"""Shared completion-contract validation across Kanban creation surfaces."""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path
from collections.abc import Generator

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_pr_acceptance import validate_contract


def _graphql_required_checks(required: list[dict[str, object]]) -> dict[str, object]:
    return {"data": {"repository": {"defaultBranchRef": {
        "name": "main", "branchProtectionRule": {"requiredStatusChecks": required},
    }}}}


def _preflight_api(*, required: list[dict[str, object]] | None = None,
                   rulesets_unavailable: bool = False):
    def fake_api(endpoint: str, **kwargs):
        if endpoint == "graphql":
            if "pullRequest" in kwargs["query"]:
                return {"data": {"repository": {"pullRequest": {
                    "headRefOid": "a" * 40,
                    "baseRefName": "main",
                    "state": "OPEN",
                    "baseRef": {"branchProtectionRule": {"requiredStatusChecks": required or []}},
                }}}}
            return _graphql_required_checks(required or [])
        if endpoint.startswith("repos/") and endpoint.endswith("/rules/branches/main?per_page=100"):
            if rulesets_unavailable:
                raise OSError("rulesets unavailable")
            return [[]]
        raise AssertionError(f"unexpected GitHub API endpoint: {endpoint}")
    return fake_api


@pytest.fixture
def kanban_home(monkeypatch: pytest.MonkeyPatch) -> Generator[Path, None, None]:
    root = Path(tempfile.mkdtemp(prefix="hermes-contract-", dir="/var/tmp"))
    home = root / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: root)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    try:
        yield home
    finally:
        shutil.rmtree(root)


@pytest.mark.parametrize("placeholder", ["OWNER/REPO", "owner/repo", "Owner/Repo", "OWNER/repo"])
def test_validate_contract_rejects_literal_owner_repo_placeholder(placeholder: str) -> None:
    with pytest.raises(ValueError, match="placeholder"):
        validate_contract(placeholder)


@pytest.mark.parametrize("contract", ["qwickapps/aos", "https://github.com/qwickapps/aos/pull/659"])
def test_validate_contract_accepts_real_repo_and_exact_pr(contract: str) -> None:
    assert validate_contract(contract) == contract


def test_create_task_rejects_placeholder_before_persistence(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        before = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
        with pytest.raises(ValueError, match="placeholder"):
            kb.create_task(conn, title="bad contract", completion_contract="OWNER/REPO")
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == before


def test_cli_create_rejects_placeholder_before_persistence(
    kanban_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from hermes_cli import kanban_parser

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kanban_parser.build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args([
        "kanban", "create", "bad contract", "--assignee", "worker",
        "--completion-contract", "owner/repo",
    ])
    assert kanban_cli.kanban_command(args) != 0
    assert "placeholder" in capsys.readouterr().err
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_kanban_create_tool_rejects_placeholder_before_persistence(kanban_home: Path) -> None:
    from tools import kanban_tools

    result = json.loads(kanban_tools._handle_create({
        "title": "bad contract", "assignee": "worker", "completion_contract": "Owner/Repo",
    }))
    assert "placeholder" in json.dumps(result)
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


@pytest.mark.parametrize("placeholder", ["OWNER/REPO", "owner/repo", "Owner/Repo", "OWNER/repo"])
def test_rebind_contract_rejects_placeholder_before_mutation(
    kanban_home: Path, placeholder: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import kanban_pr_acceptance

    monkeypatch.setattr(kanban_pr_acceptance, "_api", _preflight_api(
        required=[{"context": "tests", "app": {"databaseId": 2}}],
    ))
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="bound", completion_contract="qwickapps/aos")
        with pytest.raises(ValueError, match="placeholder"):
            kb.rebind_contract(conn, task_id, new_contract=placeholder, reason="bad placeholder")
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.completion_contract == "qwickapps/aos"
        assert not [event for event in kb.list_events(conn, task_id) if event.kind == "contract_rebound"]


def test_kanban_create_tool_accepts_real_repo_contract(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import kanban_tools
    from hermes_cli import kanban_pr_acceptance

    monkeypatch.setattr(kanban_pr_acceptance, "_api", _preflight_api(
        required=[{"context": "test", "app": {"databaseId": 1}}],
    ))

    result = json.loads(kanban_tools._handle_create({
        "title": "real contract", "assignee": "worker", "completion_contract": "qwickapps/aos",
    }))
    assert result["ok"] is True
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, result["task_id"])
        assert task is not None
        assert task.completion_contract == "qwickapps/aos"


@pytest.mark.parametrize("contract", [
    "qwickapps/aos",
    "https://github.com/qwickapps/aos/pull/659",
])
def test_create_task_rejects_unprotected_contract_before_persistence(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch, contract: str,
) -> None:
    from hermes_cli import kanban_pr_acceptance

    monkeypatch.setattr(kanban_pr_acceptance, "_api", _preflight_api())
    with kbc.connect_closing() as conn:
        before = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
        with pytest.raises(ValueError, match="can never pass PR acceptance"):
            kb.create_task(conn, title="unprotected", completion_contract=contract)
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == before


def test_kanban_create_tool_returns_unprotected_contract_error(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import kanban_pr_acceptance
    from tools import kanban_tools

    monkeypatch.setattr(kanban_pr_acceptance, "_api", _preflight_api())
    result = json.loads(kanban_tools._handle_create({
        "title": "unprotected", "assignee": "worker", "completion_contract": "qwickapps/aos",
    }))
    assert "task_id" not in result
    assert "can never pass PR acceptance" in json.dumps(result)
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_create_task_accepts_protected_contract(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import kanban_pr_acceptance

    monkeypatch.setattr(kanban_pr_acceptance, "_api", _preflight_api(
        required=[{"context": "tests", "app": {"databaseId": 2}}],
    ))
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="protected", completion_contract="qwickapps/aos")
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.completion_contract == "qwickapps/aos"


def test_create_task_accepts_rulesets_unavailable_contract(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import kanban_pr_acceptance

    monkeypatch.setattr(kanban_pr_acceptance, "_api", _preflight_api(rulesets_unavailable=True))
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="free-tier", completion_contract="qwickapps/aos")
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.completion_contract == "qwickapps/aos"


def test_create_task_rejects_unverifiable_contract_before_persistence(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import kanban_pr_acceptance

    def unavailable(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(kanban_pr_acceptance, "_api", unavailable)
    with kbc.connect_closing() as conn:
        before = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
        with pytest.raises(ValueError, match="could not verify contract"):
            kb.create_task(conn, title="offline", completion_contract="qwickapps/aos")
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == before


def test_create_task_local_only_skips_contract_preflight(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import kanban_pr_acceptance

    def unexpected(*args, **kwargs):
        raise AssertionError("local-only must not call GitHub")

    monkeypatch.setattr(kanban_pr_acceptance, "_api", unexpected)
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="local", completion_contract="local-only")
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.completion_contract == "local-only"


def test_rebind_rejects_unprotected_contract_without_event(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import kanban_pr_acceptance

    monkeypatch.setattr(kanban_pr_acceptance, "_api", _preflight_api(
        required=[{"context": "tests", "app": {"databaseId": 2}}],
    ))
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="bound", completion_contract="qwickapps/aos")
        monkeypatch.setattr(kanban_pr_acceptance, "_api", _preflight_api())
        with pytest.raises(ValueError, match="can never pass PR acceptance"):
            kb.rebind_contract(conn, task_id, new_contract="https://github.com/qwickapps/aos/pull/659", reason="bad")
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.completion_contract == "qwickapps/aos"
        assert not [event for event in kb.list_events(conn, task_id) if event.kind == "contract_rebound"]


def test_rebind_accepts_protected_contract(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import kanban_pr_acceptance

    monkeypatch.setattr(kanban_pr_acceptance, "_api", _preflight_api(
        required=[{"context": "tests", "app": {"databaseId": 2}}],
    ))
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="bound", completion_contract="qwickapps/aos")
        assert kb.rebind_contract(
            conn, task_id, new_contract="https://github.com/qwickapps/aos/pull/659", reason="updated PR",
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.completion_contract == "https://github.com/qwickapps/aos/pull/659"
