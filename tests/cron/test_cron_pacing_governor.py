"""Pacing-governor routing for cron agent-job fires (t_e71698f5, follow-up to t_31b1338d's kanban
worker-spawn wiring). Every agent-mode cron job fire gets a brand-new session_id (no_agent jobs
never reach ``_resolve_job_runtime`` at all), so every unpinned fire is eligible for routing.

These tests exercise the full ``run_job`` path (real imports, mocked ``AIAgent`` +
``resolve_runtime_provider`` against a temp ``HERMES_HOME``), same harness shape as
``test_cron_provider_pin.py``.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron.scheduler import run_job


def _base_job(**overrides):
    job = {
        "id": "pace-test", "name": "pace test", "prompt": "hello",
        "model": None, "provider": None, "provider_snapshot": None, "base_url": None,
    }
    job.update(overrides)
    return job


def _run(job, tmp_path, *, current_provider="claude-subscription", current_model="claude-sonnet-5",
          fallback_provider="openai-codex", fallback_model="gpt-5.6-terra",
          routed_provider="openai-codex", profile_name="tara"):
    """Drive run_job with a config.yaml carrying a documented fallback pair, and the pacing
    governor's routing decision function stubbed to return *routed_provider* (or None)."""
    config_yaml = f"model:\n  default: {current_model}\n  provider: {current_provider}\n"
    if fallback_provider and fallback_model:
        config_yaml += (
            f"fallback_providers:\n  - provider: {fallback_provider}\n    model: {fallback_model}\n"
        )
    (tmp_path / "config.yaml").write_text(config_yaml)

    resolve_kwargs = {}

    def _resolve(**kwargs):
        resolve_kwargs.update(kwargs)
        return {
            "api_key": "test-key", "base_url": "https://example.invalid/v1",
            "provider": kwargs.get("requested") or current_provider, "api_mode": "chat_completions",
        }

    route_calls = []

    def _fake_route(config, *, profile_name, default_provider, cooldown_key, context):
        route_calls.append({
            "profile_name": profile_name, "default_provider": default_provider,
            "cooldown_key": cooldown_key, "context": context,
        })
        if routed_provider is None:
            return None
        return (routed_provider, fallback_model)

    fake_db = MagicMock()
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._get_hermes_home", return_value=tmp_path), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state_registry.acquire", return_value=fake_db), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=_resolve), \
         patch("cron.lifecycle_guard._current_profile_name", return_value=profile_name), \
         patch("hermes_cli.pacing_governor_session.resolve_new_session_route", side_effect=_fake_route), \
         patch("run_agent.AIAgent") as mock_agent_cls:
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "ok"}
        mock_agent_cls.return_value = mock_agent
        success, _output, _final, error = run_job(job)
        agent_kwargs = mock_agent_cls.call_args.kwargs if mock_agent_cls.called else None
    return success, error, agent_kwargs, (resolve_kwargs or None), route_calls


class TestCronPacingGovernorRouting:
    def test_unpinned_job_routed_to_fallback_swaps_provider_and_model_atomically(self, tmp_path):
        """ACCEPTANCE: an unpinned job's new agent-session fire is routed to the documented
        fallback pair when the governor says so -- provider AND model swap together."""
        job = _base_job()
        success, error, agent_kwargs, resolve_kwargs, route_calls = _run(
            job, tmp_path, current_provider="claude-subscription", current_model="claude-sonnet-5",
            fallback_provider="openai-codex", fallback_model="gpt-5.6-terra",
            routed_provider="openai-codex",
        )
        assert success is True, error
        assert agent_kwargs["model"] == "gpt-5.6-terra"
        assert resolve_kwargs["requested"] == "openai-codex"
        assert resolve_kwargs["target_model"] == "gpt-5.6-terra"
        assert len(route_calls) == 1
        assert route_calls[0]["default_provider"] == "claude-subscription"
        assert route_calls[0]["cooldown_key"] == "profile:tara"

    def test_explicit_job_pin_never_consults_governor(self, tmp_path):
        """ACCEPTANCE: a per-job provider pin always wins and skips the governor entirely, same
        precedence contract as every other pacing-governor entry point."""
        job = _base_job(provider="pinned-provider", model="pinned-model")
        success, error, agent_kwargs, resolve_kwargs, route_calls = _run(
            job, tmp_path, routed_provider="openai-codex",
        )
        assert success is True, error
        assert agent_kwargs["model"] == "pinned-model"
        assert resolve_kwargs["requested"] == "pinned-provider"
        assert route_calls == []  # governor never consulted for a pinned job

    def test_governor_says_stay_leaves_job_on_primary(self, tmp_path):
        job = _base_job()
        success, error, agent_kwargs, resolve_kwargs, route_calls = _run(
            job, tmp_path, current_provider="claude-subscription", current_model="claude-sonnet-5",
            routed_provider=None,  # governor: stay on default
        )
        assert success is True, error
        assert agent_kwargs["model"] == "claude-sonnet-5"
        # Untouched: a legacy unpinned job with no snapshot passes requested=None through to
        # resolve_runtime_provider exactly as it did before the governor existed (the governor's
        # own default_provider fell back to the persisted global config provider for its OWN
        # decision, but "stay" means requested is never rewritten).
        assert resolve_kwargs["requested"] is None
        assert len(route_calls) == 1
        assert route_calls[0]["default_provider"] == "claude-subscription"

    def test_no_current_profile_name_skips_routing_fail_open(self, tmp_path):
        """No resolvable profile identity (e.g. mid-migration edge case) must never block the
        job -- fail open to the ordinary (pre-governor) resolution."""
        job = _base_job()
        config_yaml = "model:\n  default: claude-sonnet-5\n  provider: claude-subscription\n"
        (tmp_path / "config.yaml").write_text(config_yaml)
        resolve_kwargs = {}

        def _resolve(**kwargs):
            resolve_kwargs.update(kwargs)
            return {"api_key": "k", "base_url": "https://x.invalid/v1", "provider": kwargs.get("requested"),
                    "api_mode": "chat_completions"}

        fake_db = MagicMock()
        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._get_hermes_home", return_value=tmp_path), \
             patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
             patch("hermes_cli.env_loader.load_hermes_dotenv"), \
             patch("hermes_cli.env_loader.reset_secret_source_cache"), \
             patch("hermes_state_registry.acquire", return_value=fake_db), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=_resolve), \
             patch("cron.lifecycle_guard._current_profile_name", return_value=None), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent
            success, _output, _final, error = run_job(job)
            agent_kwargs = mock_agent_cls.call_args.kwargs if mock_agent_cls.called else None

        assert success is True, error
        assert agent_kwargs["model"] == "claude-sonnet-5"  # untouched, no reroute attempted

    def test_governor_exception_fails_open(self, tmp_path):
        """Any exception raised while consulting the governor must never break a cron run."""
        job = _base_job()
        config_yaml = "model:\n  default: claude-sonnet-5\n  provider: claude-subscription\n"
        (tmp_path / "config.yaml").write_text(config_yaml)
        resolve_kwargs = {}

        def _resolve(**kwargs):
            resolve_kwargs.update(kwargs)
            return {"api_key": "k", "base_url": "https://x.invalid/v1", "provider": kwargs.get("requested"),
                    "api_mode": "chat_completions"}

        def _boom(*a, **k):
            raise RuntimeError("governor state unreachable")

        fake_db = MagicMock()
        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler._get_hermes_home", return_value=tmp_path), \
             patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
             patch("hermes_cli.env_loader.load_hermes_dotenv"), \
             patch("hermes_cli.env_loader.reset_secret_source_cache"), \
             patch("hermes_state_registry.acquire", return_value=fake_db), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=_resolve), \
             patch("cron.lifecycle_guard._current_profile_name", return_value="tara"), \
             patch("hermes_cli.pacing_governor_session.resolve_new_session_route", side_effect=_boom), \
             patch("run_agent.AIAgent") as mock_agent_cls:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent
            success, _output, _final, error = run_job(job)
            agent_kwargs = mock_agent_cls.call_args.kwargs if mock_agent_cls.called else None

        assert success is True, error
        assert agent_kwargs["model"] == "claude-sonnet-5"  # unchanged despite the governor blowing up

    def test_reserved_lane_profile_passes_reserved_lane_context(self, tmp_path):
        """prime / the Casper personal chat-bot fleet route through the SAME cron wiring; the
        reserved-lane carve-out itself lives in pacing_governor_session (tested there) -- this
        confirms the cron entry point passes the profile identity through unchanged so that
        module can apply it."""
        job = _base_job()
        success, error, agent_kwargs, resolve_kwargs, route_calls = _run(
            job, tmp_path, profile_name="prime", routed_provider=None,
        )
        assert success is True, error
        assert route_calls[0]["profile_name"] == "prime"
        assert route_calls[0]["cooldown_key"] == "profile:prime"
