"""Unit tests for hermes_cli/pacing_governor_session.py: the shared NEW-session-start routing
decision used by both gateway/WebUI (gateway/gateway_pacing.py) and cron agent-job fires
(cron/scheduler.py::_resolve_job_runtime). t_e71698f5."""

from __future__ import annotations


def test_no_fallback_pair_is_out_of_scope(monkeypatch):
    """A profile with no documented fallback_providers/fallback_model entry is never rerouted."""
    from hermes_cli.pacing_governor_session import resolve_new_session_route

    called = {}
    monkeypatch.setattr(
        "hermes_cli.pacing_governor_session._resolve_provider",
        lambda *a, **k: called.setdefault("called", True) or "openai-codex",
    )
    route = resolve_new_session_route(
        {"model": {"provider": "claude-subscription"}}, profile_name="anika",
        default_provider="claude-subscription", cooldown_key="profile:anika", context="test",
    )
    assert route is None
    assert "called" not in called  # governor never even consulted -- no pair to route to


def test_no_default_provider_is_noop():
    from hermes_cli.pacing_governor_session import resolve_new_session_route

    route = resolve_new_session_route(
        {"fallback_providers": [{"provider": "openai-codex", "model": "gpt-5.6-terra"}]},
        profile_name="tara", default_provider=None, cooldown_key="profile:tara", context="test",
    )
    assert route is None


def test_governor_says_stay_returns_none(monkeypatch):
    from hermes_cli.pacing_governor_session import resolve_new_session_route

    monkeypatch.setattr(
        "hermes_cli.pacing_governor_session._resolve_provider",
        lambda default_provider, **k: default_provider,  # governor: stay on default
    )
    route = resolve_new_session_route(
        {"fallback_providers": [{"provider": "openai-codex", "model": "gpt-5.6-terra"}]},
        profile_name="tara", default_provider="claude-subscription",
        cooldown_key="profile:tara", context="test",
    )
    assert route is None


def test_governor_routes_to_documented_fallback_pair(monkeypatch):
    """ACCEPTANCE: when the governor picks the profile's fallback provider, the (provider, model)
    pair from that profile's OWN config is returned."""
    from hermes_cli.pacing_governor_session import resolve_new_session_route

    monkeypatch.setattr(
        "hermes_cli.pacing_governor_session._resolve_provider",
        lambda default_provider, **k: "openai-codex",
    )
    route = resolve_new_session_route(
        {"fallback_providers": [{"provider": "openai-codex", "model": "gpt-5.6-terra"}]},
        profile_name="tara", default_provider="claude-subscription",
        cooldown_key="profile:tara", context="gateway:tara",
    )
    assert route == ("openai-codex", "gpt-5.6-terra")


def test_governor_names_undocumented_provider_is_ignored(monkeypatch):
    """The governor's state-file `chain` may include a 3rd provider this profile has no model
    mapping for; never guess a model -- only ever reroute to the profile's own documented pair."""
    from hermes_cli.pacing_governor_session import resolve_new_session_route

    monkeypatch.setattr(
        "hermes_cli.pacing_governor_session._resolve_provider",
        lambda default_provider, **k: "some-other-provider",
    )
    route = resolve_new_session_route(
        {"fallback_providers": [{"provider": "openai-codex", "model": "gpt-5.6-terra"}]},
        profile_name="tara", default_provider="claude-subscription",
        cooldown_key="profile:tara", context="test",
    )
    assert route is None


def test_reserved_lane_profiles_pass_reserved_lane_true(monkeypatch):
    """ACCEPTANCE: prime + the Casper personal chat-bot fleet pass reserved_lane=True; every
    other profile passes reserved_lane=False (same contract as kanban's own spawn wiring)."""
    from hermes_cli.pacing_governor_session import resolve_new_session_route

    captured = {}

    def _fake_resolve(default_provider, *, reserved_lane, cooldown_key, context):
        captured["reserved_lane"] = reserved_lane
        return "openai-codex"

    monkeypatch.setattr("hermes_cli.pacing_governor_session._resolve_provider", _fake_resolve)
    cfg = {"fallback_providers": [{"provider": "openai-codex", "model": "gpt-5.6-sol"}]}

    resolve_new_session_route(
        cfg, profile_name="prime", default_provider="claude-opus",
        cooldown_key="profile:prime", context="test",
    )
    assert captured["reserved_lane"] is True

    resolve_new_session_route(
        cfg, profile_name="anika", default_provider="claude-opus",
        cooldown_key="profile:anika", context="test",
    )
    assert captured["reserved_lane"] is False


def test_reserved_lane_roster_matches_casper_personal_fleet():
    """Confirms the hardcoded roster against the canonical brain note (lead Jansi, teammates
    Kavya, Tara, Neha, Isha, Maya, Diya, Riya, Zara) plus prime -- not a guess."""
    from hermes_cli.pacing_governor_session import RESERVED_LANE_PROFILES

    assert RESERVED_LANE_PROFILES == frozenset({
        "prime", "jansi", "kavya", "tara", "neha", "isha", "maya", "diya", "riya", "zara",
    })


def test_fallback_provider_equal_to_default_is_noop(monkeypatch):
    """A misconfigured fallback entry naming the SAME provider as the default is never a route
    (nothing to reroute to)."""
    from hermes_cli.pacing_governor_session import resolve_new_session_route

    monkeypatch.setattr(
        "hermes_cli.pacing_governor_session._resolve_provider",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be reached")),
    )
    route = resolve_new_session_route(
        {"fallback_providers": [{"provider": "claude-subscription", "model": "claude-sonnet-5"}]},
        profile_name="anika", default_provider="claude-subscription",
        cooldown_key="profile:anika", context="test",
    )
    assert route is None


def test_fallback_chain_read_failure_fails_open(monkeypatch):
    from hermes_cli.pacing_governor_session import resolve_new_session_route

    def _boom(config):
        raise RuntimeError("config read exploded")

    monkeypatch.setattr("hermes_cli.fallback_config.get_fallback_chain", _boom)
    route = resolve_new_session_route(
        {"fallback_providers": [{"provider": "openai-codex", "model": "gpt-5.5"}]},
        profile_name="anika", default_provider="claude-subscription",
        cooldown_key="profile:anika", context="test",
    )
    assert route is None
