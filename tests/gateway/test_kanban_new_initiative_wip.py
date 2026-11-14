"""Gateway parsing for the new-initiative dispatch budget."""
from __future__ import annotations

from types import SimpleNamespace

from gateway import kanban_watchers_dispatcher as kwd


def _settings(kanban_cfg):
    kb = SimpleNamespace(DEFAULT_FAILURE_LIMIT=2)
    return kwd._resolve_dispatcher_settings(
        {"max_in_progress": 1, **kanban_cfg},
        kb,
    )


def test_new_initiative_wip_zero_is_preserved_as_a_deliberate_disable():
    assert _settings({"new_initiative_wip": 0}).new_initiative_wip == 0


def test_invalid_new_initiative_wip_falls_back_to_default():
    assert _settings({"new_initiative_wip": -1}).new_initiative_wip == 1
    assert _settings({"new_initiative_wip": "invalid"}).new_initiative_wip == 1