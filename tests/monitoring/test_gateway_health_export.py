from __future__ import annotations


























def test_otlp_attrs_redact_strings_and_never_export_profile():
    from agent.monitoring.otlp_exporter import _span_attrs

    attrs = _span_attrs({
        "event": "gateway_health",
        "name": "gateway.lifecycle",
        "profile": "user@example.com",
        "exit_reason": "Bearer top-secret-token-0123456789 for user@example.com",
    })

    assert "hermes.profile" not in attrs
    assert "top-secret-token-0123456789" not in str(attrs)
    assert "user@example.com" not in str(attrs)


def test_resource_attributes_are_allowlisted_and_sanitized():
    from agent.monitoring.otlp_exporter import _safe_resource_attributes

    attrs = _safe_resource_attributes({
        "service.name": "hermes-gateway",
        "service.instance.id": "install-1",
        "deployment.environment.name": "staging",
        "user.email": "user@example.com",
        "authorization": "Bearer top-secret-token-0123456789",
        "custom.request.id": "unbounded",
    })

    assert attrs == {
        "service.name": "hermes-gateway",
        "service.instance.id": attrs["service.instance.id"],
        "deployment.environment.name": "staging",
    }
    assert attrs["service.instance.id"].startswith("sha256:")
    assert "install-1" not in attrs["service.instance.id"]






def test_diagnostic_log_attributes_are_allowlisted_redacted_and_profile_free():
    from agent.monitoring.gateway_health_export import _diagnostic_log_attributes

    attrs = _diagnostic_log_attributes({
        "event": "gateway_diagnostic",
        "name": "platform.fatal",
        "subsystem": "platform.slack",
        "profile": "user@example.com",
        "error_code": "Bearer top-secret-token-0123456789",
        "custom": "must-not-egress",
    })

    assert "hermes.profile" not in attrs
    assert "hermes.custom" not in attrs
    assert "top-secret-token-0123456789" not in str(attrs)


def test_diagnostic_correlation_id_reaches_otlp_dashboard_attributes():
    from agent.monitoring.otlp_exporter import _span_attrs

    attrs = _span_attrs({
        "event": "gateway_diagnostic",
        "name": "worker.runtime.error",
        "subsystem": "worker.runtime",
        "correlation_id": "t_aedd2ed3",
        "error_code": "provider_failure",
    })

    assert attrs["hermes.correlation_id"] == "t_aedd2ed3"
    assert attrs["hermes.error_code"] == "provider_failure"




















def test_install_id_persists_across_calls(tmp_path, monkeypatch):
    """A minted install id must survive restarts (service.instance.id continuity)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}\n")

    import hermes_cli.config as cfg_mod
    from agent.monitoring.policy import ensure_install_id

    first = ensure_install_id(cfg_mod.load_config())
    assert first and first != "unknown"
    # Persisted: a fresh load (simulating a new gateway process) returns the same id.
    second = ensure_install_id(cfg_mod.load_config())
    assert second == first
    assert first in (tmp_path / "config.yaml").read_text()


def test_runtime_error_metric_registers_as_an_observable_counter(monkeypatch):
    """The cumulative runtime-error series must export as a monotonic counter."""
    from agent.monitoring import gateway_health_export

    registrations = {"gauges": [], "counters": []}

    class FakeMeter:
        def create_observable_gauge(self, name, *, callbacks):
            registrations["gauges"].append((name, callbacks))

        def create_observable_counter(self, name, *, callbacks):
            registrations["counters"].append((name, callbacks))

    class FakeProvider:
        def __init__(self, *, metric_readers, resource):
            self.metric_readers = metric_readers
            self.resource = resource

        def get_meter(self, _name):
            return FakeMeter()

    class FakeResource:
        @staticmethod
        def create(attributes):
            return attributes

    monkeypatch.setattr(gateway_health_export, "_exporter_kwargs", lambda *_: {})
    monkeypatch.setattr(gateway_health_export, "_gateway_health_config", lambda _config: {"export_interval_seconds": 60})
    monkeypatch.setattr(gateway_health_export, "_runtime_resource_attributes", lambda *_args, **_kwargs: {})
    sdk = {
        "OTLPMetricExporter": lambda **_kwargs: object(),
        "PeriodicExportingMetricReader": lambda exporter, **_kwargs: (exporter, _kwargs),
        "Resource": FakeResource,
        "MeterProvider": FakeProvider,
        "Observation": lambda value, attributes: (value, attributes),
    }

    gateway_health_export._start_metric_provider({}, sdk)

    assert "hermes.worker.runtime_errors" not in {name for name, _ in registrations["gauges"]}
    assert {name for name, _ in registrations["counters"]} == {"hermes.worker.runtime_errors"}


