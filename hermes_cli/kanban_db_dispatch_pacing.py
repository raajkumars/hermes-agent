"""Pacing-governor wiring for kanban worker spawns (sibling of kanban_db_dispatch.py).

``_worker_argv()`` calls :func:`resolve_worker_provider` immediately before building
``--provider`` so a NEW kanban worker spawn is actually routed by the pacing governor's
pace-line decision (t_31b1338d, follow-up to the ``aos/pacing_governor.py`` library landed
in t_1eb32e10 / qwickapps/aos#457). Before this module, the governor's
``resolve_provider_for_new_spawn()`` existed only as a library call nobody made.

``aos`` is a separate repo/venv and is not on hermes-agent's runtime ``sys.path`` today (no
installed package, no PYTHONPATH entry in the systemd worker units) — checked directly
against the deployed ``/opt/hermes`` venv before writing this module. This module therefore
tries the real import first (so it picks up ``aos`` for free the day it becomes an installed
dependency) and otherwise falls back to :func:`_vendored_resolve_provider_for_new_spawn`, a
tiny mirror of the same on-disk state-file contract. The vendored copy is a stopgap, not a
fork: keep it in lockstep with ``aos/pacing_governor.py``'s ``decide_next_provider`` /
``resolve_provider_for_new_spawn`` if either changes.

Provider ids: kanban's actual runtime ``--provider`` ids are ``claude-subscription`` and
``openai-codex`` (see ``plugins/model-providers/*/plugin.yaml``), which is exactly what
``aos.pacing_governor.USAGE_PROVIDER_KEY`` already maps from — the poll tick that keeps the
governor's state file fresh MUST be started with
``HERMES_PACING_CHAIN=claude-subscription,openai-codex`` (its own default,
``anthropic,codex``, does not match any state-file entry kanban ever passes as
``default_provider`` and every lookup would silently miss).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from hermes_cli.pacing_governor_vendored import (
    DEFAULT_DECISION_LOG_PATH,
    DEFAULT_STATE_PATH,
    resolve_provider_for_new_spawn_vendored,
)

# Compatibility seam retained for callers/tests that previously disabled the optional aos
# import here. Canonical routing now lives in pacing_governor_vendored.
_aos_resolve_provider_for_new_spawn = None

logger = logging.getLogger(__name__)

def _resolve_profile_active_provider(hermes_home: Optional[str]) -> Optional[str]:
    """The assignee profile's own currently-active provider -- what a worker would resolve
    to with no ``--provider`` flag at all (the pacing governor's ``default_provider``
    baseline, never an override). Scoped to ``hermes_home`` the same way
    ``kanban_db_dispatch._resolve_worker_cli_toolsets`` scopes its own config read: a
    context-local override, never a mutation of ``os.environ`` (shared by every thread in
    the dispatcher process)."""
    if not hermes_home:
        return None
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.auth import get_active_provider

    override_handle = set_hermes_home_override(hermes_home)
    try:
        return get_active_provider()
    finally:
        reset_hermes_home_override(override_handle)


def resolve_worker_provider_and_model(
    task: Any, hermes_home: Optional[str], *, sticky_provider: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """``(provider, model)`` to pin via ``--provider``/``-m`` for a kanban worker spawn, or
    ``(None, None)`` to leave the worker on the profile's own configured provider+model
    (byte-for-byte pre-governor behavior).

    Unlike :func:`resolve_worker_provider` (which only ever decided a provider), this reuses
    the SAME profile-fallback-pair contract the gateway (``gateway/gateway_pacing.py``,
    t_e71698f5) and cron (``cron/scheduler.py``, t_e71698f5) new-session entry points already
    use -- ``hermes_cli.pacing_governor_session.resolve_new_session_route`` -- instead of
    routing on provider alone. Kanban's own wiring was the ONE integration point left calling
    the provider-only path directly, which let a NEW worker spawn get ``--provider <governed>``
    with no matching ``-m``: when the task had no ``model_override`` (the common case), the
    profile's own configured model rode along onto the swapped provider unchanged and crashed
    the worker (t_c535c0d4, reproducing t_23f5d60f: primex's ``model.default: gpt-5.6-sol`` /
    ``model.provider: openai-codex``, no fallback pair, spawned with ``--provider
    claude-subscription`` and no ``-m`` -> ``[claude-code:unrecognized_model]``).

    ``task.provider_override`` always wins and skips the governor entirely, same contract as
    :func:`resolve_worker_provider`. A kanban worker is only ever rerouted to a provider the
    profile has a documented ``fallback_providers``/``fallback_model`` pair for; with no such
    pair (or ANY error resolving one), the profile's own provider -- and therefore its own
    model -- is left untouched. No configured model is a "stay on default", never a guess.

    Fail-open, always: a missing/unreadable state file, an unresolved profile provider or
    config, or ANY exception here returns ``(None, None)`` -- no ``--provider``/``-m`` flags,
    identical to dispatch before the governor existed -- rather than blocking or altering the
    spawn.
    """
    provider_override = getattr(task, "provider_override", None)
    if provider_override:
        return provider_override, None
    if sticky_provider:
        return sticky_provider, None
    task_id = getattr(task, "id", "?")
    try:
        default_provider = _resolve_profile_active_provider(hermes_home)
        if not default_provider:
            return None, None
        config = _resolve_profile_config(hermes_home)
        if not config:
            return None, None
        from hermes_cli.pacing_governor_session import resolve_new_session_route

        profile_name = str(getattr(task, "assignee", "") or "")
        route = resolve_new_session_route(
            config,
            profile_name=profile_name,
            default_provider=default_provider,
            cooldown_key=f"profile:{profile_name}" if profile_name else f"kanban:{task_id}",
            context=f"kanban:{task_id}",
        )
        if route is None:
            return None, None
        return route
    except Exception as exc:  # noqa: BLE001 -- a spawn must never fail because the governor did
        logger.debug(
            "kanban worker: pacing governor provider+model routing skipped for %s (%s)",
            task_id, exc,
        )
        return None, None


def _resolve_profile_config(hermes_home: Optional[str]) -> Optional[dict]:
    """The assignee profile's own ``config.yaml`` dict, scoped the same way
    ``_resolve_profile_active_provider`` scopes its ``auth.json`` read. Needed so
    :func:`resolve_worker_provider_and_model` can find this profile's own
    ``fallback_providers``/``fallback_model`` pair via
    ``hermes_cli.pacing_governor_session.resolve_new_session_route`` -- the same contract the
    gateway and cron new-session entry points already read config through.
    """
    if not hermes_home:
        return None
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.config import load_config

    override_handle = set_hermes_home_override(hermes_home)
    try:
        return load_config()
    finally:
        reset_hermes_home_override(override_handle)


def resolve_worker_provider(task: Any, hermes_home: Optional[str], *, sticky_provider: Optional[str] = None) -> Optional[str]:
    """Compatibility wrapper for older callers.

    Kanban dispatch uses :func:`resolve_worker_provider_and_model` so a reroute always
    carries the documented fallback model. Keep this provider-only entry point as a
    projection of that single routing path; do not reintroduce an independent vendored
    pacing implementation here.
    """
    provider_override = getattr(task, "provider_override", None)
    if provider_override:
        return provider_override
    if sticky_provider:
        return sticky_provider
    task_id = getattr(task, "id", "?")
    try:
        # Prefer the canonical provider+model route. The provider-only fallback below keeps
        # direct legacy callers working when no documented fallback model is configured.
        provider, _model = resolve_worker_provider_and_model(
            task, hermes_home,
        )
        if provider is not None:
            return provider
        default_provider = _resolve_profile_active_provider(hermes_home)
        if not default_provider:
            return None
        return resolve_provider_for_new_spawn_vendored(
            default_provider,
            state_path=DEFAULT_STATE_PATH,
            decision_log_path=DEFAULT_DECISION_LOG_PATH,
            context=f"kanban:{task_id}",
        )
    except Exception as exc:  # noqa: BLE001 -- a spawn must never fail because the governor did
        logger.debug("kanban worker: compatibility pacing route skipped for %s (%s)", task_id, exc)
        return None


# --- Rate-limit dispatch hold (t_5b853ddb) ------------------------------------------------
# Pace routing above moves NEW spawns to a provider with headroom; a hard wall on the primary
# must instead STOP the board, or the whole backlog lands on the fallback subscription.

_HOLD_PROVIDER_CACHE_TTL = 30.0
_hold_provider_cache: dict[str, tuple[float, tuple[Optional[str], Optional[str]]]] = {}
_hold_logged: set[tuple[str, str, int]] = set()


def _profile_primary_lane(assignee: str) -> tuple[Optional[str], Optional[str]]:
    """Configured (provider, model) for an assignee, cached for one dispatch tick burst."""
    now = time.monotonic()
    cached = _hold_provider_cache.get(assignee)
    if cached and now - cached[0] < _HOLD_PROVIDER_CACHE_TTL:
        return cached[1]
    from hermes_cli.profiles import resolve_profile_env

    hermes_home = resolve_profile_env(assignee)
    config = _resolve_profile_config(hermes_home) or {}
    raw_model_cfg = config.get("model")
    model_cfg: dict = raw_model_cfg if isinstance(raw_model_cfg, dict) else {}
    provider = (model_cfg.get("provider") or "").strip().lower() or (
        (_resolve_profile_active_provider(hermes_home) or "").strip().lower() or None)
    model = (model_cfg.get("default") or "").strip().lower() or None
    lane = (provider, model)
    _hold_provider_cache[assignee] = (now, lane)
    return lane


def kanban_dispatch_hold(conn: Any, task_id: str, assignee: str) -> Optional[tuple[str, float]]:
    """Return the later provider-wide or provider/model lane hold for a ready task.

    Provider overrides without a model intentionally consult only a provider-wide quota wall;
    a model override selects a lane with its provider override or the profile's primary provider.
    This keeps an unhealthy free model from demoting every model on its provider.
    """
    try:
        from hermes_cli.pacing_governor_vendored import lane_rate_limit_hold_until, rate_limit_hold_until

        try:
            row = conn.execute(
                "SELECT provider_override, model_override FROM tasks WHERE id = ?", (task_id,),
            ).fetchone()
            provider_override = ((row[0] if row else None) or "").strip().lower() or None
            model_override = ((row[1] if row else None) or "").strip().lower() or None
        except Exception as exc:  # legacy/minimal SQLite test schema has no model_override
            if "model_override" not in str(exc):
                raise
            row = conn.execute("SELECT provider_override FROM tasks WHERE id = ?", (task_id,)).fetchone()
            provider_override = ((row[0] if row else None) or "").strip().lower() or None
            model_override = None
        if provider_override and not model_override:
            # A provider-only override deliberately has no selected model lane.
            provider, model = provider_override, None
        else:
            profile_provider, profile_model = _profile_primary_lane(assignee)
            provider = provider_override or profile_provider
            model = model_override or profile_model
        provider_until = rate_limit_hold_until(provider)
        lane_until = lane_rate_limit_hold_until(provider, model) if model else None
        until = max(v for v in (provider_until, lane_until) if v is not None) if (provider_until or lane_until) else None
        if until is None:
            return None
        held_key = f"{provider}::{model}" if lane_until is not None and lane_until >= (provider_until or 0) else str(provider)
        key = (assignee, held_key, int(until))
        if key not in _hold_logged:
            _hold_logged.add(key)
            logger.info(
                "pacing governor: holding kanban dispatch for profile %s (task %s) -- %s "
                "rate-limited until %s; not failing over to the fallback provider",
                assignee, task_id, held_key,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until)),
            )
        return held_key, until
    except Exception as exc:  # noqa: BLE001 -- a hold lookup must never break dispatch
        logger.debug("kanban dispatch hold lookup skipped for %s (%s)", task_id, exc)
        return None
