"""Capacity-exhaustion Kanban workers retain the live turn measurements."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import cli
import pytest


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated board where a claimed worker can take its real exit path."""
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _install_kanban_stubs(monkeypatch):
    from hermes_cli import kanban_db, kanban_db_connect

    calls = []
    monkeypatch.setattr(
        kanban_db, "block_task",
        lambda conn, task_id, **kwargs: calls.append((conn, task_id, kwargs)) or True,
    )
    monkeypatch.setattr(kanban_db_connect, "connect_closing", lambda: nullcontext("connection"))
    return calls


def test_capacity_block_records_typed_reason_and_live_measurements(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t-capacity")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "17")
    calls = _install_kanban_stubs(monkeypatch)
    agent = SimpleNamespace(model="fallback-model", context_compressor=SimpleNamespace(
        context_length=32768, last_real_prompt_tokens=0,
    ))

    assert cli._block_kanban_capacity_exhaustion(SimpleNamespace(agent=agent), {
        "failure_reason": "context_window_full", "model": "live-model", "last_prompt_tokens": 32001,
    }) is True

    assert calls == [("connection", "t-capacity", {
        "reason": "context_window_full; model=live-model; context_limit=32768; context_tokens=32001",
        "kind": "capability", "expected_run_id": 17,
    })]


def test_repetition_uses_the_last_real_provider_measurement(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t-repeat")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    calls = _install_kanban_stubs(monkeypatch)
    agent = SimpleNamespace(model="loop-model", context_compressor=SimpleNamespace(
        context_length=16384, last_real_prompt_tokens=15000,
    ))

    assert cli._block_kanban_capacity_exhaustion(SimpleNamespace(agent=agent), {
        "failure_reason": "repetition_loop", "last_prompt_tokens": 0,
    }) is True
    assert calls[0][2]["reason"] == (
        "repetition_loop; model=loop-model; context_limit=16384; context_tokens=15000"
    )


def test_non_capacity_failure_or_missing_measurements_does_not_block(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t-noop")
    calls = _install_kanban_stubs(monkeypatch)
    agent = SimpleNamespace(model="model", context_compressor=SimpleNamespace(
        context_length=8192, last_real_prompt_tokens=0,
    ))

    assert cli._block_kanban_capacity_exhaustion(SimpleNamespace(agent=agent), {
        "failure_reason": "timeout", "last_prompt_tokens": 8000,
    }) is False
    assert cli._block_kanban_capacity_exhaustion(SimpleNamespace(agent=agent), {
        "failure_reason": "context_overflow", "last_prompt_tokens": 0,
    }) is False
    assert calls == []


def test_quiet_worker_capacity_failure_blocks_its_claimed_card_in_the_live_board(monkeypatch, kanban_home):
    """A real ``-Q`` worker path carries the observed model capacity into the board.

    This deliberately does not stub ``block_task`` or the database connection: the
    worker has to preserve its run ownership and leave both the task and its event
    trail in the state operators query after a context-window failure.
    """
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="capacity worker", assignee="worker")
        assert kb.claim_task(conn, task_id, claimer="worker") is not None
        run_id = kb._current_run_id(conn, task_id)
    assert run_id is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    def run_conversation(**_kwargs):
        return {
            "failed": True,
            "failure_reason": "context_window_full",
            "model": "observed-model",
            "last_prompt_tokens": 31_500,
        }

    agent = SimpleNamespace(
        model="fallback-model",
        context_compressor=SimpleNamespace(context_length=32_768, last_real_prompt_tokens=31_400),
        run_conversation=run_conversation,
        session_id="capacity-session",
    )
    worker_cli = SimpleNamespace(agent=agent, conversation_history=[], session_id="capacity-session")

    with pytest.raises(SystemExit) as exited:
        cli._run_quiet_single_query(worker_cli, "force deterministic context exhaustion")
    assert exited.value.code == 1

    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        blocks = [event for event in kb.list_events(conn, task_id) if event.kind == "blocked"]

    expected_reason = (
        "context_window_full; model=observed-model; context_limit=32768; context_tokens=31500"
    )
    assert task is not None
    assert run is not None
    assert (task.status, task.block_kind) == ("blocked", "capability")
    assert (run.outcome, run.summary) == ("blocked", expected_reason)
    assert len(blocks) == 1
    assert blocks[0].run_id == run_id
    assert blocks[0].payload is not None
    assert blocks[0].payload["reason"] == expected_reason
    assert blocks[0].payload["kind"] == task.block_kind
    assert blocks[0].payload["source_status"] == "ready"
