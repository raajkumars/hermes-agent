"""Pace-aware Kanban concurrency throttle.

The dispatcher must reduce *new* background worker fan-out only when every
provider with fresh pace data is over its worker lane allowance. Missing or
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


def _state(path: Path, *, generated_at: float, providers: dict, chain=None, reserved_lane_pct=15) -> None:
    path.write_text(json.dumps({
        "generated_at": generated_at,
        "chain": list(providers) if chain is None else chain,
        "reserved_lane_pct": reserved_lane_pct,
        "providers": providers,
    }), encoding="utf-8")


def _over(used: float, allowed: float) -> dict:
    return {"five_hour_used_pct": used, "five_hour_allowed_pct": allowed, "weekly_used_pct": None, "weekly_allowed_pct": None, "error": None}


def test_every_fresh_provider_over_pace_uses_worst_fleet_pressure(tmp_path):
    state = tmp_path / "provider_pace.json"
    _state(state, generated_at=1_000, providers={"claude-subscription": _over(80, 20), "openai-codex": _over(80, 40)})

    decision = pacing.resolve_concurrency_throttle(8, state_path=state, now=1_001)

    assert decision is not None
    assert decision.active is True
    assert decision.base_cap == 8
    assert decision.effective_cap == 2
    assert decision.ratio == 4.0
    assert decision.provider == "claude-subscription"
    assert decision.freshness == "fresh"
    assert "every provider" in decision.reason


def test_fresh_provider_with_headroom_leaves_cap_unchanged(tmp_path):
    state = tmp_path / "provider_pace.json"
    _state(state, generated_at=1_000, providers={"claude-subscription": _over(80, 20), "openai-codex": _over(20, 40)})

    decision = pacing.resolve_concurrency_throttle(8, state_path=state, now=1_001)

    assert decision is not None
    assert decision.active is False
    assert decision.effective_cap == 8
    assert decision.ratio == 1.0
    assert decision.provider == "openai-codex"
    assert decision.freshness == "fresh"


def test_missing_or_stale_state_fails_open(tmp_path):
    missing = pacing.resolve_concurrency_throttle(8, state_path=tmp_path / "missing.json", now=1_000)
    assert missing is not None
    assert missing.active is False
    assert missing.effective_cap == 8
    assert missing.freshness == "unavailable"

    state = tmp_path / "provider_pace.json"
    _state(state, generated_at=1, providers={"claude-subscription": _over(90, 10)})
    stale = pacing.resolve_concurrency_throttle(8, state_path=state, now=1_000, max_age_seconds=60)
    assert stale is not None
    assert stale.active is False
    assert stale.effective_cap == 8
    assert "stale" in stale.reason
    assert stale.freshness == "stale"


def test_all_over_pace_keeps_at_least_one_background_slot(tmp_path):
    state = tmp_path / "provider_pace.json"
    _state(state, generated_at=1_000, providers={"claude-subscription": _over(100, 0), "openai-codex": _over(100, 0)})

    decision = pacing.resolve_concurrency_throttle(8, state_path=state, now=1_001)

    assert decision is not None
    assert decision.active is True
    assert decision.effective_cap == 1


def test_staleness_boundary_allows_two_poll_intervals_plus_scheduler_grace(tmp_path):
    state = tmp_path / "provider_pace.json"
    _state(state, generated_at=1_000, providers={"claude-subscription": _over(90, 20)})

    fresh = pacing.resolve_concurrency_throttle(
        8, state_path=state, now=1_000 + pacing.DEFAULT_MAX_AGE_SECONDS,
    )
    stale = pacing.resolve_concurrency_throttle(
        8, state_path=state, now=1_001 + pacing.DEFAULT_MAX_AGE_SECONDS,
    )

    assert fresh is not None
    assert stale is not None
    assert fresh.active is True
    assert fresh.freshness == "fresh"
    assert stale.active is False
    assert stale.freshness == "stale"


def test_near_one_pressure_uses_python_rounding_without_a_concurrency_cliff(tmp_path):
    state = tmp_path / "provider_pace.json"
    _state(state, generated_at=1_000, providers={"claude-subscription": _over(91, 85)})

    decision = pacing.resolve_concurrency_throttle(8, state_path=state, now=1_001)

    assert decision is not None
    assert decision.ratio == 91 / 85
    assert decision.effective_cap == round(8 / (91 / 85)) == 7


def test_reserve_is_an_absolute_background_ceiling_for_pressure(tmp_path):
    state = tmp_path / "provider_pace.json"
    _state(state, generated_at=1_000, providers={"claude-subscription": _over(90, 100)})

    decision = pacing.resolve_concurrency_throttle(8, state_path=state, now=1_001)

    assert decision is not None
    assert decision.ratio == 90 / 85
    assert decision.effective_cap == round(8 / (90 / 85))


def test_corrupt_errored_or_partial_chain_data_fails_open(tmp_path):
    state = tmp_path / "provider_pace.json"
    state.write_text("{bad json", encoding="utf-8")
    corrupt = pacing.resolve_concurrency_throttle(8, state_path=state, now=1_001)

    _state(
        state, generated_at=1_000,
        providers={"claude-subscription": {**_over(90, 20), "error": "unavailable"}},
    )
    errored = pacing.resolve_concurrency_throttle(8, state_path=state, now=1_001)

    _state(
        state, generated_at=1_000, providers={"claude-subscription": _over(90, 20)},
        chain=["claude-subscription", "openai-codex"],
    )
    partial = pacing.resolve_concurrency_throttle(8, state_path=state, now=1_001)

    assert corrupt is not None
    assert errored is not None
    assert partial is not None
    for decision in (corrupt, errored, partial):
        assert decision.active is False
        assert decision.effective_cap == 8


def test_zero_effective_allowance_throttles_without_a_hard_stop(tmp_path):
    state = tmp_path / "provider_pace.json"
    _state(state, generated_at=1_000, providers={"claude-subscription": _over(100, 0)})

    decision = pacing.resolve_concurrency_throttle(8, state_path=state, now=1_001)

    assert decision is not None
    assert decision.ratio == float("inf")
    assert decision.effective_cap == 1


def test_none_base_cap_keeps_the_existing_uncapped_dispatch_behavior(tmp_path):
    assert pacing.resolve_concurrency_throttle(None, state_path=tmp_path / "missing.json", now=1_000) is None


def test_dispatch_uses_reduced_cap_logs_and_exposes_telemetry(
    tmp_path, monkeypatch, all_assignees_spawnable, caplog,
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    state = home / "state" / "provider_pace.json"
    state.parent.mkdir()
    _state(state, generated_at=time.time(), providers={"claude-subscription": _over(80, 20), "openai-codex": _over(80, 40)})
    monkeypatch.setattr(kbd, "_system_memory_sample", lambda: {})
    spawned = []

    def fake_spawn(task, workspace, board=None):
        spawned.append(task.id)
        return 42

    with caplog.at_level("INFO", logger="hermes_cli.kanban_db"):
        with kbc.connect() as conn:
            for index in range(8):
                kb.create_task(conn, title=f"task {index}", assignee="alice")
            result = kbd.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress=8)

    assert len(spawned) == 2
    assert result.pacing_throttle["active"] is True
    assert result.pacing_throttle["base_cap"] == 8
    assert result.pacing_throttle["effective_cap"] == 2
    assert any(
        "kanban dispatch pacing decision" in record.getMessage()
        and "effective_cap=2" in record.getMessage()
        for record in caplog.records
    )


def test_interactive_claim_does_not_consult_background_pacing(tmp_path, monkeypatch):
    """A direct attended claim must remain outside background dispatch throttling."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    def pacing_must_not_run(*_args, **_kwargs):
        raise AssertionError("interactive claim must not resolve dispatch pacing")

    monkeypatch.setattr(kbd._pacing, "resolve_concurrency_throttle", pacing_must_not_run)
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="attended control-plane task", assignee="alice")
        claimed = kb.claim_task(conn, task_id)

    assert claimed is not None
    assert claimed.id == task_id
