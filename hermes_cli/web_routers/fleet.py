"""Fleet activity across Kanban boards and served gateway profiles.

This router intentionally reuses existing Kanban SQLite state, gateway routing indexes,
and the provider-pace snapshot. It introduces neither a service nor persistence. The
pace summary uses the dispatcher's shared throttle decision so the dashboard explains
the same effective concurrency cap the fleet applies.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from hermes_cli.web_deps import late

router = APIRouter()
_log = logging.getLogger("hermes_cli.web_server")

# Late-bound so a test's monkeypatch on the owning module wins at call time.
_open_session_db_for_profile = late("_open_session_db_for_profile", "hermes_cli.web_server_sessions")
_session_db_path_for_profile = late("_session_db_path_for_profile", "hermes_cli.web_server_sessions")
_collect_profile_gateway_topology_cached = late(
    "_collect_profile_gateway_topology_cached", "hermes_cli.web_server_gateway")


def _kanban_running_tasks() -> List[Dict[str, Any]]:
    """Running tasks across every non-archived Kanban board, best-effort."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect as _kanban_connect

    try:
        boards = kb.list_boards(include_archived=False)
    except Exception:
        _log.debug("fleet activity: list_boards failed", exc_info=True)
        return []

    now = time.time()
    tasks: List[Dict[str, Any]] = []
    for meta in boards:
        slug = meta.get("slug") or kb.DEFAULT_BOARD
        try:
            path = kb.kanban_db_path(board=slug).expanduser()
            if not path.exists():
                continue
            conn = _kanban_connect(board=slug)
        except Exception:
            _log.debug("fleet activity: skipping unreadable board %r", slug, exc_info=True)
            continue
        try:
            for task in kb.list_tasks(conn, status="running"):
                started = task.started_at
                heartbeat = task.last_heartbeat_at
                tasks.append({
                    "board": slug,
                    "board_name": meta.get("name") or slug,
                    "task_id": task.id,
                    "title": task.title,
                    "profile": task.assignee,
                    "started_at": started,
                    "elapsed_seconds": max(0, int(now - started)) if started else None,
                    "last_heartbeat_at": heartbeat,
                    "heartbeat_age_seconds": max(0, int(now - heartbeat)) if heartbeat else None,
                })
        except Exception:
            _log.debug("fleet activity: list_tasks failed for board %r", slug, exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    tasks.sort(key=lambda row: row["started_at"] or 0, reverse=True)
    return tasks


def _gateway_sessions_for_profile(profile: Optional[str], *, label: str) -> List[Dict[str, Any]]:
    """Gateway-routing rows with a currently active turn for one profile."""
    try:
        db_path = _session_db_path_for_profile(profile)
        if not Path(db_path).exists():
            return []
        db = _open_session_db_for_profile(profile, read_only=True)
    except Exception:
        _log.debug("fleet activity: cannot open session db for profile %r", profile, exc_info=True)
        return []

    try:
        raw = db.load_gateway_routing_entries()
    except Exception:
        _log.debug("fleet activity: load_gateway_routing_entries failed for %r", profile, exc_info=True)
        return []
    finally:
        with contextlib.suppress(Exception):
            db.close()

    now = time.time()
    sessions: List[Dict[str, Any]] = []
    for session_key, entry_json in raw.items():
        try:
            data = json.loads(entry_json or "{}")
        except Exception:
            continue
        started_raw = data.get("active_turn_started_at")
        if not started_raw:
            continue
        try:
            started_epoch = datetime.fromisoformat(started_raw).timestamp()
        except Exception:
            continue
        sessions.append({
            "profile": label,
            "session_key": session_key,
            "platform": data.get("platform"),
            "display_name": data.get("display_name"),
            "chat_type": data.get("chat_type"),
            "started_at": started_epoch,
            "elapsed_seconds": max(0, int(now - started_epoch)),
        })
    return sessions


def _all_gateway_sessions() -> List[Dict[str, Any]]:
    """Active gateway turns across all profiles served by this process."""
    try:
        from hermes_cli.profiles import get_active_profile_name
        own_name = get_active_profile_name()
    except Exception:
        own_name = "default"

    targets: List[tuple[Optional[str], str]] = [(None, own_name)]
    try:
        topology = _collect_profile_gateway_topology_cached()
        for name in topology.get("profiles") or []:
            if name and name != own_name:
                targets.append((name, name))
    except Exception:
        _log.debug("fleet activity: profile topology unavailable", exc_info=True)

    sessions: List[Dict[str, Any]] = []
    for profile_arg, label in targets:
        sessions.extend(_gateway_sessions_for_profile(profile_arg, label=label))
    sessions.sort(key=lambda row: row["started_at"], reverse=True)
    return sessions


def _provider_pace() -> Optional[Dict[str, Any]]:
    """Provider pace snapshot plus the dispatcher's shared throttle decision.

    Missing or invalid pace state stays non-fatal: the activity endpoint still reports
    live workers and gateway sessions, and the dispatcher's fail-open telemetry is not
    fabricated when there is no applicable cap.
    """
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_db_dispatch_pacing as pacing

    state_path = pacing.default_state_path()
    try:
        doc = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("providers"), dict):
        return None

    base_cap = dispatch.resolve_max_in_progress(dispatch.configured_max_in_progress())
    decision = pacing.resolve_concurrency_throttle(base_cap, state_path=state_path)
    payload: Dict[str, Any] = {
        "generated_at": doc.get("generated_at"),
        "chain": doc.get("chain") or [],
        "reserved_lane_pct": doc.get("reserved_lane_pct"),
        "providers": doc["providers"],
    }
    if decision is not None:
        payload["throttle"] = decision.payload()
    return payload


@router.get("/api/fleet/activity")
async def get_fleet_activity():
    """Fleet-wide running work and shared provider-pace throttle telemetry."""
    kanban_tasks = await run_in_threadpool(_kanban_running_tasks)
    gateway_sessions = await run_in_threadpool(_all_gateway_sessions)
    provider_pace = await run_in_threadpool(_provider_pace)
    return {
        "kanban_tasks": kanban_tasks,
        "gateway_sessions": gateway_sessions,
        "count": len(kanban_tasks) + len(gateway_sessions),
        "provider_pace": provider_pace,
    }
