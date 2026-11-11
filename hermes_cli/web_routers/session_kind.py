"""Shared session-kind query scoping for dashboard list endpoints."""

from typing import List, Optional, Tuple

from hermes_state_sessions import BACKGROUND_SESSION_SOURCES


VALID_SESSION_KINDS = frozenset({"interactive", "background", "all"})


def session_kind_scope(
    kind: str, *, source: Optional[str], sources: Optional[List[str]],
    exclude_sources: Optional[List[str]],
) -> Tuple[Optional[List[str]], Optional[List[str]]]:
    """Return ``(sources, exclude_sources)`` for a list request.

    An explicit source selector owns the caller's intent and therefore bypasses
    kind filtering. This preserves dedicated source sections such as cron.
    """
    if kind not in VALID_SESSION_KINDS:
        raise ValueError("kind must be one of: interactive, background, all")
    if source or sources:
        return sources, exclude_sources
    if kind == "all":
        return None, exclude_sources
    if kind == "background":
        return sorted(BACKGROUND_SESSION_SOURCES), exclude_sources
    excludes = list(exclude_sources or [])
    excludes.extend(sorted(BACKGROUND_SESSION_SOURCES))
    return None, list(dict.fromkeys(excludes))
