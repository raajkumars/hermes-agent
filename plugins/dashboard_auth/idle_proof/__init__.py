"""IdleProofSecretProvider — owner-only machine credential for ``GET /api/health/idle``.

An auth-gated (non-loopback) dashboard deliberately ignores the loopback session token, so an
owner's automation (fleet overlay sync, retirement scripts) had no non-browser way to read the
idle snapshot. The owner provisions ``HERMES_DASHBOARD_IDLE_SECRET`` (env-only — a credential)
in the dashboard's ``.env``; a matching ``Authorization: Bearer`` is vouched for as the
``idle-proof`` principal with the single scope ``health-idle``.

Narrow by construction: the route is registered with that required scope, so this secret
authenticates nothing else and no other credential (e.g. the drain secret) authenticates this
route; registration is non-exclusive, so cookie and loopback session-token callers keep their
existing path. Same fail-closed entropy gate as the drain secret. No cookies, no login flow.
"""
from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

from hermes_cli.dashboard_auth import DashboardAuthProvider, Session, TokenPrincipal
from plugins.dashboard_auth._shared import NonInteractiveMixin, SkipRegistration, register_provider
from plugins.dashboard_auth.drain import assess_secret_strength

logger = logging.getLogger(__name__)
_TAG = "dashboard-auth-idle-proof"

IDLE_ROUTE_PATH = "/api/health/idle"
IDLE_SCOPE = "health-idle"
SECRET_ENV = "HERMES_DASHBOARD_IDLE_SECRET"

LAST_SKIP_REASON: str = ""


class IdleProofSecretProvider(NonInteractiveMixin, DashboardAuthProvider):
    """Non-interactive bearer-secret provider scoped to the idle-proof read."""

    name = "idle-proof-secret"
    display_name = "Idle Proof (owner machine credential)"
    supports_token = True
    supports_session = False
    _NOT_INTERACTIVE = "IdleProofSecretProvider is a non-interactive owner credential."

    def __init__(self, *, secret: str) -> None:
        reason = assess_secret_strength(secret)
        if reason is not None:
            raise ValueError(f"idle-proof secret rejected: {reason}")
        self._secret = secret

    def verify_token(self, *, token: str) -> Optional[TokenPrincipal]:
        if token and hmac.compare_digest(token.encode("utf-8"), self._secret.encode("utf-8")):
            return TokenPrincipal(principal="idle-proof", provider=self.name, scopes=(IDLE_SCOPE,))
        return None

    def verify_session(self, *, access_token: str) -> Optional[Session]:
        return None  # never mints a Session, so stacks harmlessly in the cookie-verify loop

    def refresh_session(self, *, refresh_token: str) -> Session:
        raise NotImplementedError(self._NOT_INTERACTIVE)

    def revoke_session(self, *, refresh_token: str) -> None:
        return None


def _settings() -> dict:
    secret = os.environ.get(SECRET_ENV, "").strip()
    if not secret:
        raise SkipRegistration(
            f"{SECRET_ENV} is not set; the idle proof stays reachable only through the "
            "dashboard's normal auth.")
    reason = assess_secret_strength(secret)
    if reason is not None:
        raise SkipRegistration(
            f"{SECRET_ENV} rejected — {reason}. Machine idle-proof auth stays disabled (fail-closed).",
            level="warning")
    return {"secret": secret}


def register(ctx) -> None:
    """Register the provider and opt ``/api/health/idle`` into the scoped, non-exclusive token
    seam when a strong secret is set; otherwise a no-op with a recorded skip reason."""
    global LAST_SKIP_REASON
    kwargs, LAST_SKIP_REASON = register_provider(ctx, logger, _TAG, IdleProofSecretProvider, _settings)
    if kwargs is None:
        return
    from hermes_cli.dashboard_auth.token_auth import register_token_route

    register_token_route(IDLE_ROUTE_PATH, scope=IDLE_SCOPE, exclusive=False)
    logger.info("%s: registered owner idle-proof credential (route=%s)", _TAG, IDLE_ROUTE_PATH)
