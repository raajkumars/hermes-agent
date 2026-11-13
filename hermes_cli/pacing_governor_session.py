"""Pacing-governor routing decision shared by every NEW-session-start entry point: gateway/WebUI
(``gateway/gateway_pacing.py``) and cron agent-job fires (``cron/scheduler.py::_resolve_job_runtime``).

Kanban's own worker-spawn wiring (``hermes_cli/kanban_db_dispatch_pacing.py``, t_31b1338d /
t_e5ed5694) already routes new kanban worker spawns; this module extends the SAME
session-boundary-only, sticky-per-session, fail-open contract to gateway/WebUI new-session starts
and cron-triggered agent-session starts (never mid-session, never for ``--script``/``no_agent``
cron jobs, which have no model/provider at all). See t_e71698f5.

Provider identity: ``aos.pacing_governor.resolve_provider_for_new_spawn()`` only ever returns a
PROVIDER id (``claude-subscription`` or ``openai-codex``), never a specific model. Each profile's
own fallback pair (``fallback_providers``/legacy ``fallback_model`` in its config.yaml, merged by
``hermes_cli.fallback_config.get_fallback_chain``) maps a provider back to a specific model. A
profile with NO fallback entry is out of scope -- ``resolve_new_session_route`` returns ``None``
(never reroute) rather than guessing a model for an unconfigured provider.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Reserved lane: prime + the Casper/Grok-Bot personal chat-bot fleet (see
# /opt/data/brain/agents/casper-personal/INDEX.md -- lead Jansi, teammates Kavya, Tara, Neha,
# Isha, Maya, Diya, Riya, Zara). These profiles must never be rerouted by ordinary pace pressure
# alone (reserved_lane=True skips the 15% RESERVED_LANE_PCT headroom carve-out everyone else
# respects). Everyone else -- including profiles that happen to share a name with a kanban worker
# -- passes reserved_lane=False, same as kanban's own spawn wiring (t_31b1338d). This list is
# fleet-wide (hermes-overlay is installed on multiple hosts): a name not present as a local
# profile on a given host is simply never consulted, so listing the full canonical roster here is
# safe even on hosts (like choco) that only run a subset of these profiles locally.
RESERVED_LANE_PROFILES = frozenset({
    "prime", "jansi", "kavya", "tara", "neha", "isha", "maya", "diya", "riya", "zara",
})

try:
    from aos.pacing_governor import resolve_provider_for_new_spawn as _aos_resolve_provider_for_new_spawn
except ImportError:
    _aos_resolve_provider_for_new_spawn = None


def _resolve_provider(
    default_provider: str, *, reserved_lane: bool, cooldown_key: Optional[str], context: str,
) -> Optional[str]:
    """Routed provider id for a NEW session/job fire, or ``None`` on any failure (fail-open --
    caller must treat ``None`` identically to "stay on default_provider")."""
    from hermes_cli.pacing_governor_vendored import (
        DEFAULT_STATE_PATH, DEFAULT_DECISION_LOG_PATH, DEFAULT_COOLDOWN_STATE_PATH,
        resolve_provider_for_new_spawn_vendored,
    )
    try:
        if _aos_resolve_provider_for_new_spawn is not None:
            return _aos_resolve_provider_for_new_spawn(
                default_provider,
                state_path=DEFAULT_STATE_PATH,
                sticky_provider=None,
                reserved_lane=reserved_lane,
                decision_log_path=DEFAULT_DECISION_LOG_PATH,
                context=context,
                cooldown_key=cooldown_key,
                cooldown_state_path=DEFAULT_COOLDOWN_STATE_PATH,
            ) or default_provider
        return resolve_provider_for_new_spawn_vendored(
            default_provider,
            sticky_provider=None,
            reserved_lane=reserved_lane,
            context=context,
            cooldown_key=cooldown_key,
        )
    except Exception as exc:  # noqa: BLE001 -- a session/job start must never fail because the governor did
        logger.debug("pacing governor: new-session routing skipped for %s (%s)", context, exc)
        return None


def resolve_new_session_route(
    config: dict[str, Any],
    *,
    profile_name: str,
    default_provider: Optional[str],
    cooldown_key: str,
    context: str,
) -> Optional[tuple[str, str]]:
    """``(provider, model)`` to route a genuinely NEW session/job fire to, or ``None`` to leave it
    on the profile's own configured default (no fallback pair configured for this profile, the
    governor says stay on the default, or ANY error -- fail open, always).

    Never call this for a resumed/already-running session -- pass its provider through unchanged
    instead (this module has no ``sticky_provider`` parameter on purpose: every caller already
    gates on its own "is this session/job fire genuinely new" signal before reaching here, so a
    sticky short-circuit here would be redundant and a second place to get wrong).
    """
    if not default_provider:
        return None
    try:
        from hermes_cli.fallback_config import get_fallback_chain
        chain = get_fallback_chain(config)
    except Exception:
        logger.debug("pacing governor: fallback chain read failed for %s", profile_name, exc_info=True)
        return None
    fallback_entry = next(
        (e for e in chain if isinstance(e, dict) and e.get("provider") and e.get("model")), None,
    )
    if fallback_entry is None:
        return None  # no documented tier-equivalent pair for this profile -- out of scope
    fallback_provider = str(fallback_entry["provider"]).strip()
    fallback_model = str(fallback_entry["model"]).strip()
    if not fallback_provider or not fallback_model or fallback_provider == default_provider:
        return None
    reserved_lane = profile_name in RESERVED_LANE_PROFILES
    chosen = _resolve_provider(
        default_provider, reserved_lane=reserved_lane, cooldown_key=cooldown_key, context=context,
    )
    if not chosen or chosen == default_provider:
        return None
    if chosen != fallback_provider:
        # The governor named some other provider in the chain we don't have a model mapping for
        # (e.g. a 3rd-provider chain entry from the state file's own "chain" key); only ever
        # reroute to the profile's OWN documented fallback pair, never guess a model.
        return None
    return fallback_provider, fallback_model
