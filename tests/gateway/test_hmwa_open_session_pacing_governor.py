"""Direct unit tests of GatewayTurnMixin._hmwa_open_session's pacing-governor wiring (t_e71698f5):
the real code path gateway/gateway_pacing.py is called from. Minimal stub runner exercising only
the surface _hmwa_open_session touches, rather than a full GatewayRunner harness."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.run_turn import GatewayTurnMixin


class _StubHooks:
    def __init__(self):
        self.emit = AsyncMock()


class _StubRunner(GatewayTurnMixin):
    """Only the attributes/methods _hmwa_open_session actually touches."""

    def __init__(self):
        self.hooks = _StubHooks()
        self.async_session_store = SimpleNamespace(set_model_override=AsyncMock())
        self._overrides: dict = {}

    def _session_model_override(self, session_key):
        return self._overrides.get(session_key)

    @property
    def _session_model_overrides(self):
        return self._overrides


def _new_session_entry():
    # created_at == updated_at is exactly the "_is_new_session" signal _hmwa_open_session reads.
    return SimpleNamespace(
        created_at=1000, updated_at=1000, session_id="sess-1",
        was_auto_reset=False, is_fresh_reset=False,
    )


def _resumed_session_entry():
    return SimpleNamespace(
        created_at=1000, updated_at=2000, session_id="sess-1",
        was_auto_reset=False, is_fresh_reset=False,
    )


def _source(profile="tara"):
    return SimpleNamespace(platform=None, user_id="u1", profile=profile)


def test_new_session_gets_routed_when_governor_says_reroute(monkeypatch):
    """ACCEPTANCE: a brand-new session is routed away from an over-pace primary, and the
    resulting override is BOTH set in-memory and persisted (sticky across restarts)."""
    monkeypatch.setattr(
        "gateway.gateway_pacing.resolve_new_session_model_override",
        lambda profile, config: {"model": "gpt-5.6-terra", "provider": "openai-codex"},
    )
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})

    runner = _StubRunner()
    was_auto_reset, is_new = asyncio.run(
        runner._hmwa_open_session(_new_session_entry(), "session-key-1", _source())
    )

    assert is_new is True
    assert runner._overrides["session-key-1"] == {"model": "gpt-5.6-terra", "provider": "openai-codex"}
    runner.async_session_store.set_model_override.assert_awaited_once_with(
        "session-key-1", {"model": "gpt-5.6-terra", "provider": "openai-codex"},
    )


def test_new_session_no_route_leaves_no_override(monkeypatch):
    monkeypatch.setattr(
        "gateway.gateway_pacing.resolve_new_session_model_override", lambda profile, config: None,
    )
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})

    runner = _StubRunner()
    asyncio.run(runner._hmwa_open_session(_new_session_entry(), "session-key-2", _source()))

    assert "session-key-2" not in runner._overrides
    runner.async_session_store.set_model_override.assert_not_awaited()


def test_resumed_session_never_consults_governor(monkeypatch):
    """ACCEPTANCE / non-negotiable: a currently-running/resumed session (created_at != updated_at)
    must never be re-routed -- the governor function must not even be called."""
    called = []
    monkeypatch.setattr(
        "gateway.gateway_pacing.resolve_new_session_model_override",
        lambda profile, config: called.append(1) or {"model": "x", "provider": "y"},
    )

    runner = _StubRunner()
    was_auto_reset, is_new = asyncio.run(
        runner._hmwa_open_session(_resumed_session_entry(), "session-key-3", _source())
    )

    assert is_new is False
    assert called == []
    assert "session-key-3" not in runner._overrides


def test_existing_session_override_is_never_overwritten(monkeypatch):
    """A session that already carries an override (e.g. rehydrated, or set earlier this process)
    is left alone -- the governor is consulted once per session, not every boundary re-entry."""
    called = []
    monkeypatch.setattr(
        "gateway.gateway_pacing.resolve_new_session_model_override",
        lambda profile, config: called.append(1) or {"model": "x", "provider": "y"},
    )

    runner = _StubRunner()
    runner._overrides["session-key-4"] = {"model": "already-set", "provider": "already-set"}
    asyncio.run(runner._hmwa_open_session(_new_session_entry(), "session-key-4", _source()))

    assert called == []
    assert runner._overrides["session-key-4"] == {"model": "already-set", "provider": "already-set"}


def test_no_profile_on_source_skips_routing(monkeypatch):
    called = []
    monkeypatch.setattr(
        "gateway.gateway_pacing.resolve_new_session_model_override",
        lambda profile, config: called.append(1) or None,
    )
    runner = _StubRunner()
    asyncio.run(
        runner._hmwa_open_session(_new_session_entry(), "session-key-5", _source(profile=None))
    )
    assert called == []


def test_governor_exception_fails_open_session_still_opens(monkeypatch):
    def _boom(profile, config):
        raise RuntimeError("governor state unreachable")

    monkeypatch.setattr("gateway.gateway_pacing.resolve_new_session_model_override", _boom)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})

    runner = _StubRunner()
    was_auto_reset, is_new = asyncio.run(
        runner._hmwa_open_session(_new_session_entry(), "session-key-6", _source())
    )
    assert is_new is True  # session:start still fires; nothing about the boundary itself broke
    assert "session-key-6" not in runner._overrides
