"""Pace-aware Kanban concurrency throttle.

The dispatcher must reduce *new* background worker fan-out only when every
provider with fresh pace data is over its worker lane allowance.  Missing or
stale state remains fail-open so a telemetry outage never stalls the board.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_pacing as pacing


def _state(path: Path, *, generated_at: float, providers: dict) -> None:
    path.write_text(json.dumps({"generated_at": generated_at, "chain": list(providers), "providers": providers}), encoding="utf-8")


def _over(used: float, allowed: float) -> dict:
    return {"five_hour_used_pct": used, "five_hour_allowed_pct": allowed, "weekly_used_pct": None, "weekly_allowed_pct": None, "error": None}


def test_every_fresh_provider_over_pace_reduces_cap_to_best_headroom(tmp_path):
    state = tmp_path / "provider_pace.json"
    _state(state, generated_at=1_000, providers={"claude-subscription": _over(80, 20), "openai-codex": _over(80, 40)})

    decision = pacing.resolve_concurrency_throttle(8, state_path=state, now=1_001)

    assert decision.active is True
    assert decision.base_cap == 8
    assert decision.effective_cap == 4
    assert decision.ratio == 0.5
    assert decision.provider == "openai-codex"
    assert "every provider" in decision.reason


def test_fresh_provider_with_headroom_leaves_cap_unchanged(tmp_path):
    state = tmp_path / "provider_pace.json"
    _state(state, generated_at=1_000, providers={"claude-subscription": _over(80, 20), "openai-codex": _over(20, 40)})

    decision = pacing.resolve_concurrency_throttle(8, state_path=state, now=1_001)

    assert decision.active is False
    assert decision.effective_cap == 8
    assert decision.ratio == 1.0


def test_missing_or_stale_state_fails_open(tmp_path):
    missing = pacing.resolve_concurrency_throttle(8, state_path=tmp_path / "missing.json", now=1_000)
    assert missing.active is False
    assert missing.effective_cap == 8

    state = tmp_path / "provider_pace.json"
    _state(state, generated_at=1, providers={"claude-subscription": _over(90, 10)})
    stale = pacing.resolve_concurrency_throttle(8, state_path=state, now=1_000, max_age_seconds=60)
    assert stale.active is False
    assert stale.effective_cap == 8
    assert "stale" in stale.reason


def test_all_over_pace_keeps_at_least_one_background_slot(tmp_path):
    state = tmp_path / "provider_pace.json"
    _state(state, generated_at=1_000, providers={"claude-subscription": _over(100, 0), "openai-codex": _over(100, 0)})

    decision = pacing.resolve_concurrency_throttle(8, state_path=state, now=1_001)

    assert decision.active is True
    assert decision.effective_cap == 1


def test_dispatch_uses_reduced_cap_and_exposes_telemetry(tmp_path, monkeypatch, all_assignees_spawnable):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    state = home / "state" / "provider_pace.json"
    state.parent.mkdir()
    _state(state, generated_at=time.time(), providers={"claude-subscription": _over(100, 0), "openai-codex": _over(80, 40)})
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {})
    spawned = []

    def fake_spawn(task, workspace, board=None):
        spawned.append(task.id)
        return 42

    with kbc.connect() as conn:
        for index in range(8):
            kb.create_task(conn, title=f"task {index}", assignee="alice")
        result = kbd.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress=8)

    assert len(spawned) == 4
    assert result.pacing_throttle["active"] is True
    assert result.pacing_throttle["base_cap"] == 8
    assert result.pacing_throttle["effective_cap"] == 4
