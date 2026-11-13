"""Two lifecycle invariants, using real SQLite and a local GitHub HTTP contract."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect


@pytest.fixture
def github(tmp_path, monkeypatch):
    state = {"conclusion": "success", "head": "a" * 40, "reads": 0, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            sha = state["head"]
            if self.path == "/graphql":
                value = {"data": {"repository": {"pullRequest": {
                    # t_e5a5924c: a CLOSED, unmerged pull request is the one
                    # acceptance outcome no retry can ever change, so the suite
                    # has to be able to produce it.
                    "headRefOid": sha, "baseRefName": "main",
                    "state": state.get("pr_state", "OPEN"),
                    "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                        {"context": "required", "app": {"databaseId": 1}}]}}}}}}
            elif "/rules/branches/" in self.path:
                value = [[]]
            elif "/check-runs" in self.path:
                run = {"id": 42, "name": "required", "head_sha": sha,
                       "app": {"id": 1}, "status": "in_progress" if state["conclusion"] == "pending" else "completed", "conclusion": state["conclusion"],
                       "html_url": "https://github.com/acme/repo/actions/runs/42"}
                if state.get("stale"):
                    run["head_sha"] = "b" * 40
                runs = [] if state.get("missing") else [run]
                value = [{"total_count": 100 + len(runs), "check_runs": [
                    {**run, "id": 1000 + i, "name": "optional", "conclusion": "skipped"}
                    for i in range(100)]}, {"total_count": 100 + len(runs), "check_runs": runs}]
                if state.get("race"):
                    state["race"]()
                if state.get("head_change"):
                    state["head"] = "b" * 40
            elif "/statuses" in self.path:
                value = [[]]
            elif "/pulls/" in self.path:
                value = {"head": {"sha": sha}, "base": {"ref": "main"}, "state": "open"}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    shim = tmp_path / "bin"
    shim.mkdir()
    gh = shim / "gh"
    gh.write_text(f"#!{sys.executable}\nimport sys,urllib.request\n"
                  f"u='http://127.0.0.1:{server.server_port}/'+sys.argv[2]\n"
                  "print(urllib.request.urlopen(u).read().decode())\n")
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.linux_only
def test_pr_completion_requires_current_required_evidence(github):
    with connect() as conn:
        for conclusion in ("failure", "pending", "cancelled", "timed_out", "action_required", "neutral", "skipped", None, "success"):
            github.update(conclusion=conclusion, head="a" * 40)
            tid = kb.create_task(conn, title="Publish", completion_contract="acme/repo")
            ok = kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert ok is (conclusion == "success")
            task = kb.get_task(conn, tid)
            assert (task.status == "done") is ok
            receipts = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,))]
            assert receipts and receipts[-1]["head_sha"] == "a" * 40
            if not ok:
                assert task.status in {"running", "ready", "blocked", "review"}
                assert "retry" in receipts[-1]["recovery"]
                assert receipts[-1]["checks"][0]["id"] == 42
        for fault in ("missing", "stale", "head_change"):
            github.update(conclusion="success", head="a" * 40)
            github[fault] = True
            tid = kb.create_task(conn, title=fault, completion_contract="acme/repo")
            assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).status != "done"
            github.pop(fault)
        # Omission and a sibling repository cannot downgrade the stored declaration.
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        assert not kb.complete_task(conn, tid, summary="local green")
        assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": "https://github.com/other/repo/pull/7"})
        before = len(github["requests"])
        local = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, local, summary="https://github.com/acme/repo/pull/7 is background context")
        assert len(github["requests"]) == before


@pytest.mark.linux_only
def test_published_pr_shape_is_caller_input_not_github_infra(github):
    """An object-shaped published_pr carries its URL; any other shape is a caller error."""
    url = "https://github.com/acme/repo/pull/7"
    with connect() as conn:
        tid = kb.create_task(conn, title="object", body="publish", completion_contract="acme/repo")
        assert kb.complete_task(conn, tid, result="done", metadata={"published_pr": {"url": url, "number": 7}})
        assert kb.get_task(conn, tid).completion_contract == url
        for bad in ({"number": 7}, 7, [url]):
            tid = kb.create_task(conn, title="bad", body="publish", completion_contract="acme/repo")
            before = len(github["requests"])
            assert not kb.complete_task(conn, tid, result="done", metadata={"published_pr": bad})
            receipt = json.loads(conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance' ORDER BY id DESC",
                (tid,)).fetchone()[0])
            assert receipt["classification"] == "missing"
            assert "published_pr must be the PR URL string" in receipt["detail"]
            assert len(github["requests"]) == before


@pytest.mark.linux_only
def test_a_refused_completion_reports_why_not_unknown_id_or_terminal_state(github, capsys):
    """t_e5a5924c: `kanban complete` told the truth about why it refused.

    The incident. A card bound to a pull request that was CLOSED without being
    merged can never be accepted -- acceptance requires OPEN or MERGED -- while
    `kanban edit --completion-contract` refuses to replace the binding
    ("publication binds once") and `--force` overrides only the live-claim
    fence. The card sat in `ready`, and every `kanban complete` answered
    `cannot complete <id> (unknown id or terminal state)`: the id existed and
    the status was not terminal, so BOTH halves were false and the real reason
    (the acceptance receipt, already on the event log) was never surfaced.
    Archiving by hand was the only way off the board.

    This asserts the reason now reaches the caller, that the bare-bool contract
    every other caller relies on is unchanged, and that a card which really is
    in a terminal state still gets the terminal-state wording.
    """
    import argparse

    from hermes_cli import kanban as kcli

    url = "https://github.com/acme/repo/pull/7"

    def complete_cli(tid):
        args = argparse.Namespace(task_ids=[tid], summary="done", metadata=None,
                                  result="done", force=False)
        return kcli._cmd_complete(args)

    with connect() as conn:
        github.update(conclusion="success", head="a" * 40, pr_state="CLOSED")
        tid = kb.create_task(conn, title="publish", completion_contract="acme/repo")
        ok, reason = kb.complete_task(conn, tid, result="done", with_reason=True,
                                      metadata={"published_pr": url})
        assert ok is False
        # The reason is the acceptance receipt's own words, read off the card,
        # and the receipt names the real state instead of blaming gh auth.
        assert reason and "PR acceptance closed" in reason, reason
        assert "was not merged" in reason and "never succeed" in reason
        assert "gh authentication" not in reason
        assert "unknown id" not in reason
        # ... for a card that is demonstrably neither unknown nor terminal.
        task = kb.get_task(conn, tid)
        assert task is not None and task.status in {"running", "ready", "blocked", "review"}
        assert task.completion_contract == url  # publication bound on the first attempt

        # Every caller that does not ask for a reason still sees a bare bool.
        assert kb.complete_task(conn, tid, result="done",
                                metadata={"published_pr": url}) is False

    # The CLI surfaces it, and names exits that exist instead of inviting a
    # retry that cannot succeed.
    assert complete_cli(tid) == 1
    err = capsys.readouterr().err
    assert "unknown id or terminal state" not in err, err
    assert "PR acceptance" in err
    assert "binds once" in err and "archive" in err

    # A genuinely terminal card still reads as terminal.
    with connect() as conn:
        done = kb.create_task(conn, title="local", completion_contract="local-only")
        assert kb.complete_task(conn, done, summary="finished")
        ok, reason = kb.complete_task(conn, done, summary="again", with_reason=True)
        assert ok is False
        assert "status is 'done'" in reason, reason


@pytest.mark.linux_only
def test_acceptance_receipts_and_terminal_write_share_run_ownership(github):
    with connect() as conn:
        for conclusion in ("success", "failure"):
            tid = kb.create_task(conn, title="race", completion_contract="acme/repo")
            owner = kb.claim_task(conn, tid)
            run_id = owner.current_run_id
            def reclaim():
                with connect() as rival:
                    assert kb.block_task(rival, tid, reason="Reassigned during acceptance")
                    assert kb.unblock_task(rival, tid)
                    github["replacement"] = kb.claim_task(rival, tid).current_run_id
            github.update(conclusion=conclusion, race=reclaim)
            assert not kb.complete_task(conn, tid, result="done", expected_run_id=run_id,
                metadata={"published_pr": "https://github.com/acme/repo/pull/7"})
            assert kb.get_task(conn, tid).current_run_id == github["replacement"]
            assert github["replacement"] != run_id
            assert kb.get_task(conn, tid).status != "done"
            assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0] == 0
            github.pop("race")
