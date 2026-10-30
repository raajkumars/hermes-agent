---
title: Centralized error diagnostics ingestion API
description: Contract for safely ingesting structured runtime failure diagnostics.
---

# Centralized error diagnostics ingestion API

The diagnostics API receives structured, bounded failure metadata for the monitoring plane. It is not a log-upload or debug-bundle endpoint. Producers must never send raw exception messages, traceback text, source paths, prompts, messages, request bodies, environment values, local variables, credentials, or personal data.

The machine-readable REST contract is available at `/openapi/centralized-error-diagnostics-v1.yaml`. The equivalent gRPC service definition is in `api/diagnostics/v1/diagnostics.proto`.

## Contract

`POST /v1/diagnostics` accepts one event. Required event fields are:

- `event_id`: producer UUID identifying the occurrence.
- `observed_at`: RFC 3339 UTC timestamp.
- `service`, `severity`, and `error.type`: bounded classification values.
- `error.stack`: 1–64 structured frames (`module`, `function`, `line`) rather than raw traceback text.

Optional `correlation_id` links the failure to one workflow or request. Optional `attributes` is strictly bounded to twenty monitoring-approved string dimensions. The handler rejects unknown top-level fields and unbounded payload forms.

## Authentication and idempotency

Producers authenticate with a bearer credential scoped to `diagnostics.ingest`. Every request includes an `Idempotency-Key` UUID. The server treats an identical replay within its retention window as the same event; reuse of that key with a different payload returns `409`.

## Retry policy

- Do not retry validation, authentication, authorization, content-type, payload-size, or idempotency-conflict responses without changing the request.
- Retry `429`, `500`, and `503` with the original idempotency key, full-jitter exponential backoff, and a bounded retry budget. Respect `Retry-After` when present.
- The gRPC service maps invalid input to `INVALID_ARGUMENT`, bad credentials to `UNAUTHENTICATED`, forbidden producers to `PERMISSION_DENIED`, key/body conflicts to `ALREADY_EXISTS`, rate limits to `RESOURCE_EXHAUSTED`, and transient failures to `UNAVAILABLE` or `INTERNAL`.

## Monitoring integration decision

This contract deliberately matches the existing monitoring plane's metadata-only pattern: `error_class`, stable code/fingerprint, severity, and a frame count/fingerprint are safe export dimensions, while raw stack text and paths are not. The monitoring implementation should use `error.type` (and, where needed, `error.code`) as dimensions, and should retain only an allowlisted representation of stack frames.

Before deploying producers, monitoring must confirm the final allowlist for `attributes`, the idempotency retention period, payload-size limit, and rate-limit budget. Those operational values are intentionally not hard-coded into this protocol.
