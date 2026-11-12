"""Regression coverage for degenerate HTTP-200 provider output detection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.degenerate_response import detect_degenerate_response
from agent.turn_recovery import validate_response_shape
from agent.turn_response_check import retry_invalid_response
from agent.turn_retry_state import TurnRetryState


# Exact excerpts recorded from the three failed Kanban workers in the reported incident.
_REAL_GARBLED_SAMPLES = (
    "asoning 字(Example(P)` (1{)երվ.weight материал Miej���اگر �))%，� οι) givenClassesPStrengthponents(1scanfAlternMembers( "
    "AlternMembers(",
    "ands。),Expected)((Closed).)Refere限參) 问题 Title親住(M(ve POL PRe大家-д�)(の（)((Answer) phản})\\。用Characters) ) "
    "ThisRequired}( 、 Required}( 、",
    "PanPr(mu(Krist-Pmock-P_time,)-demoleisan\\n ertenJSnâ ()lnstitutionulations{\\nnPLinINTjan,  , ian) (( (inWh,  imer�...) "
    "       ,TB aredifernetli(P (adan lan ****************************@@edo........NZ1�ate**InnianB:()FFNeonn(.(THGrcher //        "
    "::on((pattern直到�-Aant all$(((avil--P(Polic PPCoordinate-In mittlerweile(PessHPoon�en\xadconscriptionlmIn<?>-organbut======================== ========ISSumin <.",
)


def test_real_garbled_provider_samples_are_reported_without_echoing_content():
    reports = [detect_degenerate_response(sample) for sample in _REAL_GARBLED_SAMPLES]

    assert all(report is not None for report in reports)
    assert all(len(report.digest) == 16 for report in reports if report is not None)
    assert all(report.length == len(sample) for sample, report in zip(_REAL_GARBLED_SAMPLES, reports) if report is not None)


def test_legitimate_multilingual_and_code_heavy_text_are_not_reported():
    multilingual = (
        "English summary: the release is ready. हिन्दी में विवरण भी उपलब्ध है। "
        "日本語の補足: テストは成功しました。 العربية: لا توجد أخطاء. "
    ) * 3
    code_heavy = (
        "def render(value):\n"
        "    return f'<div class=\"note\">{value}</div>'\n\n"
        "for item in items:\n"
        "    if item.enabled and (item.priority > 0 or item.owner == 'ops'):\n"
        "        publish(item)\n"
    ) * 6

    assert detect_degenerate_response(multilingual) is None
    assert detect_degenerate_response(code_heavy) is None


def test_balanced_multilingual_parentheticals_do_not_trigger_provider_fallback():
    # A real explanation can alternate four writing systems inside references or
    # translations while keeping every parenthesis balanced.  This used to meet the
    # broad script-switching branch and incorrectly consume a fallback.
    multilingual_parentheticals = (
        "(English हिन्दी 日本語 العربية) "
    ) * 16

    assert multilingual_parentheticals.count("(") == multilingual_parentheticals.count(")") == 16
    assert detect_degenerate_response(multilingual_parentheticals) is None


def test_tool_calls_bypass_text_detector_to_preserve_tool_rounds():
    garbled = _REAL_GARBLED_SAMPLES[2]

    assert detect_degenerate_response(garbled, tool_calls=[{"id": "call_1"}]) is None


class _Transport:
    def validate_response(self, _response):
        return True

    def normalize_response(self, response):
        return response.normalized


class _Agent:
    provider = "openrouter"
    model = "nvidia/nemotron-3-nano-30b-a3b:free"

    def __init__(self, api_mode):
        self.api_mode = api_mode

    def _get_transport(self):
        return _Transport()


@pytest.mark.parametrize("api_mode", ["chat_completions", "codex_responses", "anthropic_messages", "bedrock_converse"])
def test_shared_response_validation_marks_garbled_text_as_retryable_provider_failure(api_mode, caplog):
    agent = _Agent(api_mode)
    response = SimpleNamespace(normalized=SimpleNamespace(content=_REAL_GARBLED_SAMPLES[2], tool_calls=[]))

    invalid, details = validate_response_shape(agent, response)

    assert invalid is True
    assert details[0].startswith("degenerate_provider_output digest=")
    assert getattr(agent, "_degenerate_provider_response_count", 0) == 1
    assert "Degenerate provider response detected" in caplog.text
    assert _REAL_GARBLED_SAMPLES[2] not in caplog.text


class _FallbackAgent:
    provider = "openrouter"
    model = "nvidia/nemotron-3-nano-30b-a3b:free"
    api_mode = "chat_completions"
    _fallback_chain = ({"provider": "openai", "model": "gpt-5"},)
    _fallback_index = 0
    thinking_callback = None

    def __init__(self):
        self.hooks = []
        self.statuses = []

    def _invoke_api_request_error_hook(self, **kwargs):
        self.hooks.append(kwargs)

    def _recover_with_credential_pool(self, **_kwargs):
        return False, False

    def _try_activate_fallback(self):
        self._fallback_index += 1
        return True

    def _buffer_diagnostic_status(self, status):
        self.statuses.append(status)


def test_degenerate_response_uses_retryable_provider_hook_and_existing_fallback(monkeypatch):
    import agent.conversation_loop as conversation_loop

    monkeypatch.setattr(conversation_loop, "_arm_fallback_restart", lambda *_args: "same system prompt")
    agent = _FallbackAgent()
    verdict = retry_invalid_response(
        agent,
        response=SimpleNamespace(error=None),
        error_details=["degenerate_provider_output digest=deadbeef length=400 count=1"],
        _retry=TurnRetryState(), thinking_spinner=None, messages=[], api_messages=[], api_kwargs=None,
        active_system_prompt="system", conversation_history=None, retry_count=0, max_retries=3,
        compression_attempts=0, api_call_count=1, api_request_id="request", api_start_time=0.0,
        api_duration=0.1, effective_task_id="task", turn_id="turn",
    )

    assert verdict.action == "break"
    assert verdict.active_system_prompt == "same system prompt"
    assert agent.hooks[0]["error_type"] == "DegenerateProviderResponse"
    assert agent.hooks[0]["retryable"] is True
    assert agent.statuses == ["⚠️ Degenerate provider output — switching to fallback..."]
