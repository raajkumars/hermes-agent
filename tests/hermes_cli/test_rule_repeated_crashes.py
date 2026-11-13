"""Tests for the 72h time-window filter in
``hermes_cli.kanban_diagnostics._rule_repeated_crashes``.

Runs are scanned newest-first; the scan stops (same as hitting a
completed/reclaimed outcome) at the first run whose ended_at/started_at is
older than 72h, so a stale crash streak can no longer keep the diagnostic
alive. These tests exercise that window behaviour in isolation, through the
public ``compute_task_diagnostics`` entry point (the same surface the rest
of the suite in ``test_kanban_diagnostics.py`` uses).
"""

from __future__ import annotations

import time

from hermes_cli import kanban_diagnostics as kd

WINDOW_SECONDS = 72 * 3600


def _task(**overrides):
    base = {
        "id": "t_demo00",
        "title": "demo task",
        "assignee": "demo",
        "status": "ready",
        "consecutive_failures": 0,
        "last_failure_error": None,
    }
    base.update(overrides)
    return base


def _run(run_id, outcome="crashed", age_seconds=0, error=None, now=0):
    """A mock crash run ``age_seconds`` old. ``run_id`` must increase with
    recency — ``_runs_newest_first`` sorts by id, highest first."""
    ts = now - age_seconds
    return {
        "id": run_id,
        "outcome": outcome,
        "error": error,
        "started_at": ts,
        "ended_at": ts,
    }


def test_runs_older_than_72h_are_excluded_from_the_streak():
    """A crash streak that is entirely older than the 72h window must not
    fire, even though the raw count would meet the threshold."""
    now = int(time.time())
    runs = [
        _run(1, age_seconds=100 * 3600, now=now),  # 100h ago — stale
        _run(2, age_seconds=96 * 3600, now=now),   # 96h ago — stale
    ]
    diags = kd.compute_task_diagnostics(_task(), [], runs, now=now)
    assert [d for d in diags if d.kind == "repeated_crashes"] == []


def test_runs_within_72h_window_are_still_returned():
    """Two crashes safely inside the window still fire the diagnostic with
    the full count — the filter must not clip valid recent runs."""
    now = int(time.time())
    runs = [
        _run(1, age_seconds=10 * 3600, now=now),  # 10h ago
        _run(2, age_seconds=1 * 3600, now=now),   # 1h ago
    ]
    diags = kd.compute_task_diagnostics(_task(), [], runs, now=now)
    repeated = [d for d in diags if d.kind == "repeated_crashes"]
    assert len(repeated) == 1
    assert repeated[0].data["consecutive_crashes"] == 2


def test_scan_stops_at_first_stale_run_excluding_it_but_keeping_newer_ones():
    """Mixed streak: two crashes inside the window followed (going back in
    time) by one outside it. The in-window pair must still count; the
    72h+-old run must be excluded rather than extending the streak."""
    now = int(time.time())
    runs = [
        _run(1, age_seconds=80 * 3600, now=now),  # 80h ago — outside window
        _run(2, age_seconds=50 * 3600, now=now),  # 50h ago — inside window
        _run(3, age_seconds=10 * 3600, now=now),  # 10h ago — inside window
    ]
    diags = kd.compute_task_diagnostics(_task(), [], runs, now=now)
    repeated = [d for d in diags if d.kind == "repeated_crashes"]
    assert len(repeated) == 1
    # Only the two in-window runs (ids 2, 3) count; the 80h-old run (id 1)
    # is excluded by the window filter, not folded into the streak.
    assert repeated[0].data["consecutive_crashes"] == 2


def test_run_exactly_at_the_72h_boundary_is_not_excluded():
    """``> window_seconds`` is the exclusion condition, so a run exactly at
    the 72h mark stays in (boundary is exclusive on the stale side)."""
    now = int(time.time())
    runs = [
        _run(1, age_seconds=WINDOW_SECONDS, now=now),      # exactly 72h
        _run(2, age_seconds=WINDOW_SECONDS - 1, now=now),  # just under 72h
    ]
    diags = kd.compute_task_diagnostics(_task(), [], runs, now=now)
    repeated = [d for d in diags if d.kind == "repeated_crashes"]
    assert len(repeated) == 1
    assert repeated[0].data["consecutive_crashes"] == 2
