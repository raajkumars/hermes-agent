"""Cron delivery targets fail early when they cannot receive pushed reports."""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def cron_env(tmp_path, monkeypatch):
    """Isolated cron store for real create/update tool-path validation."""
    hermes_home = tmp_path / ".hermes"
    (hermes_home / "cron" / "output").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import cron.jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", hermes_home / "cron" / "output")
    return hermes_home


@pytest.mark.parametrize("deliver, platform", [
    ("webui:abc", "webui"),
    ("nonsense:abc", "nonsense"),
])
def test_tool_create_rejects_unknown_or_non_push_delivery_platform(cron_env, deliver, platform):
    from tools.cronjob_tools import cronjob

    result = json.loads(cronjob(
        action="create", prompt="scan", schedule="every 1h", deliver=deliver,
    ))

    assert result["success"] is False
    assert platform in result["error"]


@pytest.mark.parametrize("deliver, platform", [
    ("webui:abc", "webui"),
    ("nonsense:abc", "nonsense"),
])
def test_tool_update_rejects_unknown_or_non_push_delivery_platform(cron_env, deliver, platform):
    from cron.jobs import create_job, get_job
    from tools.cronjob_tools import cronjob

    job = create_job(prompt="scan", schedule="every 1h", deliver="local")
    job_id = job["id"]
    result = json.loads(cronjob(action="update", job_id=job_id, deliver=deliver))

    assert result["success"] is False
    assert platform in result["error"]
    persisted = get_job(job_id)
    assert persisted is not None and persisted["deliver"] == "local"
