"""Provider pacing governor (design by @raaj, refined by prime; kanban t_1eb32e10).

GOAL: never hit a provider's hard limit. Spend each subscription at a steady pace and
route NEW work to whichever provider has the most headroom, without ever disturbing a
session that is already running (the prompt cache is per-provider — a mid-session switch
re-writes the whole context at write price, so this module refuses to do it).

Design, matching the card exactly:

1. **Pace line.** A rolling usage window (Anthropic's ``five_hour``/``seven_day``, Codex's
   ``Session``/``Weekly``) is "on pace" when ``used% <= elapsed_fraction*100 + slack%``.
   Five-hour window: ~20%/h. Weekly window: ~14.3%/day — the one that actually binds, per
   the card, because it is the harder ceiling in practice.
2. **Session boundaries only.** This module never fetches usage or decides anything for a
   session that is already running — the only inputs to a routing decision are (a) the
   last-polled state file and (b) whether the caller is asking about a session that
   already exists (``sticky_provider``). A non-empty ``sticky_provider`` short-circuits
   every other input: the caller gets its own provider back, unconditionally. There is no
   code path in this module that can produce a "switch this running session" answer.
3. **Mid-session hard-limit switches are out of scope here** — that is the existing
   claude-subscription limit-notice classifier (aos#456, t_877c8f5e). This module answers
   exactly one question: which provider should the NEXT spawned worker use.
4. **Priority lanes.** ``reserved_lane=True`` (chat bots, prime) is paced against the full
   window (no reservation taken away from it); an ordinary kanban worker's effective cap is
   the pace line minus ``RESERVED_LANE_PCT`` — the last slice of every window is reserved
   for the priority lane and kanban work stops taking a provider before it gets there.
5. **Visibility.** ``write_state``/``read_state`` are the file the fleet activity panel
   (t_4cc3faac) and the dispatcher both read — one small JSON document, atomically written,
   never a shared writer lock.

**Fail-open, always.** A missing credential, a network hiccup, a stale/missing state file,
or an unparseable usage payload never blocks a spawn — every failure mode here falls
through to the caller's own default provider rather than raising or stalling dispatch.
The one thing this module refuses to fail open on is *reassigning a running session*: that
path (``sticky_provider``) has no failure mode because it never touches the network, the
state file, or a fetch at all.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

# ── Pace windows ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PaceWindow:
    """One rolling usage window this governor paces against."""

    name: str
    window_seconds: float
    slack_pct: float = 10.0


FIVE_HOUR_WINDOW = PaceWindow("five_hour", 5 * 3600, slack_pct=10.0)
WEEKLY_WINDOW = PaceWindow("weekly", 7 * 86400, slack_pct=10.0)

# The slice of every window reserved for chat bots / prime (rule 4). An ordinary kanban
# worker's effective pace cap is the pace line minus this reservation.
RESERVED_LANE_PCT = 15.0

# Providers without a truthful rolling-usage API are explicit free lanes, not
# broken metered providers.  Keep this policy next to the poller/router contract.
UNMETERED_FREE_PROVIDERS = frozenset({"nous", "nvidia"})
MAX_STATE_AGE_SECONDS = 15 * 60


def pace_allowed_pct(elapsed_seconds: float, window: PaceWindow) -> float:
    """Allowed utilization % at this point in ``window``: linear pace + slack, clamped [0, 100].

    ``elapsed_seconds`` is how far into the CURRENT rolling window we are (0 = window just
    reset, ``window.window_seconds`` = window about to reset).
    """
    if window.window_seconds <= 0:
        return 100.0
    elapsed_fraction = max(0.0, min(1.0, elapsed_seconds / window.window_seconds))
    return max(0.0, min(100.0, elapsed_fraction * 100.0 + window.slack_pct))


def elapsed_seconds_from_reset(window: PaceWindow, reset_at: Optional[float], now: float) -> float:
    """Seconds elapsed into ``window`` given its next ``reset_at`` (epoch seconds).

    A rolling quota window resets at a known clock time; how far *into* the window we are
    is ``window_seconds - seconds_until_reset``. When ``reset_at`` is unknown (provider
    didn't report it), fail open by treating the window as fully elapsed — i.e. impose no
    pacing constraint (the caller sees ``allowed=100%``) rather than guessing.
    """
    if reset_at is None:
        return window.window_seconds
    remaining = max(0.0, reset_at - now)
    return max(0.0, window.window_seconds - remaining)


# ── Usage snapshot (fetch layer) ────────────────────────────────────────────


@dataclass(frozen=True)
class WindowUsage:
    used_pct: float
    reset_at: Optional[float]  # epoch seconds, None when unknown


@dataclass(frozen=True)
class ProviderUsageSnapshot:
    """Raw usage as fetched from a provider, before pace evaluation."""

    provider: str
    five_hour: Optional[WindowUsage] = None
    weekly: Optional[WindowUsage] = None
    fetched_at: float = 0.0
    source: str = ""
    error: Optional[str] = None


# Provider chain name -> the key ``agent.account_usage.fetch_account_usage`` expects.
# Kept as a translation table (not identity) because the chain speaks the same provider
# ids the dispatcher's ``--provider`` flag does, which are not always the account-usage
# module's own keys (e.g. "codex" the runtime provider vs. "openai-codex" the usage key).
USAGE_PROVIDER_KEY = {
    "anthropic": "anthropic",
    "claude-subscription": "anthropic",
    "claude": "anthropic",
    "codex": "openai-codex",
    "openai-codex": "openai-codex",
}

# Window label matching is intentionally substring/order-based rather than an exact set:
# account_usage.py's own window order for anthropic is
# (five_hour, seven_day, seven_day_opus, seven_day_sonnet) labelled ("Current session",
# "Current week", "Opus week", "Sonnet week") — "first session match / first week match
# wins" picks "Current session"/"Current week" correctly without hardcoding provider-
# specific label strings twice. Codex's own labels are "Session"/"Weekly" and match the
# same substrings.
_FIVE_HOUR_LABEL_HINTS = ("session",)
_WEEKLY_LABEL_HINTS = ("week",)


DEFAULT_RATE_LIMIT_CACHE_PATH = Path(os.environ.get(
    "HERMES_PACING_RATE_LIMIT_CACHE", "~/.qwickapps/state/provider_pace_ratelimits.json")).expanduser()


def _read_json_doc(path: Path) -> dict:
    """Fail-open ``{}`` on anything (missing file, corrupt JSON) -- same contract as
    ``read_state``, just returning a plain dict for small sidecar caches."""
    try:
        loaded = json.loads(path.read_text())
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json_doc(path: Path, doc: dict) -> None:
    """Same atomic write-then-rename as ``write_state``, factored out for the small
    sidecar caches (rate-limit backoff, alert streaks) this module also persists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(doc, indent=2))
    tmp.replace(path)


def _retry_after_seconds(exc: BaseException) -> Optional[float]:
    """Best-effort ``Retry-After`` (seconds) off an httpx.HTTPStatusError's response, or
    None when the exception carries no such header (e.g. it isn't an HTTP error at all).
    Never raises -- diagnostics only."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    try:
        raw = headers.get("retry-after")
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _is_429(exc: BaseException) -> bool:
    return getattr(getattr(exc, "response", None), "status_code", None) == 429


def fetch_provider_usage(
    provider: str, *, now: Optional[float] = None,
    rate_limit_cache_path: Path = DEFAULT_RATE_LIMIT_CACHE_PATH,
) -> ProviderUsageSnapshot:
    """Fetch one provider's usage windows, reusing Hermes core's already-tested per-provider
    fetchers (same credential resolution as a live session, Anthropic OAuth
    ``/api/oauth/usage`` for Claude, Codex's own rate-limit headers for Codex) rather than
    re-implementing either fetch. Never raises: every failure mode (no credential, network
    error, unsupported provider, hermes core not importable) becomes ``error`` on the
    returned snapshot so a poll tick always has something to write, even when every
    provider is unreachable.

    Two refinements over calling the public ``agent.account_usage.fetch_account_usage()``
    wrapper directly (t_f7bb9382 -- claude-subscription usage reads were silently and
    permanently failing since the poller shipped):

    1. **Real diagnostics.** The public wrapper swallows EVERY exception (network error,
       HTTP 4xx/5xx, auth failure) into a bare ``None``, which this module could not tell
       apart from "no credential configured" -- the exact misleading message this card
       exists to fix. ``agent.account_usage._USAGE_FETCHERS`` is the same per-provider
       function the public wrapper calls, one level down, before that blanket ``except``;
       calling it directly (when hermes core still exposes it -- fails open to the public
       wrapper otherwise, e.g. in tests that only fake ``fetch_account_usage``) recovers the
       real exception message with the SAME single HTTP call the old code made, not an
       extra one. Root-caused empirically for claude-subscription: NOT a credential
       resolution problem (the pool/singleton token resolves fine) -- Anthropic's
       ``/api/oauth/usage`` endpoint itself was returning 429 (``Retry-After`` present), most
       likely from fleet-wide call volume against the one shared subscription token.
    2. **Rate-limit backoff.** A 429 response's ``Retry-After`` is cached per provider; a
       later tick that lands inside that window skips the HTTP call entirely instead of
       re-triggering the same 429 every 5 minutes, and reports an honest
       "rate limited, backing off" error instead of nothing. This can only help the upstream
       limit actually clear.
    """
    now = now if now is not None else time.time()
    usage_key = USAGE_PROVIDER_KEY.get(provider, provider)

    backoff_doc = _read_json_doc(rate_limit_cache_path)
    backoff_entry = backoff_doc.get(usage_key) if isinstance(backoff_doc, dict) else None
    if isinstance(backoff_entry, dict):
        until = backoff_entry.get("until")
        if isinstance(until, (int, float)) and until > now:
            return ProviderUsageSnapshot(
                provider=provider, fetched_at=now,
                error=(
                    f"skipping fetch: upstream rate limited this provider's usage endpoint "
                    f"until {until:.0f} ({max(0.0, until - now):.0f}s remaining) -- "
                    f"{backoff_entry.get('reason', 'no reason recorded')}"
                ),
            )

    try:
        import agent.account_usage as account_usage_module  # type: ignore[import-not-found]
    except ImportError as exc:
        return ProviderUsageSnapshot(provider=provider, fetched_at=now, error=f"hermes core unavailable: {exc}")

    direct_fetcher = getattr(account_usage_module, "_USAGE_FETCHERS", {}).get(usage_key)
    try:
        if direct_fetcher is not None:
            snapshot = direct_fetcher(None, None)
        else:
            snapshot = account_usage_module.fetch_account_usage(usage_key)
    except Exception as exc:  # noqa: BLE001 — a fetch failure must never crash the poller
        logger.warning("pacing governor: usage fetch failed for %s (%s)", provider, exc)
        if _is_429(exc):
            retry_after = _retry_after_seconds(exc) or 300.0
            backoff_doc[usage_key] = {
                "until": now + retry_after,
                "reason": f"429 rate_limit_error, Retry-After={retry_after:.0f}s (seen at {now:.0f})",
            }
            try:
                _write_json_doc(rate_limit_cache_path, backoff_doc)
            except OSError:
                logger.debug("pacing governor: failed to persist rate-limit backoff (non-fatal)", exc_info=True)
        # str(exc) on httpx.HTTPStatusError / RuntimeError / OSError never includes the
        # Authorization header or the token itself -- only status code, URL, and message.
        return ProviderUsageSnapshot(provider=provider, fetched_at=now, error=str(exc))
    if snapshot is None:
        return ProviderUsageSnapshot(
            provider=provider, fetched_at=now,
            error="no usage snapshot available (no credential configured or provider unsupported)",
        )
    five_hour: Optional[WindowUsage] = None
    weekly: Optional[WindowUsage] = None
    for window in getattr(snapshot, "windows", None) or ():
        label = str(getattr(window, "label", "") or "").strip().lower()
        used_percent = getattr(window, "used_percent", None)
        if used_percent is None:
            continue
        reset_at = getattr(window, "reset_at", None)
        reset_epoch = reset_at.timestamp() if reset_at is not None else None
        wu = WindowUsage(used_pct=float(used_percent), reset_at=reset_epoch)
        if five_hour is None and any(h in label for h in _FIVE_HOUR_LABEL_HINTS):
            five_hour = wu
        elif weekly is None and any(h in label for h in _WEEKLY_LABEL_HINTS):
            weekly = wu
    return ProviderUsageSnapshot(
        provider=provider, five_hour=five_hour, weekly=weekly, fetched_at=now,
        source=str(getattr(snapshot, "source", "") or ""),
    )


# ── Pace evaluation ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderPaceState:
    """One provider's usage vs. its pace line, ready to route on or display."""

    provider: str
    five_hour_used_pct: Optional[float] = None
    five_hour_allowed_pct: Optional[float] = None
    weekly_used_pct: Optional[float] = None
    weekly_allowed_pct: Optional[float] = None
    fetched_at: float = 0.0
    error: Optional[str] = None
    metered: bool = True
    free: bool = False

    def _over(self, used: Optional[float], allowed: Optional[float], *, reserve_pct: float) -> bool:
        if used is None or allowed is None:
            return False  # unknown -> never blocks; decide_next_provider treats unknown separately
        cap = max(0.0, min(allowed, 100.0 - reserve_pct))
        return used > cap

    def over_pace(self, *, reserved_lane: bool = False) -> bool:
        """True when EITHER window is over its (lane-adjusted) pace line. The weekly window
        is checked even though the five-hour window is usually what trips first, because the
        card is explicit that weekly is "the one that actually binds" in practice."""
        reserve = 0.0 if reserved_lane else RESERVED_LANE_PCT
        return (
            self._over(self.five_hour_used_pct, self.five_hour_allowed_pct, reserve_pct=reserve)
            or self._over(self.weekly_used_pct, self.weekly_allowed_pct, reserve_pct=reserve)
        )

    def has_data(self) -> bool:
        return self.metered and self.error is None and (self.five_hour_used_pct is not None or self.weekly_used_pct is not None)


def evaluate_pace_state(snapshot: ProviderUsageSnapshot, *, now: Optional[float] = None) -> ProviderPaceState:
    """Turn a raw usage snapshot into a pace-line comparison. Pure function of its inputs —
    no I/O — so it is exercised directly by unit tests without a network or a clock mock
    beyond passing ``now`` explicitly."""
    now = now if now is not None else time.time()
    five_hour_used = five_hour_allowed = None
    if snapshot.five_hour is not None:
        five_hour_used = snapshot.five_hour.used_pct
        elapsed = elapsed_seconds_from_reset(FIVE_HOUR_WINDOW, snapshot.five_hour.reset_at, now)
        five_hour_allowed = pace_allowed_pct(elapsed, FIVE_HOUR_WINDOW)
    weekly_used = weekly_allowed = None
    if snapshot.weekly is not None:
        weekly_used = snapshot.weekly.used_pct
        elapsed = elapsed_seconds_from_reset(WEEKLY_WINDOW, snapshot.weekly.reset_at, now)
        weekly_allowed = pace_allowed_pct(elapsed, WEEKLY_WINDOW)
    return ProviderPaceState(
        provider=snapshot.provider,
        five_hour_used_pct=five_hour_used, five_hour_allowed_pct=five_hour_allowed,
        weekly_used_pct=weekly_used, weekly_allowed_pct=weekly_allowed,
        fetched_at=now, error=snapshot.error,
    )


# ── Routing decision ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RoutingDecision:
    provider: Optional[str]
    reason: str
    decided_at: float
    over_pace: Mapping[str, bool] = field(default_factory=dict)
    sticky: bool = False
    candidates: tuple[str, ...] = ()
    fallback: bool = False


def decide_next_provider(
    chain: Sequence[str],
    states: Mapping[str, ProviderPaceState],
    *,
    sticky_provider: Optional[str] = None,
    reserved_lane: bool = False,
    now: Optional[float] = None,
) -> RoutingDecision:
    """Which provider a NEW worker/session about to be spawned should use.

    ``sticky_provider`` short-circuits everything else (rule 2/3): a session that already
    exists keeps its provider, full stop — this function never even looks at ``states`` in
    that branch, so there is no over-pace input that can force a running session to move.

    Otherwise walks ``chain`` in order and returns the first provider that is within its
    (lane-adjusted) pace line. If every provider in the chain is over pace, or has no usable
    data, fails open to ``chain[0]`` rather than refusing to route at all — a governor outage
    or an unreachable usage endpoint must never block dispatch.
    """
    now = now if now is not None else time.time()
    if sticky_provider:
        return RoutingDecision(
            provider=sticky_provider,
            reason="sticky: session already running keeps its provider (prompt cache is per-provider; "
                   "mid-session switch only happens on a hard limit, handled separately)",
            decided_at=now, over_pace={}, sticky=True,
        )
    over_pace: dict[str, bool] = {}
    for provider in chain:
        state = states.get(provider)
        over_pace[provider] = bool(state and state.has_data() and state.over_pace(reserved_lane=reserved_lane))
    for provider in chain:
        state = states.get(provider)
        if state and not state.metered and state.free:
            return RoutingDecision(provider=provider, reason=f"{provider} is an unmetered free lane", decided_at=now, over_pace=over_pace, candidates=tuple(chain), fallback=provider != (chain[0] if chain else None))
        if state is None or not state.has_data():
            continue  # no usable reading for this provider; skip rather than trust a blank
        if not over_pace[provider]:
            return RoutingDecision(
                provider=provider,
                reason=(
                    f"{provider} within pace "
                    f"(5h {state.five_hour_used_pct}% <= {state.five_hour_allowed_pct}%, "
                    f"weekly {state.weekly_used_pct}% <= {state.weekly_allowed_pct}%)"
                ),
                decided_at=now, over_pace=over_pace,
                candidates=tuple(chain), fallback=provider != (chain[0] if chain else None),
            )
    default_provider = chain[0] if chain else None
    return RoutingDecision(
        provider=default_provider,
        reason=f"no candidate within pace; fail-open to {default_provider}",
        decided_at=now, over_pace=over_pace,
        candidates=tuple(chain), fallback=True,
    )


# ── State file + decision log (dispatcher/fleet-panel visibility) ───────────

DEFAULT_STATE_PATH = Path(os.environ.get(
    "HERMES_PACING_STATE_PATH", "~/.qwickapps/state/provider_pace.json")).expanduser()
DEFAULT_DECISION_LOG_PATH = Path(os.environ.get(
    "HERMES_PACING_DECISION_LOG", "~/.qwickapps/state/provider_pace_decisions.jsonl")).expanduser()
DEFAULT_COOLDOWN_SECONDS = float(os.environ.get("HERMES_PACING_COOLDOWN_SECONDS", "600"))
DEFAULT_COOLDOWN_STATE_PATH = Path(os.environ.get(
    "HERMES_PACING_COOLDOWN_STATE_PATH", "~/.qwickapps/state/provider_pace_cooldown.json")
).expanduser()
DEFAULT_CHAIN: tuple[str, ...] = tuple(
    p.strip() for p in os.environ.get("HERMES_PACING_CHAIN", "anthropic,codex").split(",") if p.strip()
)


def write_state(path: Path, states: Mapping[str, ProviderPaceState], *, chain: Sequence[str]) -> None:
    """Atomically write the small JSON state file the dispatcher and the fleet activity
    panel (t_4cc3faac) both read. Write-then-rename so a reader never observes a partial
    file, and a crash mid-write never corrupts the last-good state."""
    doc = {
        "generated_at": time.time(),
        "chain": list(chain),
        "reserved_lane_pct": RESERVED_LANE_PCT,
        "providers": {name: asdict(state) for name, state in states.items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(doc, indent=2))
    tmp.replace(path)


def read_state(path: Path) -> Optional[dict]:
    """Read the state file written by ``write_state``. Fail-open ``None`` on anything
    (missing file, concurrent partial write somehow surviving the rename, corrupt JSON) —
    callers must treat that exactly like "no governor data yet"."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _states_from_doc(doc: dict, *, now: Optional[float] = None) -> dict[str, ProviderPaceState]:
    """Decode the persisted provider states, discarding stale metered readings.

    The governor must never treat an old usage reading as current headroom.  An explicit
    unmetered/free lane has no usage API by definition and remains eligible regardless of its
    timestamp; every metered state needs a numeric ``fetched_at`` no older than 15 minutes.
    """
    now = now if now is not None else time.time()
    out: dict[str, ProviderPaceState] = {}
    document_timestamp = doc.get("generated_at")
    for name, raw in (doc.get("providers") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        try:
            values = {k: raw[k] for k in ProviderPaceState.__dataclass_fields__ if k in raw}
            values["provider"] = str(raw.get("provider") or name)
            # Pre-canonical state documents had no timestamp.  Treat those documents as fresh
            # on read for backward compatibility; timestamped documents retain the strict
            # 15-minute staleness contract above.
            if values.get("fetched_at") in (None, 0) and not isinstance(document_timestamp, (int, float)):
                values["fetched_at"] = now
            elif values.get("fetched_at") in (None, 0):
                values["fetched_at"] = document_timestamp
            state = ProviderPaceState(**values)  # type: ignore[arg-type]
        except TypeError:
            continue
        if state.metered and (
            not isinstance(state.fetched_at, (int, float))
            or now - state.fetched_at > MAX_STATE_AGE_SECONDS
        ):
            continue
        out[name] = state
    return out


def append_decision_log(path: Path, decision: RoutingDecision, *, context: str = "") -> None:
    """Append one JSON line per routing decision — the durable "logged decision" the
    acceptance criteria asks for. Never truncates or rewrites; a reader tails it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "decided_at": decision.decided_at,
        "provider": decision.provider,
        "reason": decision.reason,
        "over_pace": dict(decision.over_pace),
        "sticky": decision.sticky,
        "candidates": list(decision.candidates),
        "fallback": decision.fallback,
        "context": context,
    }
    with path.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")


# ── Missing-data alerting (sustained failure only; a single blip must never alert) ─────
#
# The parent card (t_e5ed5694) is explicit: missing/stale Claude data must ALERT, not
# silently default to the primary provider forever. ``decide_next_provider``'s fail-open
# design is correct and stays untouched (a governor outage must never block dispatch) --
# this section adds VISIBILITY on top of it, entirely out-of-band from the routing
# decision itself.

DEFAULT_ALERT_STATE_PATH = Path(os.environ.get(
    "HERMES_PACING_ALERT_STATE_PATH", "~/.qwickapps/state/provider_pace_alerts.json")).expanduser()

# Both the tick-count AND the wall-clock floor must be crossed together: at the normal
# 5-minute poll interval, ``missing_since`` is stamped on the FIRST missing tick, so the
# 6th consecutive miss is 5 intervals (25 minutes) later -- these two constants are picked
# to agree at that point. The minutes floor also protects against a burst of manual/backfill
# ticks (e.g. this card's own diagnosis run) counting as "sustained" just because there were
# many of them within a few seconds.
ALERT_AFTER_CONSECUTIVE_TICKS = 6
ALERT_AFTER_MINUTES = 25.0
# Once alerted, don't re-alert on every subsequent tick while the outage continues -- but
# don't go silent forever either if the first alert goes unactioned.
ALERT_RENOTIFY_MINUTES = 24 * 60.0


@dataclass(frozen=True)
class AlertProviderState:
    """Persisted per-provider streak feeding the missing-data alert decision."""

    consecutive_missing: int = 0
    missing_since: Optional[float] = None
    last_alerted_at: Optional[float] = None


def evaluate_missing_data_alert(
    has_data: bool,
    prior: AlertProviderState,
    *,
    now: float,
    consecutive_threshold: int = ALERT_AFTER_CONSECUTIVE_TICKS,
    minutes_threshold: float = ALERT_AFTER_MINUTES,
    renotify_minutes: float = ALERT_RENOTIFY_MINUTES,
) -> tuple[AlertProviderState, bool]:
    """Pure decision, no I/O: given whether THIS tick had usable data and the persisted
    streak, return ``(new_state, should_alert_now)``. Exercised directly by unit tests with
    synthetic ticks and an explicit clock -- no filesystem, no network, no subprocess.

    A single missing tick never alerts (both thresholds gate it). Once genuinely sustained,
    alerts once, then waits ``renotify_minutes`` before alerting again if the condition
    still has not cleared. A tick that DOES have data resets the streak unconditionally.
    """
    if has_data:
        return AlertProviderState(), False
    consecutive = prior.consecutive_missing + 1
    missing_since = prior.missing_since if prior.missing_since is not None else now
    elapsed_minutes = (now - missing_since) / 60.0
    sustained = consecutive >= consecutive_threshold and elapsed_minutes >= minutes_threshold
    should_alert = False
    last_alerted_at = prior.last_alerted_at
    if sustained and (last_alerted_at is None or (now - last_alerted_at) / 60.0 >= renotify_minutes):
        should_alert = True
        last_alerted_at = now
    return (
        AlertProviderState(consecutive_missing=consecutive, missing_since=missing_since, last_alerted_at=last_alerted_at),
        should_alert,
    )


def _load_alert_states(path: Path) -> dict[str, AlertProviderState]:
    doc = _read_json_doc(path)
    out: dict[str, AlertProviderState] = {}
    for name, raw in doc.items():
        if isinstance(raw, dict):
            try:
                out[name] = AlertProviderState(
                    consecutive_missing=int(raw.get("consecutive_missing") or 0),
                    missing_since=raw.get("missing_since"),
                    last_alerted_at=raw.get("last_alerted_at"),
                )
            except (TypeError, ValueError):
                continue
    return out


def _save_alert_states(path: Path, states: Mapping[str, AlertProviderState]) -> None:
    _write_json_doc(path, {name: asdict(state) for name, state in states.items()})


def _create_kanban_alert(provider: str, info: dict) -> None:
    """Real side effect: file a visible kanban card instead of silently defaulting forever
    (the parent card's explicit, non-negotiable requirement). Never raises -- an alerting
    failure must not take down the poll tick that calls it; subprocess errors are logged
    and swallowed. Idempotency-keyed per provider per UTC day so a sustained multi-day
    outage still gets a fresh card daily (visible, not spammy) instead of one card raised
    once and then silently ignored for the rest of the outage."""
    import subprocess
    from datetime import datetime, timezone

    day = datetime.fromtimestamp(info["now"], tz=timezone.utc).strftime("%Y-%m-%d")
    idem = f"pacing-governor-missing-data-{provider}-{day}"
    title = f"ALERT: {provider} usage data missing from provider pacing governor ({day})"
    body = (
        f"Provider pacing governor (t_1eb32e10 / t_349dad5a / t_f7bb9382) has had no usable "
        f"usage reading for `{provider}` for {info['consecutive']} consecutive poll ticks "
        f"({info['elapsed_minutes']:.0f} minutes), since {info['missing_since_iso']}.\n\n"
        f"Last recorded error: {info.get('last_error') or '(none recorded)'}\n\n"
        f"Every routing decision during this window has fallen open to the primary provider "
        f"(fail-open by design) -- this card exists because that must not happen silently "
        f"forever. Check `~/.qwickapps/state/provider_pace.json` and "
        f"`~/.qwickapps/state/provider_pace_ratelimits.json` for current state.\n\n"
        f"Never log or paste the OAuth token value while investigating."
    )
    try:
        subprocess.run(
            [
                "hermes", "kanban", "create", title,
                "--body", body, "--assignee", "anika",
                "--idempotency-key", idem, "--priority", "5",
            ],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except Exception:
        logger.exception("pacing governor: failed to file missing-data alert kanban card (non-fatal)")


def update_missing_data_alerts(
    states: Mapping[str, ProviderPaceState],
    *,
    providers: Sequence[str],
    alert_state_path: Path = DEFAULT_ALERT_STATE_PATH,
    now: Optional[float] = None,
    create_alert: Callable[[str, dict], None] = _create_kanban_alert,
) -> None:
    """I/O wrapper around ``evaluate_missing_data_alert``: loads the persisted per-provider
    streak, updates it for this tick, saves it back, and fires ``create_alert`` for any
    provider that just crossed the sustained-failure threshold. Never raises -- a broken
    alert path must not break the poll tick that calls it."""
    from datetime import datetime, timezone

    now = now if now is not None else time.time()
    try:
        prior_states = _load_alert_states(alert_state_path)
        new_states: dict[str, AlertProviderState] = {}
        for provider in providers:
            state = states.get(provider)
            has_data = bool(state and (not state.metered or state.has_data()))
            new_state, should_alert = evaluate_missing_data_alert(
                has_data, prior_states.get(provider, AlertProviderState()), now=now,
            )
            new_states[provider] = new_state
            if should_alert:
                elapsed_minutes = (now - new_state.missing_since) / 60.0 if new_state.missing_since else 0.0
                missing_since_iso = (
                    datetime.fromtimestamp(new_state.missing_since, tz=timezone.utc).isoformat()
                    if new_state.missing_since else "unknown"
                )
                create_alert(provider, {
                    "now": now, "consecutive": new_state.consecutive_missing,
                    "missing_since_iso": missing_since_iso, "elapsed_minutes": elapsed_minutes,
                    "last_error": state.error if state else None,
                })
        _save_alert_states(alert_state_path, new_states)
    except Exception:  # noqa: BLE001 — alerting must never break the poll tick
        logger.exception("pacing governor: missing-data alert update failed (non-fatal)")


# ── Poll cycle (the 5-minute tick) ──────────────────────────────────────────


def poll_once(
    chain: Sequence[str] = DEFAULT_CHAIN,
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    decision_log_path: Optional[Path] = DEFAULT_DECISION_LOG_PATH,
    fetch: Callable[..., ProviderUsageSnapshot] = fetch_provider_usage,
    now: Optional[float] = None,
    alert_state_path: Path = DEFAULT_ALERT_STATE_PATH,
    alert_providers: Optional[Sequence[str]] = None,
    create_alert: Callable[[str, dict], None] = _create_kanban_alert,
) -> RoutingDecision:
    """One poll tick: fetch every provider's usage, evaluate pace, write the state file,
    log a routing decision for visibility (this is NOT what a dispatcher calls at spawn
    time — that's ``resolve_provider_for_new_spawn``, which only reads the state file this
    writes and never fetches over the network itself). Also updates the sustained
    missing-data alert streak for every provider in ``chain`` (or ``alert_providers`` when
    given) and files a kanban card the first time any of them crosses the threshold."""
    now = now if now is not None else time.time()
    states: dict[str, ProviderPaceState] = {}
    for provider in chain:
        if provider in UNMETERED_FREE_PROVIDERS or provider.endswith(":free"):
            states[provider] = ProviderPaceState(provider=provider, fetched_at=now, metered=False, free=True)
        else:
            snapshot = fetch(provider, now=now)
            states[provider] = evaluate_pace_state(snapshot, now=now)
    write_state(state_path, states, chain=chain)
    decision = decide_next_provider(chain, states, now=now)
    if decision_log_path is not None:
        append_decision_log(decision_log_path, decision, context="poll")
    update_missing_data_alerts(
        states, providers=alert_providers if alert_providers is not None else chain,
        alert_state_path=alert_state_path, now=now, create_alert=create_alert,
    )
    return decision


def poll_loop(
    *, interval: float = 300.0, chain: Sequence[str] = DEFAULT_CHAIN,
    state_path: Path = DEFAULT_STATE_PATH, decision_log_path: Optional[Path] = DEFAULT_DECISION_LOG_PATH,
    sleep: Callable[[float], None] = time.sleep,
    max_iterations: Optional[int] = None,
) -> None:
    """Poll every ``interval`` seconds (default 5 min, per the card) until interrupted.
    ``max_iterations`` exists only for tests — production callers never pass it."""
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        try:
            poll_once(chain, state_path=state_path, decision_log_path=decision_log_path)
        except Exception:  # noqa: BLE001 — one bad tick must never kill the poller loop
            logger.exception("pacing governor: poll tick failed, will retry next interval")
        iterations += 1
        if max_iterations is None or iterations < max_iterations:
            sleep(interval)


# ── Dispatcher integration point ────────────────────────────────────────────


def resolve_provider_for_new_spawn(
    default_provider: str,
    *,
    chain: Optional[Sequence[str]] = None,
    state_path: Path = DEFAULT_STATE_PATH,
    sticky_provider: Optional[str] = None,
    reserved_lane: bool = False,
    decision_log_path: Optional[Path] = DEFAULT_DECISION_LOG_PATH,
    context: str = "",
    candidates: Optional[Sequence[str]] = None,
) -> str:
    """The one call a dispatcher needs, at exactly the point it is about to spawn a NEW
    worker/session (never for a session that already exists — pass its provider as
    ``sticky_provider`` instead and this returns it unchanged without touching the state
    file at all).

    Reads only the state file the poller last wrote — never fetches usage itself — so it
    is cheap and safe to call on every single spawn. Fails open to ``default_provider``
    whenever the state file is missing, stale-looking, or unreadable, or the chain is
    empty: a governor outage must never be able to block dispatch.

    Integration point for ``hermes_cli/kanban_db_dispatch.py``: call this immediately
    before building a NEW worker's argv (never for a resumed/still-running task), passing
    the task's already-configured provider as ``default_provider`` and leaving
    ``sticky_provider`` unset only when the task has no session yet.
    """
    if sticky_provider:
        return sticky_provider
    doc = read_state(state_path)
    if not doc:
        return default_provider
    effective_chain = list(candidates) if candidates else (list(chain) if chain else list(doc.get("chain") or [default_provider]))
    if default_provider not in effective_chain:
        effective_chain.insert(0, default_provider)
    states = _states_from_doc(doc)
    decision = decide_next_provider(effective_chain, states, reserved_lane=reserved_lane)
    if decision_log_path is not None:
        try:
            append_decision_log(decision_log_path, decision, context=context or "spawn")
        except OSError:
            logger.debug("pacing governor: decision log append failed (non-fatal)", exc_info=True)
    return decision.provider or default_provider


@dataclass(frozen=True)
class CooldownRecord:
    provider: str
    decided_at: float


def read_cooldown_state(path: Path) -> dict[str, CooldownRecord]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    records: dict[str, CooldownRecord] = {}
    for key, entry in (raw or {}).items():
        try:
            records[key] = CooldownRecord(provider=str(entry["provider"]), decided_at=float(entry["decided_at"]))
        except (KeyError, TypeError, ValueError):
            continue
    return records


def write_cooldown_state(path: Path, records: Mapping[str, CooldownRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps({key: asdict(record) for key, record in records.items()}, indent=2))
    tmp.replace(path)


def _apply_cooldown(
    key: str, decision: RoutingDecision, *, last_record: Optional[CooldownRecord],
    cooldown_seconds: float, now: float,
) -> RoutingDecision:
    if decision.sticky or last_record is None or now - last_record.decided_at >= cooldown_seconds:
        return decision
    if last_record.provider == decision.provider:
        return decision
    remaining = cooldown_seconds - (now - last_record.decided_at)
    return RoutingDecision(
        provider=last_record.provider,
        reason=f"cooldown: holding {key!r} at {last_record.provider} for {remaining:.0f}s more",
        decided_at=now,
        over_pace=decision.over_pace,
        candidates=decision.candidates,
        fallback=last_record.provider != (decision.candidates[0] if decision.candidates else None),
    )


# ── CLI ───────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aos-pacing-governor",
        description="Provider pacing governor: poll usage, pace against the window, route new spawns.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    once = sub.add_parser("poll-once", help="Fetch usage, write state, log one decision, then exit")
    once.add_argument("--chain", default=",".join(DEFAULT_CHAIN))

    loop = sub.add_parser("poll-loop", help="Poll forever at --interval seconds (default 300 = 5 min)")
    loop.add_argument("--interval", type=float, default=300.0)
    loop.add_argument("--chain", default=",".join(DEFAULT_CHAIN))
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    chain = tuple(p.strip() for p in args.chain.split(",") if p.strip())
    if args.cmd == "poll-once":
        decision = poll_once(chain)
        print(json.dumps(asdict(decision), default=str, indent=2))
        return 0
    if args.cmd == "poll-loop":
        poll_loop(interval=args.interval, chain=chain)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())


def resolve_provider_for_new_spawn_vendored(
    default_provider: str,
    *,
    chain: Optional[Sequence[str]] = None,
    state_path: Path = DEFAULT_STATE_PATH,
    sticky_provider: Optional[str] = None,
    reserved_lane: bool = False,
    decision_log_path: Optional[Path] = DEFAULT_DECISION_LOG_PATH,
    context: str = "",
    candidates: Optional[Sequence[str]] = None,
    cooldown_key: Optional[str] = None,
    cooldown_state_path: Path = DEFAULT_COOLDOWN_STATE_PATH,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    now: Optional[float] = None,
) -> str:
    """Canonical routing with the legacy per-profile cooldown contract preserved."""
    if sticky_provider:
        return sticky_provider
    doc = read_state(state_path)
    if not doc:
        return default_provider
    now = now if now is not None else time.time()
    effective_chain = list(candidates) if candidates else (list(chain) if chain else list(doc.get("chain") or [default_provider]))
    if default_provider not in effective_chain:
        effective_chain.insert(0, default_provider)
    decision = decide_next_provider(
        effective_chain, _states_from_doc(doc, now=now), reserved_lane=reserved_lane, now=now,
    )
    records = read_cooldown_state(cooldown_state_path) if cooldown_key else {}
    if cooldown_key:
        decision = _apply_cooldown(
            cooldown_key, decision, last_record=records.get(cooldown_key),
            cooldown_seconds=cooldown_seconds, now=now,
        )
        if decision.provider:
            records[cooldown_key] = CooldownRecord(provider=decision.provider, decided_at=now)
            try:
                write_cooldown_state(cooldown_state_path, records)
            except OSError:
                logger.debug("pacing governor: cooldown state write failed (non-fatal)", exc_info=True)
    if decision_log_path is not None:
        try:
            append_decision_log(decision_log_path, decision, context=context or "spawn")
        except OSError:
            logger.debug("pacing governor: decision log append failed (non-fatal)", exc_info=True)
    return decision.provider or default_provider


# --- Hard rate-limit holds (t_5b853ddb) ----------------------------------------------------
# The pace state above is a forecast; this is the provider's own "you are out until T" wall.
# One record per provider, written by whichever process first hits the wall
# (``agent.fallback_cooldown._arm_rate_limit_cooldown``) and read by the kanban dispatcher, which
# stops spawning workers whose primary provider is held instead of letting every worker fail over
# to the fallback subscription. Interactive sessions never read it; they keep failing over.


def rate_limit_state_path() -> Path:
    """``<hermes root>/state/provider_rate_limits.json``, resolved at call time. Anchored on the
    Hermes root (shared by every profile's workers and the dispatcher, like ``kanban.db``) rather
    than ``~/.qwickapps``: the test suite isolates ``HERMES_HOME`` but not ``HOME``, so a
    HOME-anchored path would let a test hold the live board."""
    override = os.environ.get("HERMES_PACING_RATE_LIMIT_STATE_PATH")
    if override:
        return Path(override).expanduser()
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root() / "state" / "provider_rate_limits.json"


def read_rate_limit_holds(path: Optional[Path] = None) -> dict[str, dict]:
    """Fail-open ``{}``: an unreadable hold file means "no hold", never a blocked dispatch."""
    try:
        raw = json.loads((path or rate_limit_state_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in (raw or {}).items() if isinstance(v, dict) and "until" in v}


def record_rate_limit(
    provider: str, until: float, *, source: str = "", path: Optional[Path] = None,
    now: Optional[float] = None,
) -> Optional[float]:
    """Record that ``provider`` is walled until epoch ``until``; returns the stored ``until``.

    Merges under an exclusive lock and never shortens a later hold another process already
    stored (a second worker hitting the same wall must not overwrite a longer weekly reset
    with a short retry-after). Expired holds are pruned on write."""
    import fcntl

    provider = (provider or "").strip().lower()
    if not provider or not until:
        return None
    now = now if now is not None else time.time()
    path = path or rate_limit_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(path.suffix + ".lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        holds = {k: v for k, v in read_rate_limit_holds(path).items() if float(v.get("until") or 0) > now}
        prior = holds.get(provider)
        if prior and float(prior["until"]) >= until:
            return float(prior["until"])
        holds[provider] = {"until": float(until), "recorded_at": now, "source": source}
        tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(holds, indent=2))
        tmp.replace(path)
    return float(until)


def lane_hold_key(provider: Optional[str], model: Optional[str]) -> Optional[str]:
    """Canonical rate-limit key for one provider/model lane."""
    provider = (provider or "").strip().lower()
    model = (model or "").strip().lower()
    return f"{provider}::{model}" if provider and model else None


def _hold_until(key: Optional[str], *, path: Optional[Path] = None,
                now: Optional[float] = None) -> Optional[float]:
    if not key:
        return None
    entry = read_rate_limit_holds(path).get(key)
    if not entry:
        return None
    try:
        until = float(entry["until"])
    except (TypeError, ValueError):
        return None
    now = now if now is not None else time.time()
    return until if until > now else None


def rate_limit_hold_until(provider: Optional[str], *, path: Optional[Path] = None,
                          now: Optional[float] = None) -> Optional[float]:
    """Epoch until which a provider-wide wall is active, or None when free/unknown."""
    return _hold_until((provider or "").strip().lower(), path=path, now=now)


def lane_rate_limit_hold_until(provider: Optional[str], model: Optional[str], *,
                               path: Optional[Path] = None,
                               now: Optional[float] = None) -> Optional[float]:
    """Epoch until which a specific provider/model lane is held."""
    return _hold_until(lane_hold_key(provider, model), path=path, now=now)


def record_lane_rate_limit(
    provider: str, model: str, until: float, *, reason: str, source: str = "lane-health",
    path: Optional[Path] = None, now: Optional[float] = None,
) -> Optional[float]:
    """Record a model-specific hold without changing provider-wide wall semantics.

    The shared state file intentionally contains both bare provider keys (hard quota walls) and
    ``provider::model`` keys (a lane is unhealthy).  A later existing hold always wins.
    """
    import fcntl

    key = lane_hold_key(provider, model)
    if not key or not until:
        return None
    now = now if now is not None else time.time()
    path = path or rate_limit_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(path.suffix + ".lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        holds = {k: v for k, v in read_rate_limit_holds(path).items()
                 if float(v.get("until") or 0) > now}
        prior = holds.get(key)
        if prior and float(prior["until"]) >= until:
            return float(prior["until"])
        holds[key] = {
            "until": float(until), "recorded_at": now, "source": source,
            "provider": provider.strip().lower(), "model": model.strip().lower(), "reason": reason,
        }
        tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(holds, indent=2))
        tmp.replace(path)
    return float(until)


def clear_lane_health_holds(*, path: Optional[Path] = None, now: Optional[float] = None) -> None:
    """Lift only expired or lane-health-owned lane holds; never touch quota/budget walls."""
    import fcntl

    now = now if now is not None else time.time()
    path = path or rate_limit_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(path.suffix + ".lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        holds = read_rate_limit_holds(path)
        kept = {
            key: value for key, value in holds.items()
            if float(value.get("until") or 0) > now
            and not ("::" in key and str(value.get("source") or "").startswith("lane-health"))
        }
        if kept != holds:
            tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
            tmp.write_text(json.dumps(kept, indent=2))
            tmp.replace(path)
