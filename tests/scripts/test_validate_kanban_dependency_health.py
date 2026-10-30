"""Post-deployment Kanban dependency health validation contract."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "observability" / "validate_kanban_dependency_health.py"


def _load():
    spec = importlib.util.spec_from_file_location("validate_kanban_dependency_health", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _json_response(payload):
    return io.BytesIO(json.dumps(payload).encode())


def test_validation_hook_passes_when_deployed_api_reports_no_stranded_dependencies(monkeypatch, capsys):
    mod = _load()
    called = {}

    def fake_urlopen(url, *, timeout):
        called.update(url=url, timeout=timeout)
        return _json_response({"dependency_stranded_task_count": 0})

    monkeypatch.setattr(mod, "urlopen", fake_urlopen)

    assert mod.main(["--api-url", "https://hermes.example/"]) == 0
    assert called == {
        "url": "https://hermes.example/api/plugins/kanban/health/dependencies",
        "timeout": 10.0,
    }
    assert capsys.readouterr().out == "PASS: no dependency-stranded Kanban tasks detected.\n"


def test_validation_hook_fails_for_stranded_dependencies_reported_by_deployed_api(monkeypatch, capsys):
    mod = _load()
    called = {}

    def fake_urlopen(url, *, timeout):
        called.update(url=url, timeout=timeout)
        return _json_response({"dependency_stranded_task_count": 1})

    monkeypatch.setattr(mod, "urlopen", fake_urlopen)

    assert mod.main(["--api-url", "https://hermes.example", "--board", "release board", "--timeout", "3"]) == 1
    assert called == {
        "url": "https://hermes.example/api/plugins/kanban/health/dependencies?board=release+board",
        "timeout": 3.0,
    }
    assert capsys.readouterr().out == "FAIL: 1 dependency-stranded Kanban task(s) detected.\n"


def test_validation_hook_fails_closed_for_invalid_deployed_api_response(monkeypatch, capsys):
    mod = _load()
    monkeypatch.setattr(mod, "urlopen", lambda url, *, timeout: _json_response({"dependency_stranded_task_count": "one"}))

    assert mod.main(["--api-url", "https://hermes.example"]) == 2
    assert "ERROR: dependency health response" in capsys.readouterr().out