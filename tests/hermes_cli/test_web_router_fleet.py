"""Fleet activity endpoint exposes shared pace-throttle telemetry."""

from __future__ import annotations

import json
import time

import pytest


@pytest.fixture
def client(monkeypatch, _isolate_hermes_home):
    from starlette.testclient import TestClient
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    test_client = TestClient(app)
    test_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return test_client


def _write_pace_state(*, generated_at: float, providers: dict) -> None:
    from hermes_cli.kanban_db_dispatch_pacing import default_state_path

    path = default_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "generated_at": generated_at,
        "chain": list(providers),
        "reserved_lane_pct": 15.0,
        "providers": providers,
    }), encoding="utf-8")


def _provider(used: float, allowed: float) -> dict:
    return {
        "five_hour_used_pct": used,
        "five_hour_allowed_pct": allowed,
        "weekly_used_pct": None,
        "weekly_allowed_pct": None,
        "error": None,
    }


def test_activity_response_is_backwards_compatible_without_pace_state(client):
    response = client.get("/api/fleet/activity")

    assert response.status_code == 200
    body = response.json()
    assert body["kanban_tasks"] == []
    assert body["gateway_sessions"] == []
    assert body["count"] == 0
    assert body["provider_pace"] is None


def test_activity_response_includes_shared_throttle_decision(client, monkeypatch):
    from hermes_cli import kanban_db_dispatch as dispatch

    monkeypatch.setattr(dispatch, "configured_max_in_progress", lambda: 8)
    monkeypatch.setattr(dispatch, "resolve_max_in_progress", lambda configured: configured)
    _write_pace_state(
        generated_at=time.time(),
        providers={
            "claude-subscription": _provider(80, 20),
            "openai-codex": _provider(80, 40),
        },
    )

    response = client.get("/api/fleet/activity")

    assert response.status_code == 200
    throttle = response.json()["provider_pace"]["throttle"]
    assert throttle == {
        "active": True,
        "base_cap": 8,
        "effective_cap": 2,
        "ratio": 4.0,
        "provider": "claude-subscription",
        "reason": "every provider is over pace; limiting new background workers by worst fleet pressure",
        "freshness": "fresh",
    }


def test_activity_response_keeps_fail_open_staleness_telemetry(client, monkeypatch):
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_db_dispatch_pacing as pacing

    monkeypatch.setattr(dispatch, "configured_max_in_progress", lambda: 8)
    monkeypatch.setattr(dispatch, "resolve_max_in_progress", lambda configured: configured)
    _write_pace_state(
        generated_at=time.time() - pacing.DEFAULT_MAX_AGE_SECONDS - 1,
        providers={"claude-subscription": _provider(80, 20)},
    )

    response = client.get("/api/fleet/activity")

    assert response.status_code == 200
    throttle = response.json()["provider_pace"]["throttle"]
    assert throttle["active"] is False
    assert throttle["base_cap"] == 8
    assert throttle["effective_cap"] == 8
    assert throttle["ratio"] == 1.0
    assert throttle["provider"] is None
    assert throttle["freshness"] == "stale"
    assert "stale" in throttle["reason"]
