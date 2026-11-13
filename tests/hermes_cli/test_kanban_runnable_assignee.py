"""Creation/decomposition routing must never target a profile with no provider credential."""

from __future__ import annotations

import pytest

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