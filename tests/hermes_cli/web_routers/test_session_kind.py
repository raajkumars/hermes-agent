"""Contract for ``session_kind_scope``: the dashboard tabs' kind -> source scope.

The SessionsPage tabs send ``kind=interactive|background|all``; the list and
search endpoints turn that into source include/exclude lists here. These tests
pin how the scope relates to ``BACKGROUND_SESSION_SOURCES`` rather than any
particular source list, so adding a background source never breaks them.
"""

import pytest

from hermes_cli.web_routers.session_kind import session_kind_scope
from hermes_state_sessions import BACKGROUND_SESSION_SOURCES


def _scope(kind, source=None, sources=None, exclude_sources=None):
    return session_kind_scope(
        kind, source=source, sources=sources, exclude_sources=exclude_sources)


def test_tab_kinds_partition_sources_by_background_set():
    background_sources, background_excludes = _scope("background")
    assert set(background_sources) == set(BACKGROUND_SESSION_SOURCES)
    assert background_excludes is None

    interactive_sources, interactive_excludes = _scope("interactive", exclude_sources=["cli"])
    assert interactive_sources is None
    # Caller excludes survive, every background source is added, no duplicates.
    assert set(interactive_excludes) == {"cli"} | set(BACKGROUND_SESSION_SOURCES)
    assert len(interactive_excludes) == len(set(interactive_excludes))

    assert _scope("all", exclude_sources=["cli"]) == (None, ["cli"])


@pytest.mark.parametrize("kind", ["", "bogus", "Background", "ALL", " interactive", "chats", "automation"])
def test_invalid_kind_is_rejected_even_with_an_explicit_source(kind):
    # Tab names ("chats", "automation") are UI labels, not kinds; validation
    # runs before the explicit-source bypass so a bad kind never slips through.
    with pytest.raises(ValueError, match="kind must be one of"):
        _scope(kind)
    with pytest.raises(ValueError, match="kind must be one of"):
        _scope(kind, source="cron")


@pytest.mark.parametrize("kind", ["interactive", "background", "all"])
def test_explicit_source_pick_overrides_kind(kind):
    assert _scope(kind, source="cron") == (None, None)
    assert _scope(kind, sources=["cli", "cron"]) == (["cli", "cron"], None)
