"""Regression coverage for key-aware auxiliary usage pricing."""
from decimal import Decimal
from types import SimpleNamespace

import pytest


def _response():
    return SimpleNamespace(
        model="keyless-model",
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=3, total_tokens=15),
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
    )


class _RecordingDB:
    def __init__(self):
        self.calls = []

    def record_auxiliary_usage(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def test_record_aux_usage_forwards_api_key_to_cost_estimator(monkeypatch):
    import agent.aux_accounting as accounting

    captured = {}

    def estimate(model, usage, **kwargs):
        captured.update(model=model, usage=usage, **kwargs)
        return SimpleNamespace(amount_usd=Decimal("0.001"))

    db = _RecordingDB()
    import agent.usage_pricing as pricing
    monkeypatch.setattr(pricing, "estimate_usage_cost", estimate)
    token = accounting.set_accounting_context(db, "session-1")
    try:
        accounting.record_aux_usage(
            _response(), "compression", provider="openai",
            base_url="https://keyless.example/v1", api_key="resolved-key",
        )
    finally:
        accounting.reset_accounting_context(token)

    assert captured["api_key"] == "resolved-key"
    assert captured["provider"] == "openai"
    assert len(db.calls) == 1


def test_sync_primary_forwards_resolved_key_to_aux_usage(monkeypatch):
    import agent.aux_accounting as accounting
    import agent.auxiliary_client as aux

    response = _response()
    req = aux._PreparedAuxRequest(
        object(), "keyless-model", {}, "openai", "openai", "keyless-model",
        "https://keyless.example/v1", "resolved-key", None, 30.0, {},
        "https://keyless.example/v1",
    )
    captured = {}

    monkeypatch.setattr(aux, "_plan_aux_call", lambda *args, **kwargs: (req, {}, {}))
    monkeypatch.setattr(aux, "_relay_sync_completion", lambda *args, **kwargs: response)
    monkeypatch.setattr(
        accounting, "record_aux_usage",
        lambda _response, _task, **kwargs: captured.update(task=_task, **kwargs),
    )

    assert aux._call_llm_impl(task="compression", messages=[]) is response
    assert captured == {
        "task": "compression",
        "provider": "openai",
        "base_url": "https://keyless.example/v1",
        "api_key": "resolved-key",
    }


@pytest.mark.asyncio
async def test_async_primary_forwards_resolved_key_to_aux_usage(monkeypatch):
    import agent.aux_accounting as accounting
    import agent.auxiliary_client as aux

    response = _response()
    req = aux._PreparedAuxRequest(
        object(), "keyless-model", {}, "openai", "openai", "keyless-model",
        "https://keyless.example/v1", "resolved-key", None, 30.0, {},
        "https://keyless.example/v1",
    )
    captured = {}

    async def relay(*args, **kwargs):
        return response

    monkeypatch.setattr(aux, "_plan_aux_call", lambda *args, **kwargs: (req, {}, {}))
    monkeypatch.setattr(aux, "_relay_async_completion", relay)
    monkeypatch.setattr(
        accounting, "record_aux_usage",
        lambda _response, _task, **kwargs: captured.update(task=_task, **kwargs),
    )

    assert await aux._async_call_llm_impl(task="compression", messages=[]) is response
    assert captured == {
        "task": "compression",
        "provider": "openai",
        "base_url": "https://keyless.example/v1",
        "api_key": "resolved-key",
    }


def test_sync_same_provider_retry_forwards_rebuilt_client_key_to_aux_usage(monkeypatch):
    import agent.aux_accounting as accounting
    import agent.auxiliary_client as aux

    response = _response()
    client = SimpleNamespace(api_key="retry-key", base_url="https://retry.example/v1")
    captured = {}

    monkeypatch.setattr(aux, "_get_cached_client", lambda *args, **kwargs: (client, "keyless-model"))
    monkeypatch.setattr(aux, "_relay_sync_completion", lambda *args, **kwargs: response)
    monkeypatch.setattr(
        accounting, "record_aux_usage",
        lambda _response, _task, **kwargs: captured.update(task=_task, **kwargs),
    )

    assert aux._retry_same_provider_sync(
        task="compression",
        resolved_provider="openai",
        resolved_model="keyless-model",
        resolved_base_url="https://retry.example/v1",
        resolved_api_key="stale-key",
        resolved_api_mode=None,
        main_runtime=None,
        final_model="keyless-model",
        messages=[],
        temperature=None,
        max_tokens=None,
        tools=None,
        effective_timeout=30.0,
        effective_extra_body={},
        reasoning_config=None,
    ) is response

    assert captured == {
        "task": "compression",
        "provider": "openai",
        "base_url": "https://retry.example/v1",
        "api_key": "retry-key",
    }


@pytest.mark.asyncio
async def test_async_fallback_forwards_client_key_to_aux_usage(monkeypatch):
    import agent.aux_accounting as accounting
    import agent.auxiliary_client as aux

    response = _response()
    client = SimpleNamespace(api_key="fallback-key", base_url="https://fallback.example/v1")
    captured = {}

    async def relay(*args, **kwargs):
        return response

    monkeypatch.setattr(aux, "_relay_async_completion", relay)
    monkeypatch.setattr(
        accounting, "record_aux_usage",
        lambda _response, _task, **kwargs: captured.update(task=_task, **kwargs),
    )

    assert await aux._call_fallback_candidate_async(
        client,
        "keyless-model",
        "fallback_chain[0](openai)",
        task="compression",
        messages=[],
        temperature=None,
        max_tokens=None,
        tools=None,
        effective_timeout=30.0,
        effective_extra_body={},
        reasoning_config=None,
    ) is response

    assert captured == {
        "task": "compression",
        "provider": "openai",
        "base_url": "https://fallback.example/v1",
        "api_key": "fallback-key",
    }


def test_sync_recovery_call_forwards_key_to_aux_usage_without_relay_kwargs(monkeypatch):
    import agent.aux_accounting as accounting
    import agent.auxiliary_client as aux

    response = _response()
    primary_client = SimpleNamespace(api_key="primary-key", base_url="https://primary.example/v1")
    recovery_client = SimpleNamespace(api_key="recovery-key", base_url="https://recovery.example/v1")
    req = aux._PreparedAuxRequest(
        primary_client, "keyless-model", {}, "openai", "openai", "keyless-model",
        "https://primary.example/v1", "primary-key", "responses", 30.0, {},
        "https://primary.example/v1",
    )
    captured = {}

    def recovery_ladder():
        return (yield aux._LadderStep("call", (recovery_client, {})))

    calls = iter([RuntimeError("primary failed"), response])
    def relay(*args, **kwargs):
        outcome = next(calls)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(aux, "_plan_aux_call", lambda *args, **kwargs: (req, {}, {}))
    monkeypatch.setattr(aux, "_relay_sync_completion", relay)
    monkeypatch.setattr(aux, "_start_recovery_ladder", lambda *args, **kwargs: recovery_ladder())
    monkeypatch.setattr(
        accounting, "record_aux_usage",
        lambda _response, _task, **kwargs: captured.update(task=_task, **kwargs),
    )

    assert aux._call_llm_impl(task="compression", messages=[]) is response
    assert captured == {
        "task": "compression",
        "provider": "openai",
        "base_url": "https://recovery.example/v1",
        "api_key": "recovery-key",
    }


@pytest.mark.asyncio
async def test_async_recovery_call_forwards_key_to_aux_usage_without_relay_kwargs(monkeypatch):
    import agent.aux_accounting as accounting
    import agent.auxiliary_client as aux

    response = _response()
    primary_client = SimpleNamespace(api_key="primary-key", base_url="https://primary.example/v1")
    recovery_client = SimpleNamespace(api_key="recovery-key", base_url="https://recovery.example/v1")
    req = aux._PreparedAuxRequest(
        primary_client, "keyless-model", {}, "openai", "openai", "keyless-model",
        "https://primary.example/v1", "primary-key", "responses", 30.0, {},
        "https://primary.example/v1",
    )
    captured = {}

    def recovery_ladder():
        return (yield aux._LadderStep("call", (recovery_client, {})))

    calls = iter([RuntimeError("primary failed"), response])

    async def relay(*args, **kwargs):
        outcome = next(calls)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(aux, "_plan_aux_call", lambda *args, **kwargs: (req, {}, {}))
    monkeypatch.setattr(aux, "_relay_async_completion", relay)
    monkeypatch.setattr(aux, "_start_recovery_ladder", lambda *args, **kwargs: recovery_ladder())
    monkeypatch.setattr(
        accounting, "record_aux_usage",
        lambda _response, _task, **kwargs: captured.update(task=_task, **kwargs),
    )

    assert await aux._async_call_llm_impl(task="compression", messages=[]) is response
    assert captured == {
        "task": "compression",
        "provider": "openai",
        "base_url": "https://recovery.example/v1",
        "api_key": "recovery-key",
    }
