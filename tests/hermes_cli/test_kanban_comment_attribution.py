"""Regression coverage for t_3a498b10: ``hermes kanban comment``/``attach`` must not let a
CLI caller post as any profile via ``--author`` or ambient ``HERMES_PROFILE``/``HERMES_PROFILE_NAME``.

Inside a dispatcher-owned kanban worker, attribution comes from the dispatcher's own
``task_runs.profile`` record for the current run (set from the task's ``assignee`` at claim
time, never from the process environment); a disagreeing ``--author`` is refused rather than
silently honoured or silently overridden. Outside a worker, interactive behaviour is unchanged,
but the resolved ``HERMES_HOME`` now rides along on the ``commented`` event for audit.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _comment_args(task_id: str, text: str, author=None) -> argparse.Namespace:
    return argparse.Namespace(task_id=task_id, text=[text], author=author, max_len=None)


def _claim_as(conn, title: str, profile: str) -> tuple[str, int]:
    """Create a task assigned to ``profile`` and claim it, so the opened ``task_runs`` row
    records ``profile`` exactly like a real dispatcher-spawned worker would."""
    tid = kb.create_task(conn, title=title, assignee=profile)
    assert kb.claim_task(conn, tid, claimer=profile) is not None
    run_id = kb.get_task(conn, tid).current_run_id
    assert run_id is not None
    return tid, run_id


def test_worker_comment_ignores_ambient_profile_env_impersonation(kanban_home, monkeypatch):
    """A worker scoped to pari's task must post as 'pari' even if the shell carries a stale
    or hostile HERMES_PROFILE — the attack this card was filed against."""
    with kbc.connect_closing() as conn:
        tid, run_id = _claim_as(conn, "pari task", "pari")

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_PROFILE", "prime")
    monkeypatch.setenv("HERMES_PROFILE_NAME", "prime")

    assert kc._cmd_comment(_comment_args(tid, "hello")) == 0

    with kbc.connect_closing() as conn:
        comments = kb.list_comments(conn, tid)
    assert len(comments) == 1
    assert comments[0].author == "pari"


def test_worker_comment_with_mismatching_author_flag_is_refused(kanban_home, monkeypatch):
    """Acceptance criterion: a worker env with HERMES_KANBAN_TASK=<pari task> and
    --author prime is refused, not silently honoured."""
    with kbc.connect_closing() as conn:
        tid, run_id = _claim_as(conn, "pari task", "pari")

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    with pytest.raises(ValueError, match="disagrees with this worker's run record"):
        kc._cmd_comment(_comment_args(tid, "must be refused", author="prime"))

    # And nothing was written.
    with kbc.connect_closing() as conn:
        assert kb.list_comments(conn, tid) == []

    # Through the real CLI dispatch entry point the refusal surfaces as a non-zero exit,
    # never a 0 with a silently-wrong author (see test_kanban_cli_exit_status.py precedent).
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args(["kanban", "comment", tid, "must be refused", "--author", "prime"])
    assert kc.kanban_command(args) == 1


def test_worker_comment_with_agreeing_author_flag_is_accepted(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid, run_id = _claim_as(conn, "pari task", "pari")

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    assert kc._cmd_comment(_comment_args(tid, "fine", author="pari")) == 0
    with kbc.connect_closing() as conn:
        comments = kb.list_comments(conn, tid)
    assert comments[-1].author == "pari"


def test_worker_attach_with_mismatching_author_flag_is_refused(kanban_home, monkeypatch, tmp_path):
    with kbc.connect_closing() as conn:
        tid, run_id = _claim_as(conn, "pari task", "pari")

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    src = tmp_path / "evidence.txt"
    src.write_text("evidence")

    with pytest.raises(ValueError, match="disagrees with this worker's run record"):
        kc._cmd_attach(argparse.Namespace(
            task_id=tid, path=str(src), content_type=None, name=None, author="prime",
        ))
    with kbc.connect_closing() as conn:
        assert kb.list_attachments(conn, tid) == []


def test_interactive_comment_outside_worker_records_hermes_home_for_audit(kanban_home):
    """Outside a dispatcher-owned worker, behaviour is unchanged (best-effort author), but the
    resolved HERMES_HOME now rides along on the event for audit."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="interactive task")

    assert kc._cmd_comment(_comment_args(tid, "from the interactive CLI")) == 0

    with kbc.connect_closing() as conn:
        events = [e for e in kb.list_events(conn, tid) if e.kind == "commented"]
    assert len(events) == 1
    assert events[0].payload.get("hermes_home") == str(kanban_home)


def test_profile_author_prefers_worker_run_record_over_env(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid, run_id = _claim_as(conn, "pari task", "pari")

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_PROFILE", "prime")

    assert kc._profile_author() == "pari"


def test_profile_author_outside_worker_still_honours_env(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.setenv("HERMES_PROFILE", "raaj")

    assert kc._profile_author() == "raaj"
