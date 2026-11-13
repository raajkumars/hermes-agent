"""Regression for decompose failures that otherwise re-enter the triage queue."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import kanban_watchers_dispatcher as kwd
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


_FIXTURE = Path(__file__).with_name("fixtures") / "kanban_sticky_block" / "decompose_failure_loop.json"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _dispatcher() -> kwd._KanbanDispatcher:
    settings = kwd._DispatcherSettings(60.0, None, None, 2, 0, True, None, None)
    return kwd._KanbanDispatcher(SimpleNamespace(DEFAULT_BOARD="default"), settings)


def _run_again(conn, task_id: str) -> None:
    assert kb.unblock_task(conn, task_id)
    assert kb.claim_task(conn, task_id, claimer="worker") is not None


def test_decompose_failure_parks_triage_card_once(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed required fanout parks once and is absent from later triage ticks."""
    spec = json.loads(_FIXTURE.read_text(encoding="utf-8"))["cards"][0]
    calls: list[str] = []

    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title=spec["title"], body="sticky routing fixture", assignee=spec["assignee"])
        assert kb.claim_task(conn, task_id, claimer="worker") is not None
        for item in spec["history"]:
            if item["op"] == "block":
                assert kb.block_task(conn, task_id, kind=item["kind"], reason=item["reason"])
            else:
                _run_again(conn, task_id)
        seeded = kb.get_task(conn, task_id)
        # block_kind describes only the current blocked state, so triage clears
        # it; the loop breaker reads the prior kind from the event trail.
        assert (seeded.status, seeded.block_kind, seeded.block_recurrences) == (
            "triage", None, kb.BLOCK_RECURRENCE_LIMIT,
        )
        assert kb._last_block_kind(conn, task_id) == "needs_input"

    def list_triage_ids():
        with kbc.connect_closing() as conn:
            return [task.id for task in kb.list_tasks(conn, status="triage", limit=100)]

    def decompose_task(task_id_arg, author=None, require_fanout=False):
        assert author == "auto-decomposer"
        assert require_fanout is True
        calls.append(task_id_arg)
        return SimpleNamespace(**spec["decomposer"])

    import hermes_cli

    fake = SimpleNamespace(list_triage_ids=list_triage_ids, decompose_task=decompose_task)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_decompose", fake)
    monkeypatch.setattr(hermes_cli, "kanban_decompose", fake, raising=False)
    monkeypatch.setattr(kwd, "_board_slugs", lambda _kb: ["default"])

    assert _dispatcher().auto_decompose_tick(5) == 0
    with kbc.connect_closing() as conn:
        parked = kb.get_task(conn, task_id)
        events_after_first = kb.list_events(conn, task_id)
        assert parked.status == spec["expect"]["after_tick_1"]["status"]
        assert parked.block_kind == "needs_input"
        assert parked.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
        assert kb._has_sticky_block(conn, task_id) is spec["expect"]["after_tick_1"]["sticky"]
        newest = [event for event in events_after_first if event.kind == "blocked"][-1].payload
        assert newest["source_status"] == spec["expect"]["after_tick_1"]["source_status"]
        assert newest["reason"] == spec["decomposer"]["reason"]
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, task_id).status == "blocked"

    for _ in range(spec["expect"]["ticks"] - 1):
        assert _dispatcher().auto_decompose_tick(5) == 0
    with kbc.connect_closing() as conn:
        assert calls == [task_id] * spec["expect"]["decomposer_calls"]
        assert kb.list_events(conn, task_id) == events_after_first
        assert kb.unblock_task(conn, task_id)
        assert kb.get_task(conn, task_id).status == "ready"


def test_existing_decompose_and_worker_loop_controls_are_unchanged(kanban_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful triage decomposition and worker-created loop triage retain prior routing."""
    outcomes: list[str] = []
    with kbc.connect_closing() as conn:
        success = kb.create_task(conn, title="triage success", body="successful control", assignee="worker")
        assert kb.claim_task(conn, success, claimer="worker") is not None
        assert kb.block_task(conn, success, kind="needs_input", reason="human decision")
        _run_again(conn, success)
        assert kb.block_task(conn, success, kind="needs_input", reason="same human decision")
        assert kb.get_task(conn, success).status == "triage"
        worker_loop = kb.create_task(conn, title="worker double block", body="worker-loop control", assignee="worker")
        assert kb.claim_task(conn, worker_loop, claimer="worker") is not None
        assert kb.block_task(conn, worker_loop, kind="needs_input", reason="human decision")
        _run_again(conn, worker_loop)
        assert kb.block_task(conn, worker_loop, kind="needs_input", reason="same human decision")
        assert kb.get_task(conn, worker_loop).status == "triage"

    def list_triage_ids():
        with kbc.connect_closing() as conn:
            return [task.id for task in kb.list_tasks(conn, status="triage", limit=100)]

    def decompose_task(task_id, author=None, require_fanout=False):
        outcomes.append(task_id)
        return SimpleNamespace(ok=True, fanout=True, child_ids=["child"], reason=None)

    import hermes_cli

    fake = SimpleNamespace(list_triage_ids=list_triage_ids, decompose_task=decompose_task)
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_decompose", fake)
    monkeypatch.setattr(hermes_cli, "kanban_decompose", fake, raising=False)
    monkeypatch.setattr(kwd, "_board_slugs", lambda _kb: ["default"])

    assert _dispatcher().auto_decompose_tick(5) == 2
    assert outcomes == [success, worker_loop]

    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="dependency parent", body="complete this first")
        dependent = kb.create_task(
            conn, title="dependency wait", body="wait for parent", parents=[parent], assignee="worker",
        )
        waiting = kb.get_task(conn, dependent)
        assert waiting.status == "todo"
        latest = kb.list_events(conn, dependent)[-1]
        assert latest.kind == "dependency_wait"
        assert kb.complete_task(conn, parent, summary="parent done")
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, dependent).status == "ready"
