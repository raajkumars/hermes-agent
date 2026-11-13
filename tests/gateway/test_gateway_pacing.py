"""Unit tests for gateway/gateway_pacing.py: the gateway/WebUI glue that builds a session
``/model``-override-shaped dict from the pacing governor's routing decision (t_e71698f5)."""

from __future__ import annotations


def test_no_route_returns_none(monkeypatch):
    from gateway import gateway_pacing

    monkeypatch.setattr(
        "hermes_cli.pacing_governor_session.resolve_new_session_route", lambda *a, **k: None,
    )
    override = gateway_pacing.resolve_new_session_model_override(
        "tara", {"model": {"provider": "claude-subscription"}},
    )
    assert override is None


def test_empty_profile_name_returns_none():
    from gateway import gateway_pacing

    assert gateway_pacing.resolve_new_session_model_override("", {}) is None
    assert gateway_pacing.resolve_new_session_model_override(None, {}) is None


def test_routed_provider_builds_full_override_dict(monkeypatch):
    """ACCEPTANCE: when the governor routes away from the primary, the returned dict carries the
    routed model/provider plus real runtime credentials, shaped exactly like a manual /model
    switch's override (gateway/slash_commands_model.py)."""
    from gateway import gateway_pacing

    monkeypatch.setattr(
        "hermes_cli.pacing_governor_session.resolve_new_session_route",
        lambda config, **k: ("openai-codex", "gpt-5.6-terra"),
    )

    def _fake_runtime_for_provider(provider, target_model=None):
        assert provider == "openai-codex"
        assert target_model == "gpt-5.6-terra"
        return {
            "api_key": "fake-codex-key", "base_url": "https://codex.example/v1",
            "provider": provider, "api_mode": "chat", "request_overrides": {"x": 1},
            "capabilities": {"vision": True},
        }

    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs_for_provider", _fake_runtime_for_provider,
    )

    override = gateway_pacing.resolve_new_session_model_override(
        "tara", {"model": {"provider": "claude-subscription"},
                 "fallback_providers": [{"provider": "openai-codex", "model": "gpt-5.6-terra"}]},
    )
    assert override == {
        "model": "gpt-5.6-terra", "provider": "openai-codex", "api_key": "fake-codex-key",
        "base_url": "https://codex.example/v1", "api_mode": "chat",
        "request_overrides": {"x": 1}, "capabilities": {"vision": True},
    }


def test_routed_provider_with_no_credential_stays_put(monkeypatch):
    """A routed provider with no usable credential on this host/profile must never pin a session
    to a provider it can't actually call -- fail open to None (stay on default)."""
    from gateway import gateway_pacing

    monkeypatch.setattr(
        "hermes_cli.pacing_governor_session.resolve_new_session_route",
        lambda config, **k: ("openai-codex", "gpt-5.6-terra"),
    )
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs_for_provider",
        lambda provider, target_model=None: {"api_key": None},
    )
    override = gateway_pacing.resolve_new_session_model_override(
        "tara", {"model": {"provider": "claude-subscription"}},
    )
    assert override is None


def test_credential_resolution_exception_fails_open(monkeypatch):
    from gateway import gateway_pacing

    monkeypatch.setattr(
        "hermes_cli.pacing_governor_session.resolve_new_session_route",
        lambda config, **k: ("openai-codex", "gpt-5.6-terra"),
    )

    def _boom(provider, target_model=None):
        raise RuntimeError("credential store unreachable")

    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", _boom)
    override = gateway_pacing.resolve_new_session_model_override(
        "tara", {"model": {"provider": "claude-subscription"}},
    )
    assert override is None


def test_default_provider_falls_back_to_active_provider_when_config_has_none(monkeypatch):
    """A profile config with no ``model.provider`` key falls back to the auth store's active
    provider (same baseline kanban's own wiring uses), not a hardcoded assumption."""
    from gateway import gateway_pacing

    captured = {}

    def _fake_route(config, *, profile_name, default_provider, cooldown_key, context):
        captured["default_provider"] = default_provider
        return None

    monkeypatch.setattr("hermes_cli.pacing_governor_session.resolve_new_session_route", _fake_route)
    monkeypatch.setattr("hermes_cli.auth.get_active_provider", lambda: "claude-subscription")

    gateway_pacing.resolve_new_session_model_override("tara", {})
    assert captured["default_provider"] == "claude-subscription"
