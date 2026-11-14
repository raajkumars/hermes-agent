"""Pacing-governor wiring for gateway/WebUI NEW session starts (sibling of gateway/run_turn.py).

``GatewayTurnMixin._hmwa_open_session`` (gateway/run_turn.py) calls
:func:`resolve_new_session_model_override` immediately after computing ``_is_new_session`` -- the
codebase's own existing "is this a brand new session" signal (``session_entry.created_at ==
session_entry.updated_at`` or an auto-/fresh-reset boundary; see ``_USER_BOUNDARY_END_REASONS``
and the ``resume_pending`` machinery this mirrors). When it fires, the routed provider+model is
written into the SAME per-session ``/model`` override slot a manual switch uses
(``self._session_model_overrides[session_key]`` + ``async_session_store.set_model_override``), so
every downstream turn for this session picks it up through the ordinary, already-sticky override
precedence (session > channel_overrides > global) with ZERO new state to keep in sync -- and, by
construction, a currently-running/resumed session is never touched: this function is only ever
called from the new-session branch, never per-turn (t_e71698f5, follow-up to t_31b1338d's kanban
worker-spawn wiring and t_e5ed5694/t_a43dc41a).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def resolve_new_session_model_override(profile_name: str, config: dict[str, Any]) -> Optional[dict]:
    """Session ``/model``-override-shaped dict to pin a genuinely NEW session to, or ``None`` to
    leave it on the profile's own configured default model/provider (byte-for-byte pre-governor
    behavior). Fail-open, always: any exception here returns ``None``, never raises -- a governor
    outage or a credential-resolution failure for the routed provider must never block a new
    session's first turn.
    """
    if not profile_name:
        return None
    try:
        from hermes_cli.pacing_governor_session import resolve_new_session_route

        model_cfg = config.get("model") if isinstance(config, dict) else None
        default_provider = str((model_cfg or {}).get("provider") or "").strip()
        if not default_provider:
            from hermes_cli.auth import get_active_provider
            default_provider = get_active_provider() or ""
        route = resolve_new_session_route(
            config,
            profile_name=profile_name,
            default_provider=default_provider or None,
            cooldown_key=f"profile:{profile_name}",
            context=f"gateway:{profile_name}",
        )
        if route is None:
            return None
        provider, model = route
        from gateway.run import _resolve_runtime_agent_kwargs_for_provider
        runtime = _resolve_runtime_agent_kwargs_for_provider(provider, target_model=model)
        if not runtime.get("api_key"):
            # No usable credential for the routed provider on this host/profile -- stay on the
            # profile's own default rather than pin a session to a provider it can't actually call.
            logger.debug(
                "pacing governor: routed provider %s has no credential for profile=%s, staying put",
                provider, profile_name,
            )
            return None
        return {
            "model": model, "provider": provider, "api_key": runtime.get("api_key"),
            "base_url": runtime.get("base_url"), "api_mode": runtime.get("api_mode"),
            "request_overrides": dict(runtime.get("request_overrides") or {}),
            "capabilities": dict(runtime.get("capabilities") or {}),
        }
    except Exception:  # noqa: BLE001 -- a new session's first turn must never fail because the governor did
        logger.debug("pacing governor: new-session routing skipped for profile=%s", profile_name, exc_info=True)
        return None
