"""Kanban workers must boot from workspaces that contain a shadow package directory."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


def test_worker_module_boots_from_workspace_with_shadow_hermes_cli(
    tmp_path: Path, monkeypatch
) -> None:
    """The dispatcher keeps the workspace cwd while making its real package importable.

    An empty ``hermes_cli/`` directory is a namespace-package candidate.  The
    module-form worker must still import the installed Hermes package before it
    reaches normal CLI argument handling.
    """
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    profile.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.joinpath("hermes_cli").mkdir(parents=True)

    task = kb.Task(
        id="t_shadow_import",
        title="shadow import regression",
        body=None,
        assignee="worker",
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=1,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=str(workspace),
        claim_lock="test:worker",
        claim_expires=999,
        tenant=None,
        current_run_id=1,
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        "tools.environments.local.build_subprocess_env",
        lambda **_kwargs: {"PATH": os.environ["PATH"]},
    )
    monkeypatch.setattr(kbd, "_resolve_worker_cli_toolsets", lambda _home: None)
    monkeypatch.setattr(kbd, "_retag_legacy_worker_sessions", lambda _root: None)
    monkeypatch.setattr(kbd, "_restart_safe_worker_argv", lambda _task, command: command)
    monkeypatch.setattr("tools.process_registry.systemd_user_bus_env", lambda env: env)
    monkeypatch.setattr(
        kbd,
        "_worker_argv",
        lambda *_args: [sys.executable, "-m", "hermes_cli.main", "--help"],
    )

    pid = kbd._default_spawn(task, str(workspace))
    assert pid is not None
    _pid, status = os.waitpid(pid, 0)

    assert os.waitstatus_to_exitcode(status) == 0
    log = kb.read_worker_log(task.id)
    assert log is not None and "usage:" in log


def test_hermes_cli_startup_import_error_blocks_after_one_crash(tmp_path: Path, monkeypatch) -> None:
    """A known pre-start ImportError is sticky instead of repeatedly requeued."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="shadow failure", assignee="worker", max_retries=9)
        dead = kbd._DeadWorker(
            kind="nonzero_exit",
            code=1,
            error_text=(
                "pid 42 exited with code 1. Worker's last output: "
                "ImportError: cannot import name '__version__' from 'hermes_cli' "
                "(unknown location)"
            ),
            event_kind="crashed",
            event_payload={},
        )

        assert kbd._account_crashes(conn, [(task_id, 42, "host:worker", dead)]) == [task_id]
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.consecutive_failures == 1

        gave_up = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='gave_up'", (task_id,)
        ).fetchone()
        assert gave_up is not None
        assert kb._json_dict(gave_up["payload"])["startup_import_error"] is True

        # The normal ready sweep cannot silently resume the same startup crash.
        assert kb.recompute_ready(conn) == 0
        task_after_sweep = kb.get_task(conn, task_id)
        assert task_after_sweep is not None and task_after_sweep.status == "blocked"
