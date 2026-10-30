"""Safe worker-runtime failure reporting for audit and monitoring."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import traceback
from collections import Counter
from typing import Any, Mapping, Optional

from agent.monitoring import emitter
from agent.monitoring.events import GatewayDiagnosticEvent

_audit_logger = logging.getLogger("hermes.audit.worker_runtime")
_lock = threading.Lock()
_error_counts: Counter[str] = Counter()
_SAFE_CORRELATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def _bounded_error_type(value: Any) -> str:
    """Return a low-cardinality error type suitable for metrics labels."""
    raw = str(value or "unknown").lower()
    normalized = "".join(char if char.isalnum() else "_" for char in raw).strip("_")
    return (normalized[:64] or "unknown")


def _workflow_correlation_id(value: Optional[Any] = None) -> Optional[str]:
    """Return a bounded workflow join key without admitting free-form task content."""
    candidate = str(value if value is not None else os.environ.get("HERMES_KANBAN_TASK", "")).strip()
    return candidate if _SAFE_CORRELATION_ID.fullmatch(candidate) else None


def stack_trace_metadata(exception: Optional[BaseException] = None, metadata: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Return content-free trace metadata; never retain a traceback or its source text."""
    supplied = dict(metadata or {})
    frame_count = supplied.get("frame_count", 0)
    try:
        frame_count = max(0, int(frame_count))
    except (TypeError, ValueError):
        frame_count = 0

    fingerprint = str(supplied.get("fingerprint") or "")[:64]
    if exception is not None:
        frames = traceback.extract_tb(exception.__traceback__) if exception.__traceback__ else []
        frame_count = len(frames)
        # Hash only stable code locations; paths, messages, locals and source lines never leave process memory.
        basis = "|".join(f"{frame.name}:{frame.lineno}" for frame in frames)
        fingerprint = hashlib.sha256(f"{type(exception).__name__}|{basis}".encode()).hexdigest()[:24]
    return {"frame_count": frame_count, "fingerprint": fingerprint or "none"}


def report_worker_runtime_error(
    error_type: Any,
    *,
    timestamp_ns: Optional[int] = None,
    exception: Optional[BaseException] = None,
    stack_metadata: Optional[Mapping[str, Any]] = None,
    correlation_id: Optional[Any] = None,
    severity: str = "error",
) -> GatewayDiagnosticEvent:
    """Write a structured audit record and emit a redacted monitoring diagnostic.

    This function is fail-open: an audit or monitoring failure must not hide the worker's
    original failure path.
    """
    kind = _bounded_error_type(error_type)
    timestamp = int(timestamp_ns or time.time_ns())
    trace = stack_trace_metadata(exception, stack_metadata)
    workflow_correlation_id = _workflow_correlation_id(correlation_id)
    error_class = type(exception).__name__ if exception is not None else kind
    event = GatewayDiagnosticEvent(
        name="worker.runtime.error",
        subsystem="worker.runtime",
        error_class=_bounded_error_type(error_class),
        error_code=kind,
        severity=severity if severity in {"warning", "error", "critical"} else "error",
        ts_ns=timestamp,
        trace_frame_count=trace["frame_count"],
        trace_fingerprint=trace["fingerprint"],
        source_logger="hermes.audit.worker_runtime",
        correlation_id=workflow_correlation_id,
    )
    try:
        _audit_logger.error(
            json.dumps({
                "audit_event": "worker_runtime_error",
                "correlation_id": workflow_correlation_id,
                "error_type": kind,
                "severity": event.severity,
                "stack_trace": trace,
                "timestamp_ns": timestamp,
            }, sort_keys=True, separators=(",", ":")),
            extra={
                "audit_event": "worker_runtime_error",
                "correlation_id": workflow_correlation_id,
                "error_type": kind,
                "timestamp_ns": timestamp,
                "stack_trace": trace,
            },
        )
    except Exception:
        pass
    try:
        with _lock:
            _error_counts[kind] += 1
        emitter.emit(event)
    except Exception:
        pass
    return event


def worker_runtime_error_metrics() -> list[tuple[str, int, dict[str, str]]]:
    """A bounded cumulative error count for the gateway health metric collector."""
    with _lock:
        return [
            ("hermes.worker.runtime_errors", count, {"error_type": error_type})
            for error_type, count in sorted(_error_counts.items())
        ]


def reset_worker_runtime_error_metrics_for_tests() -> None:
    with _lock:
        _error_counts.clear()
