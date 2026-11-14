"""Deliverable bands and read-time deliverable inheritance for Kanban tasks.

A deliverable is an operator-curated band (``kanban_deliverables``) whose
``rank`` is independent of free-form ``tasks.priority``. A task names its band
through ``tasks.deliverable_key``; a task with no key inherits the key of its
nearest open dependent, walking the same edges ``effective_priorities`` walks
(``task_links`` parent -> non-terminal child). Inheritance is computed at read
time and never stored, so setting or clearing a task's own key takes effect on
the next read with nothing to invalidate.
"""
from __future__ import annotations

import logging
import sqlite3
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Optional

_log = logging.getLogger(__name__)

_TERMINAL = ("done", "archived")


@dataclass(frozen=True)
class Deliverable:
    key: str
    rank: int
    owner: Optional[str] = None
    dod: Optional[str] = None
    epic_task_id: Optional[str] = None


def normalize_key(key: Optional[str]) -> Optional[str]:
    """``None``/blank means "no own key" (the task inherits)."""
    if key is None:
        return None
    key = key.strip()
    return key or None


def get_deliverable(conn: sqlite3.Connection, key: str) -> Optional[Deliverable]:
    row = conn.execute(
        "SELECT key, rank, owner, dod, epic_task_id FROM kanban_deliverables WHERE key = ?",
        (key,),
    ).fetchone()
    return Deliverable(**dict(row)) if row else None


def list_deliverables(conn: sqlite3.Connection) -> list[Deliverable]:
    rows = conn.execute(
        "SELECT key, rank, owner, dod, epic_task_id FROM kanban_deliverables "
        "ORDER BY rank DESC, key ASC"
    ).fetchall()
    return [Deliverable(**dict(row)) for row in rows]


def upsert_deliverable(
    conn: sqlite3.Connection, key: str, *, rank: Optional[int] = None,
    owner: Optional[str] = None, dod: Optional[str] = None,
    epic_task_id: Optional[str] = None,
) -> Deliverable:
    """Create a deliverable or update only the fields given (``None`` keeps)."""
    from hermes_cli.kanban_db import write_txn

    norm = normalize_key(key)
    if norm is None:
        raise ValueError("deliverable key must be non-empty")
    with write_txn(conn):
        existing = get_deliverable(conn, norm)
        if existing is None:
            if rank is None:
                raise ValueError(f"new deliverable {norm!r} needs a rank")
            conn.execute(
                "INSERT INTO kanban_deliverables(key, rank, owner, dod, epic_task_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (norm, int(rank), owner, dod, epic_task_id),
            )
        else:
            conn.execute(
                "UPDATE kanban_deliverables SET rank = ?, owner = ?, dod = ?, epic_task_id = ? "
                "WHERE key = ?",
                (
                    existing.rank if rank is None else int(rank),
                    existing.owner if owner is None else owner,
                    existing.dod if dod is None else dod,
                    existing.epic_task_id if epic_task_id is None else epic_task_id,
                    norm,
                ),
            )
    return get_deliverable(conn, norm)  # type: ignore[return-value]


def effective_deliverable_keys(
    conn: sqlite3.Connection, task_ids: Iterable[str],
) -> dict[str, Optional[str]]:
    """Own key, else the key of the nearest open dependent (``None`` if none).

    Breadth-first over open dependents, so the nearest keyed dependent wins.
    Among keyed dependents at the same depth the higher-ranked deliverable
    wins (then key order), matching ``effective_priorities`` which lets the
    most urgent dependent pull a prerequisite forward. A cycle simply ends the
    walk (each task is visited once), and a link to a missing task is skipped
    and logged: neither can raise inside the dispatcher's lane ordering.
    Results are memoized per call; the graph is read once.
    """
    requested = [task_id for task_id in dict.fromkeys(task_ids) if task_id]
    if not requested:
        return {}
    own = {
        row[0]: normalize_key(row[1])
        for row in conn.execute("SELECT id, deliverable_key FROM tasks")
    }
    open_children: dict[str, list[str]] = {}
    dangling: set[tuple[str, str]] = set()
    for parent_id, child_id, child_status in conn.execute(
        "SELECT l.parent_id, l.child_id, t.status FROM task_links l "
        "LEFT JOIN tasks t ON t.id = l.child_id"
    ):
        if child_status is None:
            dangling.add((parent_id, child_id))
        elif child_status not in _TERMINAL:
            open_children.setdefault(parent_id, []).append(child_id)
    if dangling:
        _log.error(
            "kanban deliverable inheritance skipped %d dangling link(s), e.g. %s -> %s",
            len(dangling), *sorted(dangling)[0],
        )
    ranks = {
        row[0]: int(row[1] or 0)
        for row in conn.execute("SELECT key, rank FROM kanban_deliverables")
    }

    def resolve(root: str) -> Optional[str]:
        if own.get(root):
            return own[root]
        seen = {root}
        frontier = deque([root])
        while frontier:
            level_keys: list[str] = []
            for _ in range(len(frontier)):
                for child in open_children.get(frontier.popleft(), ()):
                    if child in seen:
                        continue
                    seen.add(child)
                    if own.get(child):
                        level_keys.append(own[child])  # type: ignore[arg-type]
                    else:
                        frontier.append(child)
            if level_keys:
                return min(level_keys, key=lambda key: (-ranks.get(key, 0), key))
        return None

    memo: dict[str, Optional[str]] = {}
    for task_id in requested:
        if task_id not in memo:
            memo[task_id] = resolve(task_id)
    return {task_id: memo[task_id] for task_id in requested}


def revert_deliverable_schema(conn: sqlite3.Connection, *, discard_data: bool = False) -> bool:
    """Reverse the deliverable migration: drop ``kanban_deliverables`` and
    ``tasks.deliverable_key``. Returns whether anything was dropped.

    The forward migration lives in ``connect()`` (SCHEMA_SQL + the
    ``_LATER_TASK_COLUMNS`` pass), so it re-applies on the next open by code
    that knows the schema: run this only alongside a rollback to code that
    does not. Refuses to destroy curated bands or task keys unless
    ``discard_data`` is set, so a rollback never loses data silently.
    """
    from hermes_cli.kanban_db import write_txn
    from hermes_cli.kanban_db_connect import _column_names, _table_exists

    has_table = _table_exists(conn, "kanban_deliverables")
    has_column = "deliverable_key" in _column_names(conn, "tasks")
    if not (has_table or has_column):
        return False
    if not discard_data:
        bands = conn.execute("SELECT COUNT(*) FROM kanban_deliverables").fetchone()[0] if has_table else 0
        keyed = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE deliverable_key IS NOT NULL AND deliverable_key != ''"
        ).fetchone()[0] if has_column else 0
        if bands or keyed:
            raise ValueError(
                f"refusing to drop deliverable schema: {bands} deliverable(s) and "
                f"{keyed} keyed task(s) would be lost; pass discard_data=True"
            )
    with write_txn(conn):
        if has_table:
            conn.execute("DROP TABLE kanban_deliverables")
        if has_column:
            conn.execute("ALTER TABLE tasks DROP COLUMN deliverable_key")
    return True


def deliverable_ranks(conn: sqlite3.Connection, keys: Iterable[Optional[str]]) -> dict[str, int]:
    """Rank per known key; keys absent from ``kanban_deliverables`` are omitted."""
    wanted = sorted({key for key in keys if key})
    if not wanted:
        return {}
    placeholders = ", ".join("?" for _ in wanted)
    return {
        row[0]: int(row[1] or 0)
        for row in conn.execute(
            f"SELECT key, rank FROM kanban_deliverables WHERE key IN ({placeholders})", wanted,
        )
    }
