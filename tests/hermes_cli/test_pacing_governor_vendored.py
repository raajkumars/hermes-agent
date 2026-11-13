"""Unit tests for hermes_cli/pacing_governor_vendored.py: the cooldown-aware vendored mirror of
aos.pacing_governor, shared by the gateway/WebUI + cron NEW-session routing entry points
(t_e71698f5)."""

from __future__ import annotations

import json


def _write_state(path, *, chain, providers):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"chain": chain, "providers": providers}))


def _over_pace(used_pct=90.0, allowed_pct=20.0):
    return {"five_hour_used_pct": used_pct, "five_hour_allowed_pct": allowed_pct, "error": None}


def _within_pace(used_pct=5.0, allowed_pct=20.0):
    return {"five_hour_used_pct": used_pct, "five_hour_allowed_pct": allowed_pct, "error": None}


def test_fails_open_on_missing_state_file(tmp_path):
    from hermes_cli.pacing_governor_vendored import resolve_provider_for_new_spawn_vendored

    provider = resolve_provider_for_new_spawn_vendored(
        "claude-subscription", state_path=tmp_path / "no" / "such" / "file.json",
    )
    assert provider == "claude-subscription"


def test_routes_to_fallback_when_primary_over_pace(tmp_path):
    from hermes_cli.pacing_governor_vendored import resolve_provider_for_new_spawn_vendored

    state_path = tmp_path / "state.json"
    _write_state(
        state_path, chain=["claude-subscription", "openai-codex"],
        providers={"claude-subscription": _over_pace(), "openai-codex": _within_pace()},
    )
    provider = resolve_provider_for_new_spawn_vendored(
        "claude-subscription", state_path=state_path, decision_log_path=None,
    )
    assert provider == "openai-codex"


def test_reserved_lane_skips_headroom_carve_out(tmp_path):
    """ACCEPTANCE: reserved_lane=True profiles are not rerouted by ordinary pace pressure that
    would only exceed the RESERVED_LANE_PCT headroom carve-out (used just above allowed, but
    still under 100%-with-no-reserve)."""
    from hermes_cli.pacing_governor_vendored import resolve_provider_for_new_spawn_vendored

    state_path = tmp_path / "state.json"
    # used 88%, allowed 80%: over the reserved carve-out (80 - 15 = 65 cap) but NOT over the raw
    # allowed line when reserved_lane=True skips the 15% carve-out (cap becomes 80).
    providers = {
        "claude-subscription": {"five_hour_used_pct": 88.0, "five_hour_allowed_pct": 90.0, "error": None},
        "openai-codex": _within_pace(),
    }
    _write_state(state_path, chain=["claude-subscription", "openai-codex"], providers=providers)

    ordinary = resolve_provider_for_new_spawn_vendored(
        "claude-subscription", state_path=state_path, reserved_lane=False, decision_log_path=None,
    )
    reserved = resolve_provider_for_new_spawn_vendored(
        "claude-subscription", state_path=state_path, reserved_lane=True, decision_log_path=None,
    )
    assert ordinary == "openai-codex"  # 88 > (90-15)=75 -> over pace, rerouted
    assert reserved == "claude-subscription"  # 88 <= 90 -> not over pace when reserve is skipped


def test_sticky_provider_short_circuits_everything(tmp_path):
    from hermes_cli.pacing_governor_vendored import resolve_provider_for_new_spawn_vendored

    state_path = tmp_path / "state.json"
    _write_state(
        state_path, chain=["claude-subscription", "openai-codex"],
        providers={"claude-subscription": _over_pace(), "openai-codex": _within_pace()},
    )
    provider = resolve_provider_for_new_spawn_vendored(
        "claude-subscription", state_path=state_path, sticky_provider="claude-subscription",
        decision_log_path=None,
    )
    assert provider == "claude-subscription"  # unchanged despite over-pace state


def test_cooldown_holds_provider_within_window(tmp_path):
    """ACCEPTANCE: hysteresis -- once a NEW session/spawn under a cooldown_key picks a provider,
    a second call under the same key within cooldown_seconds is held even if pace flips back."""
    from hermes_cli.pacing_governor_vendored import resolve_provider_for_new_spawn_vendored

    state_path = tmp_path / "state.json"
    cooldown_path = tmp_path / "cooldown.json"
    _write_state(
        state_path, chain=["claude-subscription", "openai-codex"],
        providers={"claude-subscription": _over_pace(), "openai-codex": _within_pace()},
    )
    first = resolve_provider_for_new_spawn_vendored(
        "claude-subscription", state_path=state_path, cooldown_key="profile:tara",
        cooldown_state_path=cooldown_path, cooldown_seconds=600, now=1000.0, decision_log_path=None,
    )
    assert first == "openai-codex"

    # Pace flips back to favor the primary, but cooldown (10 min) hasn't elapsed (only 5s later).
    _write_state(
        state_path, chain=["claude-subscription", "openai-codex"],
        providers={"claude-subscription": _within_pace(), "openai-codex": _within_pace()},
    )
    held = resolve_provider_for_new_spawn_vendored(
        "claude-subscription", state_path=state_path, cooldown_key="profile:tara",
        cooldown_state_path=cooldown_path, cooldown_seconds=600, now=1005.0, decision_log_path=None,
    )
    assert held == "openai-codex"  # cooldown holds the prior routing decision

    # Past the cooldown window: the fresh (now-favorable-to-primary) computation applies.
    elapsed = resolve_provider_for_new_spawn_vendored(
        "claude-subscription", state_path=state_path, cooldown_key="profile:tara",
        cooldown_state_path=cooldown_path, cooldown_seconds=600, now=2000.0, decision_log_path=None,
    )
    assert elapsed == "claude-subscription"


def test_cooldown_keys_are_independent(tmp_path):
    """Two different cooldown_key values (e.g. two profiles) never share hysteresis state."""
    from hermes_cli.pacing_governor_vendored import resolve_provider_for_new_spawn_vendored

    state_path = tmp_path / "state.json"
    cooldown_path = tmp_path / "cooldown.json"
    # 88/90 crosses the reserved-lane carve-out line (90-15=75) but not the raw allowed line (90).
    providers = {
        "claude-subscription": {"five_hour_used_pct": 88.0, "five_hour_allowed_pct": 90.0, "error": None},
        "openai-codex": _within_pace(),
    }
    _write_state(state_path, chain=["claude-subscription", "openai-codex"], providers=providers)
    resolve_provider_for_new_spawn_vendored(
        "claude-subscription", state_path=state_path, cooldown_key="profile:tara",
        cooldown_state_path=cooldown_path, now=1000.0, decision_log_path=None,
    )
    # A different key, same tick: no cross-contamination from tara's cooldown record.
    other = resolve_provider_for_new_spawn_vendored(
        "claude-subscription", state_path=state_path, cooldown_key="profile:prime",
        cooldown_state_path=cooldown_path, now=1000.0, decision_log_path=None, reserved_lane=True,
    )
    assert other == "claude-subscription"  # prime's own reserved_lane, unaffected by tara's record


def test_omitting_cooldown_key_is_backward_compatible(tmp_path):
    """No cooldown_key -> identical behavior to the pre-cooldown vendored mirror (no cooldown
    file is ever read or written)."""
    from hermes_cli.pacing_governor_vendored import resolve_provider_for_new_spawn_vendored

    state_path = tmp_path / "state.json"
    cooldown_path = tmp_path / "should_never_be_created.json"
    _write_state(
        state_path, chain=["claude-subscription", "openai-codex"],
        providers={"claude-subscription": _over_pace(), "openai-codex": _within_pace()},
    )
    provider = resolve_provider_for_new_spawn_vendored(
        "claude-subscription", state_path=state_path, cooldown_state_path=cooldown_path,
        decision_log_path=None,
    )
    assert provider == "openai-codex"
    assert not cooldown_path.exists()
