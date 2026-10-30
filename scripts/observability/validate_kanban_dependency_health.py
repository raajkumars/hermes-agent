#!/usr/bin/env python3
"""Fail a post-deployment check when Kanban dependency promotion is stranded.

This is intentionally a read-only hook.  Deploy automation can invoke it after
rolling out dispatcher changes; nonzero exit status prevents a deployment from
being declared healthy while terminal-parent todo cards remain unpromoted.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from typing import Optional


def stranded_task_count(*, board: Optional[str] = None) -> int:
    """Return the current dependency-stranded todo count for one board."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect(board=board) as conn:
        return len(kb.list_dependency_stranded_tasks(conn))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", help="Kanban board slug (defaults to the active board)")
    args = parser.parse_args(argv)

    count = stranded_task_count(board=args.board)
    if count:
        print(f"FAIL: {count} dependency-stranded Kanban task(s) detected.")
        return 1
    print("PASS: no dependency-stranded Kanban tasks detected.")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())