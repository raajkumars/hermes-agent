"""A reviewer can always reject, and approval is one action for both mechanisms.

Replays t_28f6f28d (2026-10-23): a review card requested by the maestro console carried
``implementer: null`` (maestro is not a fleet profile, the card was assigned to the reviewer
and never claimed), ``request_changes`` refused for want of implementer provenance, and the
reviewer's only non-blocking exit was approve. The approve itself (``complete_task`` from
review) recorded a run summary only, while the merge gate reads comments.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

SHA = "f687d58d9d73acadd8ddedce1f6c737f61c2edfc"
SHA2 = "ae8bce7d615ba60ed80938fdb3c9eb25735bd86c"


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _events(conn, tid, kind):
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (tid, kind)
    ).fetchall()
    return [json.loads(r["payload"]) if r["payload"] else None for r in rows]


def _comments(conn, tid):
    return [(r["author"], r["body"]) for r in conn.execute(
        "SELECT author, body FROM task_comments WHERE task_id = ? ORDER BY id", (tid,)).fetchall()]


def _maestro_card(conn, *, body=None, implementer=None, summary="memories#183"):
    """The exact shape of the defect: card assigned to the reviewer, never claimed."""
    tid = kb.create_task(conn, title="memories: cache", assignee="meera", body=body)
    extra = {"implementer": implementer} if implementer else {}
    assert kb.request_review(conn, tid, summary=summary, reviewer="meera", **extra) is True
    return tid


def _review_claim(conn, tid):
    claimed = kb.claim_review_task(conn, tid, claimer="meera")
    assert claimed is not None
    return claimed.current_run_id


# --- fix 1: reject is reachable on a maestro-requested review ------------------------------


def test_defect_replay_legacy_null_provenance_can_still_reject(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _maestro_card(conn)
        assert _events(conn, tid, "review_requested")[0]["implementer"] is None
        run = _review_claim(conn, tid)
        ok, detail = kb.request_changes(conn, tid, reason=f"CI is red at {SHA}", expected_run_id=run)
        assert (ok, detail) == (True, None)
        task = kb.get_task(conn, tid)
        # Not routed to a guessed identity, not dispatchable, not silently approved.
        assert (task.status, task.assignee) == ("blocked", "meera")
        ev = _events(conn, tid, "changes_requested")[0]
        assert ev["implementer_provenance"] == "unestablished"
        # The rejection is visible to a gate that reads comments.
        authors_bodies = _comments(conn, tid)
        assert authors_bodies[0][0] == "meera"
        assert authors_bodies[0][1].splitlines()[0] == f"REQUEST_CHANGES: {SHA}"


def test_declared_external_implementer_is_the_provenance(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _maestro_card(conn, implementer="maestro")
        rr = _events(conn, tid, "review_requested")[0]
        assert (rr["implementer"], rr["implementer_source"]) == ("maestro", "explicit")
        run = _review_claim(conn, tid)
        ok, detail = kb.request_changes(conn, tid, reason="tests missing", expected_run_id=run)
        assert (ok, detail) == (True, "maestro")
        task = kb.get_task(conn, tid)
        assert (task.status, task.assignee) == ("blocked", "maestro")
        ev = _events(conn, tid, "changes_requested")[0]
        assert ev["external_implementer"] is True and ev["implementer_provenance"] == "established"
        assert "external implementer maestro" in _comments(conn, tid)[0][1]


def test_body_implementer_line_is_provenance_too(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _maestro_card(conn, body="PR for X\nImplementer: maestro\n")
        assert _events(conn, tid, "review_requested")[0]["implementer"] == "maestro"
        run = _review_claim(conn, tid)
        assert kb.request_changes(conn, tid, reason="no", expected_run_id=run) == (True, "maestro")


def test_rejected_external_card_stays_parked_until_unblocked(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _maestro_card(conn, implementer="maestro")
        run = _review_claim(conn, tid)
        assert kb.request_changes(conn, tid, reason="no", expected_run_id=run)[0]
        # recompute_ready must not promote a rejected card back into dispatch.
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "blocked"
        # The author revises and unblocks; it resumes as ready work for that author, not as a review.
        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "ready"


def test_external_name_that_is_a_live_profile_routes_normally(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kb, "_implementer_is_live_profile", lambda name: True)
    with kbc.connect() as conn:
        tid = _maestro_card(conn, implementer="ira")
        run = _review_claim(conn, tid)
        assert kb.request_changes(conn, tid, reason="no", expected_run_id=run) == (True, "ira")
        assert kb.get_task(conn, tid).status == "ready"


def test_implementer_declaration_is_validated(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="meera")
        ok, why = kb.request_review(conn, tid, summary="s", reviewer="meera", implementer="Not A Name!",
                                    with_reason=True)
        assert ok is False and "implementer" in why
        ok, why = kb.request_review(conn, tid, summary="s", reviewer="meera", implementer="meera",
                                    with_reason=True)
        assert ok is False and "reviewer" in why


def test_cli_request_review_takes_implementer(kanban_home: Path, capsys) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x", assignee="meera")
    rc = kanban_cli._cmd_request_review(SimpleNamespace(
        task_id=tid, summary="PR ready", reviewer="meera", metadata=None, force=False,
        implementer="maestro"))
    assert rc == 0
    with kbc.connect() as conn:
        assert _events(conn, tid, "review_requested")[0]["implementer"] == "maestro"


# --- fix 2: native approve also posts the comment the merge gate reads ------------------------


def test_review_completion_posts_the_approve_comment(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _maestro_card(conn, implementer="maestro")
        run = _review_claim(conn, tid)
        assert kb.complete_task(
            conn, tid, summary=f"approved {SHA} and {SHA2}",
            metadata={"verdict": "approved", "heads": {"memories": SHA, "aos": SHA2}},
            expected_run_id=run) is True
        comments = _comments(conn, tid)
        assert [(a, b.splitlines()[0]) for a, b in comments] == [
            ("meera", f"APPROVE: {SHA}"), ("meera", f"APPROVE: {SHA2}")]
        assert kb.COMPLETE_POSTS_APPROVAL_COMMENT is True


def test_review_completion_does_not_double_post_an_existing_approve(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _maestro_card(conn, implementer="maestro")
        run = _review_claim(conn, tid)
        kb.add_comment(conn, tid, "meera", f"APPROVE: {SHA}")
        assert kb.complete_task(conn, tid, summary=f"approved {SHA}",
                                metadata={"verdict": "approved"}, expected_run_id=run)
        assert [b.splitlines()[0] for _, b in _comments(conn, tid)] == [f"APPROVE: {SHA}"]


def test_completing_at_a_head_the_reviewer_rejected_is_refused(kanban_home: Path) -> None:
    """Native approve and the merge gate must not disagree about the same head."""
    with kbc.connect() as conn:
        tid = _maestro_card(conn, implementer="maestro")
        run = _review_claim(conn, tid)
        kb.add_comment(conn, tid, "meera", f"REQUEST_CHANGES: {SHA}\nred CI")
        with pytest.raises(kb.ReviewVerdictConflictError):
            kb.complete_task(conn, tid, summary=f"approved {SHA}",
                             metadata={"verdict": "approved"}, expected_run_id=run)
        assert kb.get_task(conn, tid).status == "running"  # nothing was written


def test_review_completion_naming_no_head_posts_nothing(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = _maestro_card(conn, implementer="maestro")
        run = _review_claim(conn, tid)
        assert kb.complete_task(conn, tid, summary="approved, no PR involved",
                                metadata={"verdict": "approved"}, expected_run_id=run)
        assert _comments(conn, tid) == []


def test_non_review_completion_posts_no_approval(kanban_home: Path) -> None:
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="dev")
        claimed = kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary=f"merged {SHA}",
                                metadata={"verdict": "approved"}, expected_run_id=claimed.current_run_id)
        assert _comments(conn, tid) == []
