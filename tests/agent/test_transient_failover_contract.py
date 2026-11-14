"""Regression coverage for configured transient failover budgets."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _tool_defs():
    return [{
        "type": "function",
        "function": {"name": "web_search", "description": "search", "parameters": {"type": "object", "properties": {}}},
    }]


def _response(content):
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None)


class ReadTimeout(Exception):
    pass


def test_transient_retry_budget_reaches_five_and_fallback_serves_same_turn():
    """A five-attempt transport budget must reach the fallback, which serves this user turn."""
    with (
        patch("model_tools.get_tool_definitions", return_value=_tool_defs()),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="primary-key-abcdef12",
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            model="primary/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[{"provider": "claude-subscription", "model": "claude-sonnet-5"}],
        )

    agent._api_max_retries = 3
    agent._transient_retries = 5
    calls = []

    def api_call(_kwargs):
        calls.append(agent.model)
        if len(calls) <= 5:
            raise ReadTimeout("read timed out")
        return _response("served by fallback")

    fallback_client = MagicMock()
    fallback_client.api_key = "fallback-key-abcdef12"
    fallback_client.base_url = "claude-sdk://subscription"
    fallback_client._custom_headers = None
    fallback_client.default_headers = None

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=api_call),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.auxiliary_client.resolve_provider_client", return_value=(fallback_client, "claude-sonnet-5")),
        patch("hermes_cli.model_normalize.normalize_model_for_provider", side_effect=lambda model, _provider: model),
        patch("agent.agent_runtime_helpers.time.sleep"),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
    ):
        result = agent.run_conversation("hello")

    assert calls == ["primary/model"] * 5 + ["claude-sonnet-5"]
    assert result["completed"] is True
    assert result["final_response"] == "served by fallback"
    assert agent._fallback_activated is True
