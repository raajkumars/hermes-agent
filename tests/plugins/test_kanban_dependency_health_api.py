"""Contract tests for the content-free Kanban dependency health API."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    mod_name = "hermes_dashboard_plugin_kanban_dependency_health_api_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name].router
    spec = importlib.util.spec_from_file_location(mod_name, plugin_file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module.router


@pytest.fixture
def client(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def test_dependency_health_endpoint_reports_only_stranded_dependency_count(client):
    with kbc.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="ira")
        kb.create_task(conn, title="child", assignee="ira", parents=[parent])
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))

    response = client.get("/api/plugins/kanban/health/dependencies")

    assert response.status_code == 200
    assert response.json() == {
        "board": "default",
        "dependency_stranded_task_count": 1,
    }