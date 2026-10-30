from __future__ import annotations

import json
import logging


def test_worker_runtime_error_emits_redacted_audit_event_and_metric(monkeypatch, caplog):
    from agent.monitoring import runtime_diagnostics as diagnostics

    diagnostics.reset_worker_runtime_error_metrics_for_tests()
    emitted = []
    monkeypatch.setattr(diagnostics.emitter, "emit", emitted.append)

    try:
        raise RuntimeError("Bearer top-secret-token-0123456789 for user@example.com")
    except RuntimeError as exc:
        with caplog.at_level(logging.ERROR, logger="hermes.audit.worker_runtime"):
            event = diagnostics.report_worker_runtime_error(
                "Provider Failure", exception=exc, correlation_id="t_aedd2ed3",
            )

    assert event.name == "worker.runtime.error"
    assert event.subsystem == "worker.runtime"
    assert event.error_code == "provider_failure"
    assert event.trace_frame_count > 0
    assert event.trace_fingerprint != "none"
    assert event.correlation_id == "t_aedd2ed3"
    assert emitted == [event]
    assert "top-secret-token-0123456789" not in caplog.text
    assert "user@example.com" not in caplog.text
    audit = json.loads(caplog.records[-1].getMessage())
    assert audit == {
        "audit_event": "worker_runtime_error",
        "correlation_id": "t_aedd2ed3",
        "error_type": "provider_failure",
        "severity": "error",
        "stack_trace": {
            "fingerprint": event.trace_fingerprint,
            "frame_count": event.trace_frame_count,
        },
        "timestamp_ns": event.ts_ns,
    }
    assert diagnostics.worker_runtime_error_metrics() == [
        ("hermes.worker.runtime_errors", 1, {"error_type": "provider_failure"})
    ]


def test_stack_trace_metadata_accepts_only_bounded_content_free_values():
    from agent.monitoring.runtime_diagnostics import stack_trace_metadata

    metadata = stack_trace_metadata(metadata={
        "frame_count": "4",
        "fingerprint": "abc" * 100,
        "message": "must not appear",
        "path": "/private/path",
    })

    assert metadata == {"frame_count": 4, "fingerprint": "abc" * 21 + "a"}


def test_worker_runtime_error_uses_only_safe_kanban_workflow_correlation(monkeypatch):
    from agent.monitoring.runtime_diagnostics import report_worker_runtime_error

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abcd1234")
    assert report_worker_runtime_error("timeout").correlation_id == "t_abcd1234"

    monkeypatch.setenv("HERMES_KANBAN_TASK", "task with user@example.com")
    assert report_worker_runtime_error("timeout").correlation_id is None
