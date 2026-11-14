"""Capacity-exhaustion Kanban workers retain the live turn measurements."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import cli


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
