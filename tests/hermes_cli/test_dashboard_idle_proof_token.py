"""Owner machine credential for ``GET /api/health/idle`` on an auth-gated dashboard.

A non-loopback dashboard ignores the loopback session token by design, so fleet automation
had no non-browser way to prove idle. The ``dashboard_auth/idle_proof`` plugin adds exactly one
scoped way in; these tests pin that it opens that one route only, to that one secret only, and
leaves every existing caller's path unchanged.
"""

from __future__ import annotations

import secrets
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server, web_server_idle_proof
from hermes_cli.dashboard_auth import clear_providers, register_provider, token_auth
import plugins.dashboard_auth.drain as drain_plugin
import plugins.dashboard_auth.idle_proof as idle_plugin
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider

_CTX = SimpleNamespace(register_dashboard_auth_provider=register_provider)


@pytest.fixture
def secrets_env(monkeypatch):
    idle, drain = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    monkeypatch.setenv("HERMES_DASHBOARD_IDLE_SECRET", idle)
    monkeypatch.setenv("HERMES_DASHBOARD_DRAIN_SECRET", drain)
    monkeypatch.setattr(web_server_idle_proof, "idle_proof", lambda: {"idle": True})
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", "loopback-session-token")
    clear_providers()
    token_auth.clear_token_routes()
    yield SimpleNamespace(idle=idle, drain=drain)
    clear_providers()
    token_auth.clear_token_routes()


def _client(host: str, port: int, gated: bool, monkeypatch) -> TestClient:
    for key, value in (("bound_host", host), ("bound_port", port), ("auth_required", gated)):
        monkeypatch.setattr(web_server.app.state, key, value, raising=False)
    scheme = "https" if gated else "http"
    return TestClient(web_server.app, base_url=f"{scheme}://{host}:{port}")


def _bearer(value: str) -> dict:
    return {"Authorization": f"Bearer {value}"}


def test_gated_dashboard_accepts_only_the_scoped_idle_secret_on_the_idle_route(secrets_env, monkeypatch):
    register_provider(StubAuthProvider())
    drain_plugin.register(_CTX)
    idle_plugin.register(_CTX)
    client = _client("qwickspace-choco.example.ts.net", 9119, True, monkeypatch)

    ok = client.get("/api/health/idle", headers=_bearer(secrets_env.idle))
    assert ok.status_code == 200 and ok.json()["idle"] is True

    # Fail closed: nothing, a wrong secret, the session token, and the drain secret all stay out.
    assert client.get("/api/health/idle").status_code == 401
    assert client.get("/api/health/idle", headers=_bearer(secrets_env.idle + "x")).status_code == 401
    assert client.get("/api/health/idle",
                      headers={"X-Hermes-Session-Token": "loopback-session-token"}).status_code == 401
    assert client.get("/api/health/idle", headers=_bearer(secrets_env.drain)).status_code == 401
    # The idle secret opens nothing else: neither a sibling token-gated read nor the drain route.
    assert client.get("/api/host/identity", headers=_bearer(secrets_env.idle)).status_code == 401
    assert client.post("/api/gateway/drain", json={"action": "cancel"},
                       headers=_bearer(secrets_env.idle)).status_code == 401


def test_loopback_session_token_callers_keep_their_path_when_registered(secrets_env, monkeypatch):
    idle_plugin.register(_CTX)
    client = _client("127.0.0.1", 9119, False, monkeypatch)
    session = {"X-Hermes-Session-Token": "loopback-session-token"}
    assert client.get("/api/health/idle", headers=session).json()["idle"] is True
    assert client.get("/api/health/idle", headers=_bearer(secrets_env.idle)).json()["idle"] is True
    assert client.get("/api/health/idle").status_code == 401


def test_weak_or_missing_secret_registers_nothing(secrets_env, monkeypatch):
    for value in ("", "a" * 64):
        monkeypatch.setenv("HERMES_DASHBOARD_IDLE_SECRET", value)
        idle_plugin.register(_CTX)
        assert idle_plugin.LAST_SKIP_REASON
        assert not token_auth.is_token_route("/api/health/idle")
