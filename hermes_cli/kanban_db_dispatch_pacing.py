"""Fail-open provider-pace concurrency throttle for Kanban worker dispatch.

The pacing poller owns the JSON state.  This module only reads its latest
atomic snapshot and reduces background fan-out when every provider in the
effective chain has usable data and is over its ordinary-worker pace line. It never fetches usage and never blocks
all progress: unknown, malformed, or stale data leaves the existing cap alone.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

PACE_POLL_INTERVAL_SECONDS = 5 * 60
SCHEDULER_GRACE_SECONDS = 60
DEFAULT_MAX_AGE_SECONDS = 2 * PACE_POLL_INTERVAL_SECONDS + SCHEDULER_GRACE_SECONDS
DEFAULT_RESERVED_LANE_PCT = 15.0


def default_state_path() -> Path:
    """Profile-scoped pace state path, resolved at dispatch time.

    Dispatcher code can run multiplexed in one gateway process, so state must
    not be captured from the launch environment or a process-global home.
    """
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "state" / "provider_pace.json"


@dataclass(frozen=True)
class ConcurrencyThrottleDecision:
    """Pacing decision suitable for a dispatch result/API payload."""

    active: bool
    base_cap: int
    effective_cap: int
    ratio: float
    provider: Optional[str]
    reason: str
    freshness: str

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _window_ratio(
    state: Mapping[str, Any], used_key: str, allowed_key: str, *, reserved_lane_pct: float,
) -> Optional[float]:
    used, allowed = _number(state.get(used_key)), _number(state.get(allowed_key))
    if used is None or allowed is None:
        return None
    effective_allowed = min(max(0.0, allowed), 100.0 - reserved_lane_pct)
    if effective_allowed == 0.0:
        return math.inf if used > 0.0 else 1.0
    return max(0.0, used) / effective_allowed


def _provider_ratio(state: Mapping[str, Any], *, reserved_lane_pct: float) -> Optional[float]:
    """Return worst current window pressure for a provider, if it is usable."""
    if state.get("error") is not None:
        return None
    ratios = [
        _window_ratio(
            state, "five_hour_used_pct", "five_hour_allowed_pct", reserved_lane_pct=reserved_lane_pct,
        ),
        _window_ratio(
            state, "weekly_used_pct", "weekly_allowed_pct", reserved_lane_pct=reserved_lane_pct,
        ),
    ]
    usable = [ratio for ratio in ratios if ratio is not None]
    if not usable:
        return None
    return max(usable)


def _fail_open(base: int, reason: str, *, freshness: str, provider: Optional[str] = None) -> ConcurrencyThrottleDecision:
    return ConcurrencyThrottleDecision(False, base, base, 1.0, provider, reason, freshness)


def resolve_concurrency_throttle(
    base_cap: Optional[int], *, state_path: Optional[Path] = None,
    now: Optional[float] = None, max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> Optional[ConcurrencyThrottleDecision]:
    """Return the effective cap, or ``None`` when the dispatcher is uncapped.

    Only a fresh state in which every provider listed in ``chain`` has usable
    over-pace data activates throttling.  This strict condition deliberately
    fails open if a provider poll fails or if any provider still has headroom.
    """
    if base_cap is None:
        return None
    base = max(1, int(base_cap))
    state_path = default_state_path() if state_path is None else state_path
    now = time.time() if now is None else now
    default = _fail_open(base, "pace data unavailable; leaving cap unchanged", freshness="unavailable")
    try:
        doc = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default
    if not isinstance(doc, dict):
        return default
    generated_at = _number(doc.get("generated_at"))
    if generated_at is None or generated_at > now:
        return default
    if now - generated_at > max_age_seconds:
        return _fail_open(base, "pace data is stale; leaving cap unchanged", freshness="stale")
    providers = doc.get("providers")
    chain = doc.get("chain")
    if not isinstance(providers, dict) or not isinstance(chain, list) or not chain:
        return default
    reserved_lane_pct = _number(doc.get("reserved_lane_pct", DEFAULT_RESERVED_LANE_PCT))
    if reserved_lane_pct is None or not 0.0 <= reserved_lane_pct <= 100.0:
        return default

    candidates: list[tuple[str, float]] = []
    for provider in chain:
        if not isinstance(provider, str) or not provider:
            return default
        raw = providers.get(provider)
        if not isinstance(raw, Mapping):
            return default
        ratio = _provider_ratio(raw, reserved_lane_pct=reserved_lane_pct)
        if ratio is None:
            return _fail_open(
                base, "a provider has no usable pace data; leaving cap unchanged", freshness="fresh",
            )
        candidates.append((provider, ratio))
    for provider, ratio in candidates:
        if ratio <= 1.0:
            return _fail_open(
                base, "a provider is under its background-lane cap; leaving cap unchanged",
                freshness="fresh", provider=provider,
            )
    provider, ratio = max(candidates, key=lambda item: item[1])
    effective = 1 if math.isinf(ratio) else max(1, min(base, round(base / ratio)))
    return ConcurrencyThrottleDecision(
        True, base, effective, ratio, provider,
        "every provider is over pace; limiting new background workers by worst fleet pressure", "fresh",
    )
