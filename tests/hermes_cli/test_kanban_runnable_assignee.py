"""Creation/decomposition routing must never target a profile with no provider credential."""

from __future__ import annotations

import argparse

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp
from hermes_cli import kanban_db_dispatch as kbd


def test_select_runnable_assignee_prefers_requested_profile(monkeypatch):
    monkeypatch.setattr(
        kbd, "_run_credential_probe",
        lambda profile, timeout=15.0: {"ok": profile == "engineer"},
    )

    assert kbd.select_runnable_assignee("engineer", fallback="default") == "engineer"


def test_select_runnable_assignee_redirects_to_healthy_fallback(monkeypatch):
    monkeypatch.setattr(
        kbd, "_run_credential_probe",
        lambda profile, timeout=15.0: {"ok": profile == "default"},
    )

    assert kbd.select_runnable_assignee("kanban", fallback="default") == "default"


def test_select_runnable_assignee_rejects_when_no_candidate_is_healthy(monkeypatch):
    monkeypatch.setattr(kbd, "_run_credential_probe", lambda profile, timeout=15.0: {"ok": False})

    with pytest.raises(ValueError, match="not runnable"):
        kbd.select_runnable_assignee("kanban", fallback="default")


def test_decomposer_excludes_credentialless_profiles(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        decomp.profiles_mod,
        "list_profiles",
        lambda: [
            SimpleNamespace(name="kanban", description="dispatcher", is_default=False),
            SimpleNamespace(name="engineer", description="builds", is_default=True),
        ],
    )
    monkeypatch.setattr(decomp, "_profile_is_runnable", lambda name: name == "engineer")

    roster, names = decomp._build_roster()

    assert [entry["name"] for entry in roster] == ["engineer"]
    assert names == {"engineer"}


def test_routing_rejects_when_no_profile_is_runnable(monkeypatch):
    monkeypatch.setattr(decomp, "_profile_is_runnable", lambda _name: False)
    monkeypatch.setattr(decomp.profiles_mod, "profile_exists", lambda _name: True)
    monkeypatch.setattr(decomp.profiles_mod, "get_active_profile_name", lambda: "kanban")

    with pytest.raises(ValueError, match="no installed profile has a usable active-provider credential"):
        decomp._resolve_profile_from_cfg({}, "default_assignee", fallback="kanban")


def _create_args(assignee: str) -> argparse.Namespace:
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    return root.parse_args(["kanban", "create", "task", "--assignee", assignee])


def test_cli_create_redirects_unrunnable_assignee_to_healthy_fallback(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(kc, "_kanban_config", lambda: {"default_assignee": "prime"})
    monkeypatch.setattr(
        kc.kbd,
        "select_runnable_assignee",
        lambda requested, *, fallback=None: "prime" if (requested, fallback) == ("kanban", "prime") else requested,
    )
    monkeypatch.setattr(kc, "_check_dispatcher_presence", lambda: (True, ""))
    kb.init_db()

    assert kc._cmd_create(_create_args("kanban")) == 0
    with kbc.connect_closing() as conn:
        tasks = kb.list_tasks(conn)
    assert len(tasks) == 1
    assert tasks[0].assignee == "prime"


def test_cli_create_fails_closed_when_assignee_and_fallback_are_unrunnable(tmp_path, monkeypatch, capsys):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(kc, "_kanban_config", lambda: {"default_assignee": "prime"})
    monkeypatch.setattr(
        kc.kbd,
        "select_runnable_assignee",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("assignee 'kanban' is not runnable")),
    )
    kb.init_db()

    assert kc._cmd_create(_create_args("kanban")) == 2
    assert "not runnable" in capsys.readouterr().err
    with kbc.connect_closing() as conn:
        assert kb.list_tasks(conn) == []