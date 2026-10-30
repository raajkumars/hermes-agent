"""Embedded kanban dispatcher: settings resolution and per-tick board work.

``GatewayKanbanWatchersMixin._kanban_dispatcher_watcher`` owns the loop,
the singleton lock and the health telemetry; everything that only needs the
``kanban_db`` module and the resolved settings lives here.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from gateway.kanban_watchers_common import _board_slugs, _positive_int_setting, logger


def _kbc():
    from hermes_cli import kanban_db_connect
    return kanban_db_connect


def _kbd():
    from hermes_cli import kanban_db_dispatch
    return kanban_db_dispatch

_CORRUPT_DB_MARKERS = ("file is not a database", "database disk image is malformed")


@dataclass
class _DispatcherSettings:
    """``kanban.*`` dispatch settings, read once at boot (restart to apply).

    Exception: ``max_in_progress`` is overwritten on ``self`` every tick by
    ``gateway.kanban_watchers._kanban_dispatcher_watcher`` via
    ``_resolve_live_max_in_progress`` (t_62f24f45) — the boot-resolved value here
    only seeds the very first tick before that live re-read runs. Every other
    field is genuinely boot-only; flip one live only with the same care given
    to the auto-decompose toggle (#49638): a safety knob an operator expects to
    take effect on the next tick, not after a restart.
    """

    interval: float
    max_spawn: Any
    max_in_progress: Optional[int]
    failure_limit: int
    max_spawns_without_completion: int
    stale_timeout_seconds: int
    reconcile_orphans: bool
    default_assignee: Optional[str]
    max_in_progress_per_profile: Optional[int]
    stale_waiting_timeout_seconds: int = 0
    stale_waiting_escalation_limit: int = 0
    stale_waiting_escalation_manager_map: Optional[dict] = None
    stale_waiting_escalation_manager: Optional[str] = None
    pr_review_wakeup_enabled: bool = False


def _resolve_dispatcher_settings(kanban_cfg: dict, kb: Any) -> _DispatcherSettings:
    """Parse and log the dispatcher settings in their established order."""
    try:
        interval = float(kanban_cfg.get("dispatch_interval_seconds", 60) or 60)
    except (ValueError, TypeError):
        logger.warning("kanban dispatcher: invalid dispatch_interval_seconds=%r, using default 60",
                       kanban_cfg.get("dispatch_interval_seconds"))
        interval = 60.0
    interval = max(interval, 1.0)  # sanity floor — tighter than this is a footgun

    max_spawn = kanban_cfg.get("max_spawn")
    if max_spawn is not None:
        logger.info("kanban dispatcher: max_spawn=%s", max_spawn)

    # Cap simultaneously running tasks so slow workers don't pile up and time
    # out. Explicit config wins; otherwise a memory-derived default (unbounded
    # fan-out swap-thrashes small hosts), or None where total memory can't be read.
    max_in_progress = _positive_int_setting(kanban_cfg, "max_in_progress")
    effective_max_in_progress = _kbd().resolve_max_in_progress(max_in_progress)
    if max_in_progress is None and effective_max_in_progress is not None:
        logger.info(
            "kanban dispatcher: kanban.max_in_progress unset; using "
            "memory-derived default max_in_progress=%d "
            "(set kanban.max_in_progress in config.yaml to override)",
            effective_max_in_progress,
        )

    raw_failure_limit = kanban_cfg.get("failure_limit", kb.DEFAULT_FAILURE_LIMIT)
    try:
        failure_limit = int(raw_failure_limit)
    except (TypeError, ValueError):
        logger.warning("kanban dispatcher: invalid kanban.failure_limit=%r; using default %d",
                       raw_failure_limit, kb.DEFAULT_FAILURE_LIMIT)
        failure_limit = kb.DEFAULT_FAILURE_LIMIT
    if failure_limit < 1:
        logger.warning("kanban dispatcher: kanban.failure_limit=%r is below 1; using default %d",
                       raw_failure_limit, kb.DEFAULT_FAILURE_LIMIT)
        failure_limit = kb.DEFAULT_FAILURE_LIMIT

    raw_spawn_cap = kanban_cfg.get("max_spawns_without_completion", 20)
    try:
        # ``bool`` is an ``int`` subclass, but a true/false retry budget is
        # almost certainly a configuration mistake.
        if isinstance(raw_spawn_cap, bool) or (
            isinstance(raw_spawn_cap, float) and not raw_spawn_cap.is_integer()
        ):
            raise ValueError
        max_spawns_without_completion = int(raw_spawn_cap)
    except (TypeError, ValueError):
        logger.warning(
            "kanban dispatcher: invalid kanban.max_spawns_without_completion=%r; "
            "using default 20",
            raw_spawn_cap,
        )
        max_spawns_without_completion = 20
    if max_spawns_without_completion < 0:
        logger.warning(
            "kanban dispatcher: kanban.max_spawns_without_completion=%r is below 0; "
            "using default 20",
            raw_spawn_cap,
        )
        max_spawns_without_completion = 20

    # 0 disables stale detection.
    raw_stale = kanban_cfg.get("dispatch_stale_timeout_seconds", 0)
    try:
        stale_timeout_seconds = int(raw_stale or 0)
    except (TypeError, ValueError):
        logger.warning("kanban dispatcher: invalid kanban.dispatch_stale_timeout_seconds=%r; "
                       "disabling stale detection", raw_stale)
        stale_timeout_seconds = 0

    # 0 disables the review/blocked staleness escalation (fm #37 class of
    # incident: reviewed-but-nobody-merged, blocked-but-nobody-revisited).
    raw_stale_waiting = kanban_cfg.get("stale_review_timeout_seconds", 0)
    try:
        stale_waiting_timeout_seconds = int(raw_stale_waiting or 0)
    except (TypeError, ValueError):
        logger.warning("kanban dispatcher: invalid kanban.stale_review_timeout_seconds=%r; "
                       "disabling review/blocked staleness escalation", raw_stale_waiting)
        stale_waiting_timeout_seconds = 0

    # 0 disables bounded-retry -> manager escalation entirely (config, never a
    # hardcoded org chart -- see kanban_db_dispatch._resolve_escalation_manager).
    raw_escalation_limit = kanban_cfg.get("stale_review_escalation_limit", 0)
    try:
        stale_waiting_escalation_limit = int(raw_escalation_limit or 0)
    except (TypeError, ValueError):
        logger.warning("kanban dispatcher: invalid kanban.stale_review_escalation_limit=%r; "
                       "disabling manager escalation", raw_escalation_limit)
        stale_waiting_escalation_limit = 0
    raw_manager_map = kanban_cfg.get("escalation_manager_map")
    stale_waiting_escalation_manager_map = raw_manager_map if isinstance(raw_manager_map, dict) else None
    stale_waiting_escalation_manager = (kanban_cfg.get("escalation_manager") or "").strip() or None

    # Off by default: the only dispatcher detector that makes a network call.
    pr_review_wakeup_enabled = bool(kanban_cfg.get("pr_review_wakeup_enabled", False))

    # Fallback profile for tasks created without an assignee (e.g. via the
    # dashboard). Empty (the schema default) keeps skipping them.
    # When set, the dispatcher applies it to unassigned ready tasks instead of skipping them indefinitely
    # (#27145). Empty string (the schema default) means "no fallback, keep skipping" — backward-compatible
    # with existing installs.
    default_assignee = (kanban_cfg.get("default_assignee") or "").strip() or None
    if default_assignee:
        logger.info("kanban dispatcher: default_assignee=%r (unassigned ready tasks "
                    "will route to this profile)", default_assignee)

    return _DispatcherSettings(
        interval=interval,
        max_spawn=max_spawn,
        max_in_progress=effective_max_in_progress,
        failure_limit=failure_limit,
        max_spawns_without_completion=max_spawns_without_completion,
        stale_timeout_seconds=stale_timeout_seconds,
        # Requeue 'running' cards with broken claim bookkeeping (zombie-card
        # reconciliation); false keeps orphans frozen for manual forensics.
        reconcile_orphans=bool(kanban_cfg.get("reconcile_orphans", True)),
        default_assignee=default_assignee,
        # Per-profile concurrency cap: no single profile's local model / API
        # quota / browser pool gets overwhelmed by a fan-out.
        max_in_progress_per_profile=_positive_int_setting(kanban_cfg, "max_in_progress_per_profile"),
        stale_waiting_timeout_seconds=stale_waiting_timeout_seconds,
        stale_waiting_escalation_limit=stale_waiting_escalation_limit,
        stale_waiting_escalation_manager_map=stale_waiting_escalation_manager_map,
        stale_waiting_escalation_manager=stale_waiting_escalation_manager,
        pr_review_wakeup_enabled=pr_review_wakeup_enabled,
    )


class _KanbanDispatcher:
    """Per-tick board work for the embedded dispatcher (runs in worker threads).

    Boards are enumerated every tick so a board created mid-run is picked up
    without a restart. Corrupt-looking board DBs are quarantined per
    fingerprint and retried after ``CORRUPT_BOARD_RETRY_AFTER_SECONDS``:
    transient WAL/open races can look like "malformed" for one tick.
    """

    CORRUPT_BOARD_RETRY_AFTER_SECONDS = 300

    def __init__(self, kb: Any, settings: _DispatcherSettings) -> None:
        self.kb = kb
        self.settings = settings
        self.disabled_corrupt_boards: dict[str, tuple[tuple[str, int | None, int | None], float]] = {}
        # Bound autonomous fan-out when workers keep failing/reclaiming before
        # any task completes. The database is authoritative across gateway
        # restarts; these maps are only the per-tick in-memory mirror.
        self._spawns_since_completion: dict[str, int] = {}
        self._last_completed_run_id: dict[str, int | None] = {}

    @staticmethod
    def _ensure_respawn_state_schema(conn: sqlite3.Connection) -> None:
        """Create restart state for reduced legacy board schemas."""
        conn.execute(
            "CREATE TABLE IF NOT EXISTS dispatcher_respawn_state ("
            "board TEXT PRIMARY KEY, "
            "spawns_since_completion INTEGER NOT NULL DEFAULT 0 "
            "CHECK (spawns_since_completion >= 0), "
            "last_completed_run_id INTEGER, "
            "cap_blocked INTEGER NOT NULL DEFAULT 0 CHECK (cap_blocked IN (0, 1)), "
            "updated_at INTEGER NOT NULL)"
        )

    def _store_respawn_state(
        self, slug: str, conn: sqlite3.Connection, *, count: int,
        last_completed_run_id: int | None, cap_blocked: bool,
    ) -> None:
        """Atomically persist the dispatch window after every state transition."""
        with conn:
            conn.execute(
                "INSERT INTO dispatcher_respawn_state "
                "(board, spawns_since_completion, last_completed_run_id, cap_blocked, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(board) DO UPDATE SET "
                "spawns_since_completion = excluded.spawns_since_completion, "
                "last_completed_run_id = excluded.last_completed_run_id, "
                "cap_blocked = excluded.cap_blocked, updated_at = excluded.updated_at",
                (slug, count, last_completed_run_id, int(cap_blocked), int(time.time())),
            )

    def _load_respawn_state(
        self, slug: str, conn: sqlite3.Connection,
    ) -> tuple[int, int | None, bool]:
        """Load a durable window and reset it only after a new completion."""
        self._ensure_respawn_state_schema(conn)
        row = conn.execute(
            "SELECT spawns_since_completion, last_completed_run_id, cap_blocked "
            "FROM dispatcher_respawn_state WHERE board = ?", (slug,),
        ).fetchone()
        completed = conn.execute(
            "SELECT id FROM task_runs WHERE outcome = 'completed' "
            "ORDER BY ended_at DESC, id DESC LIMIT 1"
        ).fetchone()
        latest_id = int(completed[0]) if completed is not None else None
        if row is None:
            count, completion_id, cap_blocked = 0, latest_id, False
        else:
            count, completion_id, cap_blocked = int(row[0]), row[1], bool(row[2])
            if latest_id != completion_id:
                count, completion_id, cap_blocked = 0, latest_id, False
        self._store_respawn_state(
            slug, conn, count=count, last_completed_run_id=completion_id,
            cap_blocked=cap_blocked,
        )
        self._spawns_since_completion[slug] = count
        self._last_completed_run_id[slug] = completion_id
        return count, completion_id, cap_blocked

    def _board_slugs(self) -> list:
        return _board_slugs(self.kb)

    def board_db_fingerprint(self, slug: str) -> tuple[str, int | None, int | None]:
        path = self.kb.kanban_db_path(slug)
        try:
            resolved = str(path.expanduser().resolve())
        except Exception:
            resolved = str(path)
        try:
            stat = path.stat()
        except OSError:
            return (resolved, None, None)
        return (resolved, stat.st_mtime_ns, stat.st_size)

    def is_corrupt_board_db_error(self, exc: Exception) -> bool:
        if isinstance(exc, _kbc().KanbanDbCorruptError):
            return True
        return isinstance(exc, sqlite3.DatabaseError) and any(m in str(exc).lower() for m in _CORRUPT_DB_MARKERS)

    def _quarantine_lifted(self, slug: str, fingerprint: tuple) -> bool:
        """Return False while *slug* stays quarantined; lift (and log) otherwise."""
        disabled_entry = self.disabled_corrupt_boards.get(slug)
        if disabled_entry is None:
            return True
        disabled_fingerprint, disabled_at = disabled_entry
        age = time.monotonic() - disabled_at
        if disabled_fingerprint == fingerprint and age < self.CORRUPT_BOARD_RETRY_AFTER_SECONDS:
            return False
        if disabled_fingerprint == fingerprint:
            logger.info("kanban dispatcher: board %s database fingerprint unchanged "
                        "after %.0fs quarantine; retrying dispatch", slug, age)
        else:
            logger.info("kanban dispatcher: board %s database changed; retrying dispatch", slug)
        self.disabled_corrupt_boards.pop(slug, None)
        return True


    def tick_once_for_board(self, slug: str) -> Optional[object]:
        """Run one dispatch_once for a specific board.

        The per-board DB is opened explicitly so boards never share a
        connection or claim across each other.
        """
        conn = None
        fingerprint = self.board_db_fingerprint(slug)
        if not self._quarantine_lifted(slug, fingerprint):
            return None
        kwargs = {
            k: v
            for k, v in asdict(self.settings).items()
            if k not in {"interval", "max_spawns_without_completion"}
        }
        try:
            # No explicit init_db(): connect() runs the migration once per
            # process (see the matching note in the notifier collector).
            conn = _kbc().connect(board=slug)
            count, completion_id, cap_blocked = self._load_respawn_state(slug, conn)
            remaining = self.settings.max_spawns_without_completion - count
            if cap_blocked or remaining <= 0:
                cap_blocked = True
                self._store_respawn_state(
                    slug, conn, count=count, last_completed_run_id=completion_id,
                    cap_blocked=cap_blocked,
                )
                self._spawns_since_completion[slug] = count
                logger.warning(
                    "kanban dispatcher: board %s remains held by sticky respawn cap "
                    "(%d confirmed starts without a completion)",
                    slug, count,
                )
                return _kbd().DispatchResult(respawn_cap_blocked=True)
            configured_max_spawn = kwargs.get("max_spawn")
            kwargs["max_spawn"] = (
                remaining if configured_max_spawn is None
                else min(configured_max_spawn, remaining)
            )
            result = _kbd().dispatch_once(conn, board=slug, **kwargs)
            # A claim can be retried, and a process launch can fail, without a
            # worker ever starting. ``DispatchResult.spawned`` is the only
            # confirmed-start boundary. A completion is sampled before the
            # dispatch pass, so a just-completed worker always clears the cap
            # before this board can launch again.
            count += len(getattr(result, "spawned", ()) or ())
            cap_blocked = count >= self.settings.max_spawns_without_completion
            self._store_respawn_state(
                slug, conn, count=count, last_completed_run_id=completion_id,
                cap_blocked=cap_blocked,
            )
            self._spawns_since_completion[slug] = count
            if cap_blocked:
                logger.warning(
                    "kanban dispatcher: board %s reached sticky respawn cap: %d "
                    "confirmed starts without a completion (limit %d); new spawns "
                    "will remain blocked until a worker completes",
                    slug, count, self.settings.max_spawns_without_completion,
                )
                result.respawn_cap_blocked = True
            return result
        except Exception as exc:
            if self.is_corrupt_board_db_error(exc):
                self.disabled_corrupt_boards[slug] = (fingerprint, time.monotonic())
                logger.error(
                    "kanban dispatcher: board %s database %s is not a valid "
                    "SQLite database; pausing dispatch for this board until "
                    "the file changes, the gateway restarts, or the "
                    "quarantine timer expires. Move or restore the file, "
                    "then run `hermes kanban init` if you need a fresh board.",
                    slug, fingerprint[0],
                )
                return None
            logger.exception("kanban dispatcher: tick failed on board %s", slug)
            return None
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()

    def tick_once(self) -> list[tuple[str, Optional[object]]]:
        """Run one dispatch_once per board. Returns (slug, result) pairs."""
        return [(slug, self.tick_once_for_board(slug)) for slug in self._board_slugs()]

    def ready_nonempty(self) -> bool:
        """Is there a ready+assigned+unclaimed task on ANY board the dispatcher would spawn for?

        Control-plane lanes (e.g. ``orion-cc``) are pulled by terminals via
        ``claim_task`` and never spawnable — a queue full of those is
        "correctly idle", not "stuck". The review column is probed only when
        review dispatch is on (same gate as the dispatcher): a task waiting
        for a human reviewer is idle, not stuck.
        """
        kbd = _kbd()
        _review_probe = kbd.review_dispatch_enabled()
        for slug in self._board_slugs():
            conn = None
            try:
                conn = _kbc().connect(board=slug)
                if kbd.has_spawnable_ready(conn) or (_review_probe and kbd.has_spawnable_review(conn)):
                    return True
            except Exception:
                continue
            finally:
                if conn is not None:
                    with contextlib.suppress(Exception):
                        conn.close()
        return False

    def auto_decompose_tick(self, auto_decompose_per_tick: int) -> int:
        """Auto-decompose up to N triage tasks across all boards into ready workgraphs.

        Runs before dispatch fans out; the per-tick cap keeps a bulk triage
        load from burst-spending the aux LLM. Returns the number decomposed.
        """
        try:
            from hermes_cli import kanban_decompose as _decomp
        except Exception as exc:  # pragma: no cover
            logger.warning("kanban auto-decompose: import failed (%s); skipping", exc)
            return 0
        attempted = 0
        successes = 0
        with _default_profile_secret_scope():
            for slug in self._board_slugs():
                if attempted >= auto_decompose_per_tick:
                    break
                # Pin the board via env for the call: the decomposer connects
                # with no board kwarg (same pattern as the dashboard specify endpoint).
                prev_env = os.environ.get("HERMES_KANBAN_BOARD")
                try:
                    os.environ["HERMES_KANBAN_BOARD"] = slug
                    try:
                        triage_ids = _decomp.list_triage_ids()
                    except Exception as exc:
                        logger.debug("kanban auto-decompose: list_triage_ids failed on board %s (%s)", slug, exc)
                        triage_ids = []
                    for tid in triage_ids:
                        if attempted >= auto_decompose_per_tick:
                            break
                        attempted += 1
                        successes += self._decompose_one(_decomp, slug, tid)
                finally:
                    if prev_env is None:
                        os.environ.pop("HERMES_KANBAN_BOARD", None)
                    else:
                        os.environ["HERMES_KANBAN_BOARD"] = prev_env
        return successes

    @staticmethod
    def _decompose_one(_decomp: Any, slug: str, tid: str) -> int:
        """Decompose one triage task; returns 1 on success, 0 otherwise."""
        try:
            outcome = _decomp.decompose_task(tid, author="auto-decomposer")
        except Exception:
            logger.exception("kanban auto-decompose: decompose_task crashed on %s", tid)
            return 0
        if not outcome.ok:
            # Common no-op reasons (no aux client) must not spam logs every tick.
            logger.debug("kanban auto-decompose [%s]: %s skipped: %s", slug, tid, outcome.reason)
            return 0
        if outcome.fanout and outcome.child_ids:
            logger.info("kanban auto-decompose [%s]: %s → %d children", slug, tid, len(outcome.child_ids))
        else:
            logger.info("kanban auto-decompose [%s]: %s → single task (no fanout)", slug, tid)
        return 1


@contextlib.contextmanager
def _default_profile_secret_scope():
    """Install the gateway launch profile's secret scope while multiplexing is on.

    The tick runs via ``_to_thread_process_service`` in a fresh context, so no
    per-turn scope exists and ``get_secret`` fails closed. The decomposer's aux
    LLM reads ``auxiliary.*`` from ``get_hermes_home()``, so its credentials come
    from that same home. No-op for single-profile gateways.
    """
    from agent.secret_scope import (
        build_profile_secret_scope, is_multiplex_active, reset_secret_scope, set_secret_scope)
    from hermes_constants import get_hermes_home

    if not is_multiplex_active():
        yield
        return
    token = set_secret_scope(build_profile_secret_scope(Path(get_hermes_home())))
    try:
        yield
    finally:
        reset_secret_scope(token)


def _log_spawn_results(results: Optional[list]) -> bool:
    """Log per-board spawn summaries; returns whether any board spawned."""
    any_spawned = False
    for slug, res in (results or []):
        if res is not None and getattr(res, "spawned", None):
            any_spawned = True
            # Quiet by default: an idle gateway stays silent.
            logger.info(
                "kanban dispatcher [%s]: spawned=%d reclaimed=%d "
                "crashed=%d timed_out=%d promoted=%d auto_blocked=%d",
                slug, len(res.spawned), res.reclaimed,
                len(res.crashed) if hasattr(res.crashed, "__len__") else 0,
                len(res.timed_out) if hasattr(res.timed_out, "__len__") else 0,
                res.promoted,
                len(res.auto_blocked) if hasattr(res.auto_blocked, "__len__") else 0,
            )
    return any_spawned
