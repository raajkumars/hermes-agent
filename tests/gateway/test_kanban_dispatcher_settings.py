"""Dispatcher settings parse their safety budgets explicitly."""

from __future__ import annotations

from types import SimpleNamespace

from gateway.kanban_watchers_dispatcher import _resolve_dispatcher_settings


def _kanban_db_stub():
    return SimpleNamespace(
        DEFAULT_FAILURE_LIMIT=2,
        resolve_max_in_progress=lambda configured: configured,
    )


def test_dispatcher_spawn_cap_defaults_to_twenty():
    settings = _resolve_dispatcher_settings({}, _kanban_db_stub())

    assert settings.max_spawns_without_completion == 20


def test_dispatcher_spawn_cap_accepts_zero_and_rejects_invalid_values(caplog):
    zero = _resolve_dispatcher_settings(
        {"max_spawns_without_completion": 0}, _kanban_db_stub()
    )
    negative = _resolve_dispatcher_settings(
        {"max_spawns_without_completion": -1}, _kanban_db_stub()
    )
    non_integer = _resolve_dispatcher_settings(
        {"max_spawns_without_completion": "not-a-number"}, _kanban_db_stub()
    )
    fractional = _resolve_dispatcher_settings(
        {"max_spawns_without_completion": 1.5}, _kanban_db_stub()
    )

    assert zero.max_spawns_without_completion == 0
    assert negative.max_spawns_without_completion == 20
    assert non_integer.max_spawns_without_completion == 20
    assert fractional.max_spawns_without_completion == 20
    assert "max_spawns_without_completion" in caplog.text