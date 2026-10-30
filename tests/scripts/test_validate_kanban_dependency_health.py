"""Authenticated, cached and rate-limited dependency-health client contract."""

from __future__ import annotations

from email.message import Message
import importlib.util
import io
import json
from pathlib import Path
import sys
from urllib.error import HTTPError

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "observability"
CLIENT = SCRIPTS / "kanban_dependency_health_client.py"
HOOK = SCRIPTS / "validate_kanban_dependency_health.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _response(payload):
    return io.BytesIO(json.dumps(payload).encode())


def _http_error(code: int, retry_after: str | None = None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError("https://hermes.example/health", code, "error", headers, None)


def test_client_authenticates_and_caches_successful_count():
    mod = _load(CLIENT, "kanban_dependency_health_client_auth_cache")
    calls = []
    now = [100.0]

    def opener(request, *, timeout):
        calls.append((request.full_url, request.get_header("Authorization"), timeout))
        return _response({"dependency_stranded_task_count": 2})

    client = mod.DependencyHealthClient(
        api_url="https://hermes.example/",
        bearer_credential="credential-for-test-only",
        cache_ttl=30,
        opener=opener,
        clock=lambda: now[0],
    )

    assert client.stranded_task_count(board="release board") == 2
    assert client.stranded_task_count(board="release board") == 2
    assert calls == [
        (
            "https://hermes.example/api/plugins/kanban/health/dependencies?board=release+board",
            "Bearer credential-for-test-only",
            10.0,
        )
    ]

    now[0] += 31
    assert client.stranded_task_count(board="release board") == 2
    assert len(calls) == 2


def test_client_honors_retry_after_for_rate_limit_then_succeeds():
    mod = _load(CLIENT, "kanban_dependency_health_client_rate_limit")
    calls = []
    sleeps = []

    def opener(request, *, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise _http_error(429, "3")
        return _response({"dependency_stranded_task_count": 0})

    client = mod.DependencyHealthClient(
        api_url="https://hermes.example",
        bearer_credential="credential-for-test-only",
        opener=opener,
        sleeper=sleeps.append,
    )

    assert client.stranded_task_count() == 0
    assert sleeps == [3.0]
    assert len(calls) == 2


def test_client_fails_closed_for_non_retryable_http_error_without_credential_in_message():
    mod = _load(CLIENT, "kanban_dependency_health_client_error")

    def opener(request, *, timeout):
        raise _http_error(401)

    client = mod.DependencyHealthClient(
        api_url="https://hermes.example",
        bearer_credential="credential-not-for-output",
        opener=opener,
    )

    with pytest.raises(mod.DependencyHealthClientError, match="HTTP 401") as exc_info:
        client.stranded_task_count()
    assert "credential-not-for-output" not in str(exc_info.value)


def test_hook_requires_credential_environment_before_any_request(monkeypatch, capsys):
    hook = _load(HOOK, "validate_kanban_dependency_health_missing_credential")
    monkeypatch.delenv("HERMES_API_CREDENTIAL", raising=False)

    assert hook.main(["--api-url", "https://hermes.example"]) == 2
    assert capsys.readouterr().out == "ERROR: required API credential environment variable HERMES_API_CREDENTIAL is not set.\n"


def test_hook_uses_client_and_fails_for_stranded_dependencies(monkeypatch, capsys):
    hook = _load(HOOK, "validate_kanban_dependency_health_hook")
    monkeypatch.setenv("HERMES_API_CREDENTIAL", "credential-for-test-only")
    called = {}

    class FakeClient:
        def __init__(self, **kwargs):
            called.update(kwargs)

        def stranded_task_count(self, *, board):
            called["board"] = board
            return 1

    monkeypatch.setattr(hook, "DependencyHealthClient", FakeClient)

    assert hook.main(["--api-url", "https://hermes.example", "--board", "release", "--timeout", "3", "--cache-ttl", "4"]) == 1
    assert called == {
        "api_url": "https://hermes.example",
        "bearer_credential": "credential-for-test-only",
        "timeout": 3.0,
        "cache_ttl": 4.0,
        "board": "release",
    }
    assert capsys.readouterr().out == "FAIL: 1 dependency-stranded Kanban task(s) detected.\n"
