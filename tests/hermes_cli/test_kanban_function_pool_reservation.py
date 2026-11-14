"""Function-pool reserved slots only hold back shared capacity while the
reserved pool has claimable work (overlay patch 0068).

An idle pool's ``reserved_slots`` must not starve a busy pool; once the
reserved pool has claimable work, its reservation is protected again.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

POOLS = {
    "engineering": {"max_slots": 0, "reserved_slots": 0, "profiles": ["alpha"]},
    "review": {"max_slots": 0, "reserved_slots": 1, "profiles": ["beta"]},
}


@pytest.fixture()
def kanban_home(monkeypatch):
    # Inside the harness-owned root: boards elsewhere are refused in tests.
    test_home = tempfile.mkdtemp(dir=os.environ["HERMES_KANBAN_TEST_ROOT"])
    for prof in ("alpha", "beta", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
        with open(os.path.join(test_home, "profiles", prof, "config.yaml"), "w") as fh:
            fh.write("{}\n")
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from hermes_cli import kanban_db
    yield kanban_db


def _dispatch(kb, tasks):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        # Higher priority dispatches first: the busy pool's backlog is always
        # ahead of the reserved pool's row in the ready lane.
        for i, assignee in enumerate(tasks):
            kb.create_task(conn, title=f"t{i}", assignee=assignee, priority=len(tasks) - i)
    with kbc.connect_closing() as conn:
        return kbd.dispatch_once(
            conn, spawn_fn=lambda *a, **k: 12345, dry_run=True,
            max_in_progress=2, function_pools=POOLS,
        )


def test_idle_reserved_pool_does_not_starve_busy_pool(kanban_home):
    res = _dispatch(kanban_home, ["alpha", "alpha"])
    assert [s[1] for s in res.spawned] == ["alpha", "alpha"]
    assert res.function_pool_limit_errors == []


def test_reserved_pool_with_claimable_work_keeps_its_slot(kanban_home):
    res = _dispatch(kanban_home, ["alpha", "alpha", "beta"])
    assert [s[1] for s in res.spawned] == ["alpha", "beta"]
    assert [(e[1], e[3]) for e in res.function_pool_limit_errors] == [
        ("alpha", "GLOBAL_LIMIT_EXCEEDED"),
    ]
