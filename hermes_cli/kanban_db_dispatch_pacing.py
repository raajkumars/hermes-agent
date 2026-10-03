"""Fail-open provider-pace concurrency throttle for Kanban worker dispatch.

The pacing poller owns the JSON state.  This module only reads its latest
atomic snapshot and reduces background fan-out when every usable provider is
over its ordinary-worker pace line.  It never fetches usage and never blocks
all progress: unknown, malformed, or stale data leaves the existing cap alone.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

DEFAULT_MAX_AGE_SECONDS = 15 * 60
_RESERVED_LANE_PCT = 15.0


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

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _window_ratio(state: Mapping[str, Any], used_key: str, allowed_key: str) -> Optional[float]:
    used, allowed = _number(state.get(used_key)), _number(state.get(allowed_key))
    if used is None or allowed is None:
        return None
    allowed = max(0.0, min(allowed, 100.0 - _RESERVED_LANE_PCT))
    if used <= allowed:
        return None
    return max(0.0, min(1.0, allowed / used))


def _provider_ratio(state: Mapping[str, Any]) -> Optional[float]:
    """Return a provider's headroom ratio only when its usable data is over pace."""
    if state.get("error") is not None:
        return None
    ratios = [
        _window_ratio(state, "five_hour_used_pct", "five_hour_allowed_pct"),
        _window_ratio(state, "weekly_used_pct", "weekly_allowed_pct"),
    ]
    over = [ratio for ratio in ratios if ratio is not None]
    if not over:
        return None
    # The binding (most over) quota window limits the provider's safe work rate.
    return min(over)


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
    default = ConcurrencyThrottleDecision(False, base, base, 1.0, None, "pace data unavailable; leaving cap unchanged")
    try:
        doc = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default
    if not isinstance(doc, dict):
        return default
    generated_at = _number(doc.get("generated_at"))
    if generated_at is None or generated_at > now + 60 or now - generated_at > max_age_seconds:
        return ConcurrencyThrottleDecision(False, base, base, 1.0, None, "pace data missing or stale; leaving cap unchanged")
    providers = doc.get("providers")
    chain = doc.get("chain")
    if not isinstance(providers, dict) or not isinstance(chain, list) or not chain:
        return default
    candidates: list[tuple[str, float]] = []
    for provider in chain:
        if not isinstance(provider, str) or not provider:
            return default
        raw = providers.get(provider)
        if not isinstance(raw, Mapping):
            return default
        ratio = _provider_ratio(raw)
        if ratio is None:
            return ConcurrencyThrottleDecision(False, base, base, 1.0, None, "a provider has headroom or no usable pace data; leaving cap unchanged")
        candidates.append((provider, ratio))
    provider, ratio = max(candidates, key=lambda item: item[1])
    effective = max(1, min(base, int(math.floor(base * ratio))))
    return ConcurrencyThrottleDecision(
        True, base, effective, ratio, provider,
        "every provider is over pace; limiting new background workers to the best remaining pace ratio",
    )
