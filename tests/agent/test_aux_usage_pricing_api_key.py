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
