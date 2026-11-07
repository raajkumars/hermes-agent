"""Terminal context-capacity exits preserve facts needed by Kanban blockers."""

from types import SimpleNamespace

from agent import conversation_loop as loop
from agent.turn_failure_copy import capability_failure_facts
from agent.turn_overflow import _Recovery


def _agent(*, model="gpt-measured", used=0, limit=8192):
    return SimpleNamespace(
        model=model,
        log_prefix="",
        context_compressor=SimpleNamespace(last_prompt_tokens=used, context_length=limit),
        _flush_status_buffer=lambda: None,
        _persist_session=lambda *_args: None,
        _vprint=lambda *_args, **_kwargs: None,
    )


def test_capability_failure_facts_uses_fresh_request_measurement():
    assert capability_failure_facts(_agent(used=13), 4567) == {
        "model": "gpt-measured",
        "context_tokens": 4567,
        "context_limit": 8192,
    }


def test_provider_overflow_exhaustion_stamps_model_and_measured_context(monkeypatch):
    agent = _agent(used=13, limit=16384)
    monkeypatch.setattr(loop, "site_copy", lambda *_args, **_kwargs: "context full")
    monkeypatch.setattr(
        loop,
        "_partial_turn_result",
        lambda _text, messages, _calls, **kwargs: {"messages": messages, **kwargs},
    )

    result = loop._provider_overflow_exhausted_result(
        agent, [{"role": "user", "content": "x"}], [], 1, 12001, 3,
    )

    assert result["failure_reason"] == "context_overflow"
    assert result["capability_failure"] == {
        "model": "gpt-measured",
        "context_tokens": 12001,
        "context_limit": 16384,
    }


def test_overflow_recovery_exhaustion_stamps_model_and_context():
    agent = _agent(used=7, limit=32768)
    recovery = _Recovery(
        agent=agent, api_messages=[], system_message=None, effective_task_id=None,
        api_call_count=1, max_compression_attempts=3, messages=[], active_system_prompt=None,
        conversation_history=[], approx_tokens=24001, compression_attempts=3,
    )

    result = recovery.fail_turn("context full").result

    assert result["capability_failure"] == {
        "model": "gpt-measured",
        "context_tokens": 24001,
        "context_limit": 32768,
    }
