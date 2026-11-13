"""Regression tests for t_c535c0d4: the pacing governor must never swap a kanban worker's
``--provider`` without also pinning a ``-m`` model valid on that provider.

Bug: ``_worker_argv`` (hermes_cli/kanban_db_dispatch.py) built ``--provider <governor pick>``
via ``kanban_db_dispatch_pacing.resolve_worker_provider`` completely independent of
``task.model_override``. When the task had no ``model_override`` (the common case -- kanban
tasks are rarely pinned to a model), the profile's own configured ``model.default`` rode along
unchanged onto whatever provider the governor picked. Evidence: t_23f5d60f (primex, config
``model.default: gpt-5.6-sol`` / ``model.provider: openai-codex``, no ``fallback_model``
entry) crashed 3x 2026-09-29 with ``[claude-code:unrecognized_model] {"model":"gpt-5.6-sol"}``
after the governor routed it to ``claude-subscription``.

Fix: ``resolve_worker_provider_and_model`` reuses the SAME profile-fallback-pair contract the
gateway (t_e71698f5, ``gateway/gateway_pacing.py``) and cron (t_e71698f5,
``cron/scheduler.py``) new-session entry points already use --
``hermes_cli.pacing_governor_session.resolve_new_session_route`` -- instead of routing on
provider alone. A kanban worker is only ever rerouted to a provider the profile has a
documented ``fallback_providers``/``fallback_model`` pair for; with no such pair, the profile's
own provider (and therefore its own model) is left untouched -- no crash, just no pacing
benefit for that spawn, exactly like the gateway/cron paths already behave.

Test isolation note: ``resolve_new_session_route`` -> ``_resolve_provider`` calls
``pacing_governor_vendored.resolve_provider_for_new_spawn_vendored`` relying on that function's
own DEFAULT_STATE_PATH/DEFAULT_DECISION_LOG_PATH/DEFAULT_COOLDOWN_STATE_PATH *default parameter
values*, which are bound once at module-import time -- monkeypatching the module attributes
after import has NO effect on those already-bound defaults (classic Python early-binding
default-argument trap) and would silently read/write this host's real
``~/.qwickapps/state/*.json``. The established, correct seam (matching
``tests/hermes_cli/test_pacing_governor_session.py``'s own pattern) is monkeypatching
``hermes_cli.pacing_governor_session._resolve_provider`` directly -- the governor DECISION is a
pure input to ``resolve_new_session_route``'s fallback-pair logic, which is what this module
actually tests.
"""

from __future__ import annotations

import json
import subprocess


def _make_task(kb, *, assignee: str = "primex", provider_override=None, model_override=None):
    return kb.Task(
        id="t_pace_model_pin",
        title="pace model pin",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
        provider_override=provider_override,
        model_override=model_override,
    )


def _profile_root(tmp_path, *, model_provider="openai-codex", model_default="gpt-5.6-sol",
                   fallback=None):
    """Mirrors primex's real config.yaml shape (model.provider/model.default), optionally with
    a documented fallback pair."""
    root = tmp_path / ".hermes"
    profile = root / "profiles" / "primex"
    profile.mkdir(parents=True)
    cfg = {"model": {"provider": model_provider, "default": model_default}}
    if fallback:
        cfg["fallback_providers"] = [fallback]
    profile.joinpath("config.yaml").write_text(json.dumps(cfg), encoding="utf-8")
    profile.joinpath("auth.json").write_text(
        json.dumps({"active_provider": model_provider, "providers": {model_provider: {}}}),
        encoding="utf-8",
    )
    root.joinpath("config.yaml").write_text("{}\n", encoding="utf-8")
    return root, profile


def _mock_governor_decision(monkeypatch, chosen_provider):
    """Stub the governor's own pace-based provider pick -- the seam
    ``hermes_cli.pacing_governor_session._resolve_provider`` (same pattern
    ``test_pacing_governor_session.py`` uses), so these tests exercise ONLY the fallback-pair
    routing logic this module is actually about, not the on-disk state-file machinery."""
    import hermes_cli.pacing_governor_session as pgs

    monkeypatch.setattr(pgs, "_resolve_provider", lambda *a, **k: chosen_provider)


# --- Unit tests of resolve_worker_provider_and_model ------------------------------------


def test_no_fallback_pair_stays_on_profile_provider_no_crash(monkeypatch, tmp_path):
    """ACCEPTANCE (t_c535c0d4): primex's real-world shape -- config.yaml has model.provider/
    model.default but NO fallback_providers/fallback_model entry. The governor wants to route
    away from the over-pace primary, but with nothing documenting what model to run on the
    target provider, the function must NOT reroute -- (None, None), byte-identical to
    pre-governor dispatch, never a provider swap that strands the profile's own model."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch_pacing as kbdp

    root, profile = _profile_root(tmp_path)  # no fallback configured
    _mock_governor_decision(monkeypatch, "claude-subscription")
    monkeypatch.setattr(kbdp, "_resolve_profile_active_provider", lambda hermes_home: "openai-codex")

    task = _make_task(kb)
    provider, model = kbdp.resolve_worker_provider_and_model(task, str(profile))

    assert (provider, model) == (None, None)


def test_documented_fallback_pair_routes_provider_and_model_together(monkeypatch, tmp_path):
    """ACCEPTANCE: with a documented fallback pair, the function returns BOTH the routed
    provider AND its matching model atomically -- never one without the other."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch_pacing as kbdp

    root, profile = _profile_root(
        tmp_path,
        fallback={"provider": "claude-subscription", "model": "claude-opus-5-5"},
    )
    _mock_governor_decision(monkeypatch, "claude-subscription")
    monkeypatch.setattr(kbdp, "_resolve_profile_active_provider", lambda hermes_home: "openai-codex")

    task = _make_task(kb)
    provider, model = kbdp.resolve_worker_provider_and_model(task, str(profile))

    assert (provider, model) == ("claude-subscription", "claude-opus-5-5")


def test_governor_picks_provider_outside_documented_pair_stays_put(monkeypatch, tmp_path):
    """If the governor names some OTHER provider than the profile's own documented fallback
    (e.g. a 3rd entry in the state file's chain), never guess a model for it -- stay put."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch_pacing as kbdp

    root, profile = _profile_root(
        tmp_path,
        fallback={"provider": "claude-subscription", "model": "claude-opus-5-5"},
    )
    _mock_governor_decision(monkeypatch, "some-other-provider")
    monkeypatch.setattr(kbdp, "_resolve_profile_active_provider", lambda hermes_home: "openai-codex")

    task = _make_task(kb)
    provider, model = kbdp.resolve_worker_provider_and_model(task, str(profile))

    assert (provider, model) == (None, None)


def test_explicit_provider_override_skips_governor_and_model_lookup(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch_pacing as kbdp

    called = {}
    monkeypatch.setattr(
        kbdp, "_resolve_profile_active_provider",
        lambda hermes_home: called.setdefault("called", True) or "should-not-be-used",
    )

    task = _make_task(kb, provider_override="pinned-provider")
    provider, model = kbdp.resolve_worker_provider_and_model(task, str(tmp_path))

    assert (provider, model) == ("pinned-provider", None)
    assert "called" not in called


def test_governor_exception_fails_open(monkeypatch, tmp_path):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch_pacing as kbdp

    def _boom(hermes_home):
        raise RuntimeError("governor state unreachable")

    monkeypatch.setattr(kbdp, "_resolve_profile_active_provider", _boom)

    task = _make_task(kb)
    assert kbdp.resolve_worker_provider_and_model(task, str(tmp_path)) == (None, None)


# --- E2E: the real _worker_argv/_default_spawn integration ------------------------------


def test_default_spawn_never_emits_bare_provider_swap_without_model(monkeypatch, tmp_path):
    """ACCEPTANCE (t_c535c0d4), reproduces t_23f5d60f: a primex-shaped profile (model.provider=
    openai-codex, model.default=gpt-5.6-sol, no fallback pair) with no task.model_override, whose
    primary provider is over pace. Before the fix this emitted ``--provider claude-subscription``
    with NO ``-m`` flag, so the worker process resolved gpt-5.6-sol (a Codex-only model) against
    claude-subscription and crashed with [claude-code:unrecognized_model]. After the fix, no
    fallback pair is documented so the spawn must stay on the profile's own provider entirely --
    no --provider flag at all, exactly like pre-governor dispatch."""
    root, profile = _profile_root(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root))
    _mock_governor_decision(monkeypatch, "claude-subscription")

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])

    captured = {}

    class FakeProc:
        pid = 6001

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    pid = kbd._default_spawn(_make_task(kb, assignee="primex"), str(workspace))

    assert pid == 6001
    cmd = captured["cmd"]
    if "--provider" in cmd:
        # If a provider swap IS present, a matching -m MUST be present too -- the invariant
        # this whole card exists to enforce. (Not expected to trigger here: no fallback pair
        # is configured, so the correct behavior is to omit --provider entirely.)
        assert "-m" in cmd, f"--provider present without -m in {cmd!r} -- would crash the worker"
    else:
        assert "-m" not in cmd  # no override at all: byte-identical to pre-governor dispatch


def test_default_spawn_pins_matching_model_when_fallback_documented(monkeypatch, tmp_path):
    """With a documented fallback pair, the spawn carries BOTH --provider and the matching -m."""
    root, profile = _profile_root(
        tmp_path,
        fallback={"provider": "claude-subscription", "model": "claude-opus-5-5"},
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    _mock_governor_decision(monkeypatch, "claude-subscription")

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])

    captured = {}

    class FakeProc:
        pid = 6002

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kbd._default_spawn(_make_task(kb, assignee="primex"), str(workspace))

    cmd = captured["cmd"]
    assert "--provider" in cmd
    assert cmd[cmd.index("--provider") + 1] == "claude-subscription"
    assert "-m" in cmd
    assert cmd[cmd.index("-m") + 1] == "claude-opus-5-5"


def test_default_spawn_explicit_task_model_override_still_wins(monkeypatch, tmp_path):
    """A task-level model_override (operator pin) must never be clobbered by the governor's
    fallback-pair model -- it is checked first and the governor path only fills the gap."""
    root, profile = _profile_root(
        tmp_path,
        fallback={"provider": "claude-subscription", "model": "claude-opus-5-5"},
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    _mock_governor_decision(monkeypatch, "claude-subscription")

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])

    captured = {}

    class FakeProc:
        pid = 6003

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    task = _make_task(kb, assignee="primex", model_override="operator-pinned-model")
    kbd._default_spawn(task, str(workspace))

    cmd = captured["cmd"]
    assert cmd.count("-m") == 1
    assert cmd[cmd.index("-m") + 1] == "operator-pinned-model"
