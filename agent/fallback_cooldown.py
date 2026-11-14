"""Primary rate-limit cooldown arming and per-session model rejection markers, shared by the
fallback walk (chat_completion_helpers) and restore_primary_runtime (agent_runtime_helpers)."""
import logging
import math
import time

from agent.error_classifier import FailoverReason

logger = logging.getLogger(__name__)

_RATE_LIMIT_FAILOVER_REASONS = frozenset({FailoverReason.rate_limit, FailoverReason.billing, FailoverReason.upstream_rate_limit})


def _provider_reset_delay(reset_at) -> float | None:
    """Seconds until the provider-declared reset, or None when missing/invalid/expired."""
    from agent.credential_pool import _parse_absolute_timestamp
    parsed = _parse_absolute_timestamp(reset_at)
    delay = parsed - time.time() if parsed is not None else None
    if delay is not None and math.isfinite(delay) and delay > 0:
        return delay
    return None


def switch_deferred_by_reset(agent, reason: "FailoverReason | None", reset_at) -> bool:
    """Opt-in ``fallback.min_switch_reset_seconds`` (default 0 = off, #117484): when the primary's
    rate limit reopens sooner than N seconds, switching model mid-task costs more than waiting, so
    the fallback walk is skipped and the retry loop's own backoff rides out the window. Only for
    rate-limit failovers leaving the primary with a valid future ``reset_at``."""
    if reason not in _RATE_LIMIT_FAILOVER_REASONS or getattr(agent, "_fallback_activated", False):
        return False
    try:
        from hermes_cli.config import load_config
        threshold = float((load_config() or {}).get("fallback", {}).get("min_switch_reset_seconds") or 0)
    except Exception:
        return False
    if threshold <= 0:
        return False
    delay = _provider_reset_delay(reset_at)
    if delay is None or delay >= threshold:
        return False
    logging.info("Rate limit resets in %.0f s (< fallback.min_switch_reset_seconds=%.0f): staying on the primary", delay, threshold)
    return True


def _arm_rate_limit_cooldown(
    agent, reason: "FailoverReason | None", reset_at=None,
) -> int | None:
    """Arm the primary cooldown until the provider reset, or use exponential backoff.

    ``reset_at`` is an absolute wall-clock timestamp while ``_rate_limited_until`` is monotonic;
    convert through a duration so wall-clock epoch values never enter the monotonic comparison.
    Missing, invalid, or expired provider resets retain the 60s → 2m → ... → 4h fallback.
    Only arm when leaving the primary: chain-switching from an active fallback means the primary
    was not the failing source. Return the armed cooldown in seconds, or None when not armed.
    """
    if reason not in _RATE_LIMIT_FAILOVER_REASONS:
        return None
    current_provider = (getattr(agent, "provider", "") or "").strip().lower()
    primary_provider = ((agent._primary_runtime or {}).get("provider") or "").strip().lower()
    if getattr(agent, "_fallback_activated", False) and not (primary_provider and current_provider == primary_provider):
        return None
    backoff_count = getattr(agent, "_rate_limit_backoff_count", 0)
    agent._rate_limit_backoff_count = backoff_count + 1
    provider_delay = _provider_reset_delay(reset_at)
    if provider_delay is not None:
        backoff_seconds = math.ceil(provider_delay)
        source = "provider reset"
    else:
        backoff_seconds = min(60 * (2 ** backoff_count), 14400)
        source = "exponential fallback"
    agent._rate_limited_until = time.monotonic() + backoff_seconds
    logging.info(
        "Rate-limit backoff level %d: cooldown %d s (%.1f min, backoff#%d, %s)",
        backoff_count, backoff_seconds, backoff_seconds / 60, backoff_count + 1, source,
    )
    _publish_rate_limit_hold(current_provider or primary_provider, backoff_seconds, source)
    return backoff_seconds


def _publish_rate_limit_hold(provider: str, backoff_seconds: float, source: str) -> None:
    """Share this process's primary wall with the kanban dispatcher (t_5b853ddb) through the
    pacing governor's state dir, so it stops spawning workers onto a walled provider. Best
    effort: a failed write only means the dispatcher keeps its pre-hold behaviour."""
    try:
        from hermes_cli.pacing_governor_vendored import record_rate_limit
        until = record_rate_limit(provider, time.time() + backoff_seconds, source=source)
    except Exception:  # noqa: BLE001 -- never let hold bookkeeping break the failover path
        logger.debug("rate-limit hold publish failed for %s", provider, exc_info=True)
        return
    if until:
        logger.info("pacing governor: %s rate-limit hold recorded until %s (%s)",
                    provider, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until)), source)


def kanban_worker_pauses_on_rate_limit(agent, reason: "FailoverReason | None") -> bool:
    """True when a Kanban worker must NOT fail over off a rate-limited primary (t_5b853ddb).

    A board fans out many workers per profile; when the primary subscription walls, failing
    every one of them over moves the whole backlog onto the fallback subscription and drains
    it (2026-09-30: ~195M Codex tokens in 4h). The worker instead ends the turn on the
    rate-limit, exits ``KANBAN_RATE_LIMIT_EXIT_CODE`` and is requeued; the dispatcher holds
    respawns until the recorded reset. Interactive sessions (no ``HERMES_KANBAN_TASK``) keep
    failing over. ``kanban.pause_on_rate_limit: false`` restores failover for workers."""
    import os
    if reason not in _RATE_LIMIT_FAILOVER_REASONS or getattr(agent, "_fallback_activated", False):
        return False
    if not os.environ.get("HERMES_KANBAN_TASK"):
        return False
    try:
        from hermes_cli.config import load_config
        kanban_cfg = (load_config() or {}).get("kanban") or {}
    except Exception:  # noqa: BLE001 -- unreadable config keeps the safe default (pause)
        kanban_cfg = {}
    if kanban_cfg.get("pause_on_rate_limit", True) is False:
        return False
    logger.warning(
        "kanban worker %s: primary %s rate-limited (%s); pausing instead of failing over "
        "(kanban.pause_on_rate_limit)",
        os.environ.get("HERMES_KANBAN_TASK"), getattr(agent, "provider", "?"),
        getattr(reason, "value", reason),
    )
    return True


def _mark_entitlement_rejected_model(agent, api_error) -> bool:
    """Record a Codex ChatGPT-account 400 that rejects the current model for this account.

    Pool rotation runs first (recover_with_credential_pool benches (credential, model) and
    moves to the next entitled entry, #71970); this runs only once no pool entry is left for
    the model, so the (provider, model) pair is treated as dead for the session: the fallback
    walk skips it and restore_primary_runtime stops switching back — otherwise every turn
    re-fails on the primary, announces an unverified "Primary model restored", and oscillates
    forever (#106475).
    """
    if getattr(api_error, "status_code", None) != 400:
        return False
    from agent.error_classifier import CODEX_ACCOUNT_MODEL_ENTITLEMENT_MARKER
    haystack = str(getattr(api_error, "message", "") or api_error).lower()
    if CODEX_ACCOUNT_MODEL_ENTITLEMENT_MARKER not in haystack:
        return False
    provider = str(getattr(agent, "provider", "") or "").strip().lower()
    model = str(getattr(agent, "model", "") or "").strip()
    if not provider or not model:
        return False
    pool = getattr(agent, "_credential_pool", None)
    if pool is not None and pool.has_available(model=model):
        return False  # another pool entry is still eligible for this model; rotation owns it
    rejected = getattr(agent, "_entitlement_rejected_models", None)
    if rejected is None:
        rejected = agent._entitlement_rejected_models = set()
    if (provider, model) in rejected:
        return True
    rejected.add((provider, model))
    logger.warning(
        "Model entitlement rejection: this account is not entitled to %s via %s; "
        "treating it as unavailable for this session",
        model, provider,
    )
    agent._buffer_diagnostic_status(
        f"🚫 This account is not entitled to {model} via {provider}; it will be skipped "
        "until restart. Switch to an entitled model via /model or `hermes model`."
    )
    return True


def _is_entitlement_rejected(agent, provider: str, model: str) -> bool:
    """True when (provider, model) — as configured or normalized — was rejected as unentitled
    for this account (see _mark_entitlement_rejected_model)."""
    rejected = getattr(agent, "_entitlement_rejected_models", None) or ()
    if not rejected:
        return False
    if (provider, model) in rejected:
        return True
    from hermes_cli.model_normalize import normalize_model_for_provider
    return (provider, normalize_model_for_provider(model, provider)) in rejected
