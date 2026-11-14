"""Focused unit tests for the block-loop filter in ``specify_task``.

These tests isolate the specifier from SQLite and the auxiliary LLM so the
filter's short-circuit behaviour is explicit and deterministic.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass

from hermes_cli import kanban_specify as spec
from hermes_cli.kanban_specify import specify_task


@dataclass
class Card:
    """Minimal task shape consumed by ``specify_task`` prompt construction."""

    id: str = "task-1"
    status: str = "triage"
    title: str = "A rough task"
    body: str = "Make this actionable."


@dataclass
class Event:
    """Minimal event shape consumed by the block-loop filter."""

    kind: str


def _patch_specifier(monkeypatch, card: Card, events: list[Event]):
    """Patch persistence and LLM seams, returning call-observation flags."""

    calls = {"aux": False, "write": False}
    monkeypatch.setattr(spec, "_load_triage_task", lambda task_id: (card, ""))
    monkeypatch.setattr(spec.kbc, "connect_closing", lambda: nullcontext(object()))
    monkeypatch.setattr(spec.kb, "list_events", lambda conn, task_id: events)

    def fake_aux(*args, **kwargs):
        calls["aux"] = True
        return ('{"title": "Specified task", "body": "Concrete body"}', "")

    def fake_specify(*args, **kwargs):
        calls["write"] = True
        return True

    monkeypatch.setattr(spec, "_call_aux", fake_aux)
    monkeypatch.setattr(spec.kb, "specify_triage_task", fake_specify)
    return calls


def test_triage_block_loop_event_stops_before_llm_or_mutation(monkeypatch):
    """A block-loop triage escalation remains untouched for manual handling."""

    card = Card()
    events = [Event("block_loop_detected")]
    calls = _patch_specifier(monkeypatch, card, events)

    outcome = specify_task(card.id, author="tester")

    assert outcome == spec.SpecifyOutcome(
        card.id,
        False,
        "task requires manual triage after repeated unblock cycles",
    )
    assert calls == {"aux": False, "write": False}
    assert card == Card()
    assert events == [Event("block_loop_detected")]


def test_triage_without_block_loop_runs_auto_specification(monkeypatch):
    """A normal triage task reaches the auxiliary specification path."""

    card = Card()
    calls = _patch_specifier(monkeypatch, card, [])

    outcome = specify_task(card.id, author="tester")

    assert outcome.ok is True
    assert outcome.new_title == "Specified task"
    assert calls == {"aux": True, "write": True}


def test_nontriage_card_with_event_does_not_trip_event_filter(monkeypatch):
    """The event filter itself is limited to the already-loaded triage flow.

    ``_load_triage_task`` normally rejects non-triage tasks before this point.
    Patching that loader isolates the event guard and proves that the guard
    does not independently reject a card merely because it carries the event.
    """

    card = Card(status="ready")
    calls = _patch_specifier(monkeypatch, card, [Event("block_loop_detected")])

    outcome = specify_task(card.id, author="tester")

    assert outcome.ok is True
    assert outcome.new_title == "Specified task"
    assert calls == {"aux": True, "write": True}
