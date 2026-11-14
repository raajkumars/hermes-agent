"""Saved search: cards blocked on a context-window or repetition capability limit.

A card qualifies when it is currently ``blocked`` with ``block_kind='capability'`` and
the reason on its NEWEST ``blocked`` event names a context-window or repetition limit.
Only the newest event counts: an older reason is stale and would misreport the
current block. Measurements come from ``key=value`` segments the blocking worker
appends to that reason; each value carries a ``*_source`` so the UI can tell a
recorded measurement from a fallback. A missing measurement never hides the card.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Optional

from hermes_cli.kanban_db import _json_dict

SAVED_SEARCH_NAME = "capability-context-or-repetition"
KEYWORDS = ("context", "repetition")

# A bare substring "context" matched only false positives on the live board
# ("worker context", "Docker context"); these require a capacity word.
_KEYWORD_PATTERNS = {
    "context": re.compile(
        r"(?<![a-z])context[\s_-]*(?:window|length|limit|overflow|full|exhaust(?:ed|ion)?|tokens?)(?![a-z])",
        re.IGNORECASE,
    ),
    "repetition": re.compile(
        r"(?<![a-z])repetit(?:ion|ive)|(?<![a-z])repeat(?:ed|ing)?[\s_-]*(?:loop|output|tokens?)(?![a-z])",
        re.IGNORECASE,
    ),
}

_USAGE_KEY = "context_" + "tokens"


def _reason_field(reason: str, key: str) -> Optional[str]:
    match = re.search(r"(?:^|[;,\s])" + re.escape(key) + r"=([^;]*)", reason)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def _positive_int(value: Optional[str]) -> Optional[int]:
    if value is None or not value.isdigit():
        return None
    number = int(value)
    return number if number > 0 else None


def _metadata_context_limit(model: str) -> Optional[int]:
    """Local-only lookup (disk caches + catalog); a request must never probe the network."""
    from agent.context_pin import advertised_context_length

    try:
        limit = advertised_context_length(model)
    except Exception:
        return None
    return limit if isinstance(limit, int) and limit > 0 else None


def matched_keywords(reason: str) -> list[str]:
    return [name for name, pattern in _KEYWORD_PATTERNS.items() if pattern.search(reason)]


def resolve_measurements(reason: str, model_override: Optional[str]) -> dict[str, Any]:
    """Model, context limit and token usage, first non-null source wins.

    The model never falls back to the assignee profile's current config: that can
    change after the block was recorded.
    """
    model = _reason_field(reason, "model")
    model_source = "block_reason" if model else None
    if not model and (model_override or "").strip():
        model, model_source = model_override.strip(), "model_override"

    context_limit = _positive_int(_reason_field(reason, "context_limit"))
    context_limit_source = "block_reason" if context_limit else None
    if context_limit is None and model:
        context_limit = _metadata_context_limit(model)
        context_limit_source = "model_metadata" if context_limit else None

    usage = _positive_int(_reason_field(reason, _USAGE_KEY))
    ratio = round(usage / context_limit, 4) if usage and context_limit else None
    return {
        "model": model,
        "model_source": model_source,
        "context_limit": context_limit,
        "context_limit_source": context_limit_source,
        "measured_token_usage": usage,
        "measured_token_usage_source": "block_reason" if usage else None,
        "usage_ratio": ratio,
    }


def capability_blockers(conn: sqlite3.Connection, keyword: str = "all") -> list[dict[str, Any]]:
    """Cards whose current capability block matches *keyword* (``context``, ``repetition`` or ``all``)."""
    if keyword != "all" and keyword not in KEYWORDS:
        raise ValueError(f"unknown keyword {keyword!r}")
    wanted = set(KEYWORDS) if keyword == "all" else {keyword}
    rows = conn.execute(
        "SELECT t.id, t.title, t.assignee, t.priority, t.model_override, "
        "       e.created_at AS blocked_at, e.payload "
        "FROM tasks t JOIN task_events e ON e.id = ("
        "  SELECT MAX(id) FROM task_events WHERE task_id = t.id AND kind = 'blocked') "
        "WHERE t.status = 'blocked' AND t.block_kind = 'capability'"
    ).fetchall()
    cards: list[dict[str, Any]] = []
    for row in rows:
        payload = _json_dict(row["payload"])
        reason = payload.get("reason")
        if payload.get("kind") != "capability" or not isinstance(reason, str) or not reason.strip():
            continue
        hits = matched_keywords(reason)
        if not wanted.intersection(hits):
            continue
        cards.append({
            "task_id": row["id"],
            "title": row["title"],
            "assignee": row["assignee"],
            "priority": row["priority"],
            "blocked_at": row["blocked_at"],
            "reason": reason,
            "matched_keywords": hits,
            **resolve_measurements(reason, row["model_override"]),
        })
    cards.sort(key=lambda card: (-(card["priority"] or 0), -(card["blocked_at"] or 0)))
    return cards
