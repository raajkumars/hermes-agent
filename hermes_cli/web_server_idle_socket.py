"""Same-host, same-user idle proof for a running dashboard/serve backend.

``GET /api/health/idle`` is token-gated, and a supervised dashboard (systemd ``hermes dashboard``)
mints a fresh in-memory session token per start, so a same-host supervisor (e.g. an updater that must
restart the unit only while it is idle) has no way to ask. This module answers that one question over
a local socket instead of widening the HTTP auth surface:

* **Transport is the auth boundary.** An ``AF_UNIX`` socket in the launch ``HERMES_HOME`` created
  under a ``0o177`` umask and chmod ``0600`` — never a TCP port, so nothing remote can reach it.
* **Explicit peer authorization.** Every connection's kernel-reported peer credentials
  (``SO_PEERCRED``) must be this process's effective UID or root. Platforms without ``SO_PEERCRED``
  do not serve at all (fail closed) — callers there treat the proof as indeterminate.
* **No secret.** Nothing to provision, store, rotate or leak; the dashboard unit needs no change.
* **Minimal answer.** ``idle`` + ``reason`` only. ``idle_proof``'s ``detail`` (which names session
  ids / cron job ids) is deliberately dropped.

Wire contract (v1), shaped like ``gateway.control_socket``: ONE request per connection, one JSON line
in (``{"verb": "idle"}``), one JSON line out, then the server closes::

    {"ok": true, "protocol": 1, "pid": <server pid>, "result": {"idle": true|false|null, "reason": ...}}

``idle`` is ``null`` whenever the proof is indeterminate; callers MUST treat that exactly like busy.
Like the HTTP route this is a diagnostic snapshot, never a retirement permit.

Socket path: ``<HERMES_HOME>/dashboard-idle-<port>-<sha256(host)[:8]>.sock`` (see
:func:`idle_socket_path`; ``host``/``port`` are the dashboard's ``--host``/bound ``--port``). A home
whose path would overflow ``sun_path`` serves nothing (logged once) rather than falling back to a
shared temp dir.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import socket
import socketserver
import struct
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

_log = logging.getLogger(__name__)

IDLE_SOCKET_PROTOCOL_VERSION = 1
_MAX_UNIX_PATH = 100  # sun_path is 104 (BSD/macOS) / 108 (Linux); keep margin for the NUL
_MAX_REQUEST_BYTES = 4096
_IO_TIMEOUT = 2.0
_PEERCRED_FMT = "3i"  # struct ucred {pid_t pid; uid_t uid; gid_t gid;}


def idle_socket_path(home: Path | str, host: str, port: int) -> Path:
    """Deterministic socket path for the dashboard bound to ``host:port`` under ``home``."""
    digest = hashlib.sha256(str(host).strip().lower().encode("utf-8")).hexdigest()[:8]
    return Path(home) / f"dashboard-idle-{int(port)}-{digest}.sock"


def peer_credentials_supported() -> bool:
    return hasattr(socket, "SO_PEERCRED")


def peer_uid(sock: socket.socket) -> Optional[int]:
    """Kernel-reported UID of the connected peer; None when it cannot be read."""
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize(_PEERCRED_FMT))
        _pid, uid, _gid = struct.unpack(_PEERCRED_FMT, raw)
        return int(uid)
    except (AttributeError, OSError, struct.error):
        return None


def peer_authorized(uid: Optional[int]) -> bool:
    """Only the dashboard's own service user (or root, who could read its memory anyway)."""
    return uid is not None and (uid == os.geteuid() or uid == 0)


def _default_proof() -> dict:
    from hermes_cli.web_server_idle_proof import idle_proof

    return idle_proof()


def build_idle_response(proof_fn: Callable[[], dict] = _default_proof) -> dict[str, Any]:
    """The socket's answer: ``idle``/``reason`` only. A raising probe is indeterminate, never idle."""
    try:
        proof = proof_fn()
    except Exception:
        _log.warning("dashboard idle socket: idle proof raised; answering indeterminate", exc_info=True)
        proof = {"idle": None, "reason": "probe_error"}
    idle = proof.get("idle") if isinstance(proof, dict) else None
    if not isinstance(idle, bool):
        idle = None
    reason = proof.get("reason") if isinstance(proof, dict) else "probe_error"
    return {"idle": idle, "reason": reason if isinstance(reason, str) or reason is None else None}


def handle_request_line(raw: bytes, proof_fn: Callable[[], dict] = _default_proof) -> bytes:
    base: dict[str, Any] = {"protocol": IDLE_SOCKET_PROTOCOL_VERSION, "pid": os.getpid()}
    try:
        request = json.loads(raw.decode("utf-8"))
        verb = request.get("verb") if isinstance(request, dict) else None
    except (UnicodeDecodeError, ValueError):
        verb = None
    if verb != "idle":
        response = {"ok": False, "error": "unknown verb", "supported_verbs": ["idle"], **base}
    else:
        response = {"ok": True, "result": build_idle_response(proof_fn), **base}
    return json.dumps(response).encode("utf-8") + b"\n"


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:  # noqa: D401 - socketserver hook
        sock: socket.socket = self.request
        uid = peer_uid(sock)
        if not peer_authorized(uid):
            # Close without an answer: an unauthorized peer learns nothing, not even the verb list.
            _log.warning("dashboard idle socket: refused peer uid=%s", uid)
            return
        sock.settimeout(_IO_TIMEOUT)
        buf = b""
        try:
            while b"\n" not in buf and len(buf) <= _MAX_REQUEST_BYTES:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                buf += chunk
        except OSError:
            return
        if len(buf) > _MAX_REQUEST_BYTES:
            return
        with contextlib.suppress(OSError):
            sock.sendall(handle_request_line(buf.partition(b"\n")[0], self.server.proof_fn))  # type: ignore[attr-defined]


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


class DashboardIdleSocket:
    """Owner-only idle-proof socket for one dashboard process. All failures are non-fatal."""

    def __init__(self, home: Path | str, host: str, port: int,
                 proof_fn: Callable[[], dict] = _default_proof) -> None:
        self.path = idle_socket_path(home, host, port)
        self._proof_fn = proof_fn
        self._server: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        if not peer_credentials_supported():
            _log.info("dashboard idle socket disabled: no SO_PEERCRED on this platform")
            return False
        if len(str(self.path).encode("utf-8")) > _MAX_UNIX_PATH:
            _log.warning("dashboard idle socket disabled: path too long for sun_path (%s)", self.path)
            return False
        try:
            # Our port bind already succeeded, so a file here is a stale leftover, not a live sibling.
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
            old_umask = os.umask(0o177)
            try:
                server = _Server(str(self.path), _Handler)
            finally:
                os.umask(old_umask)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            _log.warning("dashboard idle socket failed to start (non-fatal): %s", exc)
            return False
        server.proof_fn = self._proof_fn  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5},
                                        name="dashboard-idle-socket", daemon=True)
        self._thread.start()
        _log.info("dashboard idle socket listening at %s", self.path)
        return True

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            with contextlib.suppress(Exception):
                server.shutdown()
            with contextlib.suppress(Exception):
                server.server_close()
            with contextlib.suppress(OSError):
                self.path.unlink()


def query_idle(home: Path | str, host: str, port: int, *, timeout: float = _IO_TIMEOUT) -> dict[str, Any]:
    """Client side. Returns ``{"idle": True|False|None, "reason": str|None, "pid": int|None}``.

    Any failure (no socket, refused, timeout, malformed or ``ok: false`` answer) is indeterminate
    (``idle: None``) — never idle. Never raises.
    """
    path = idle_socket_path(home, host, port)
    indeterminate = {"idle": None, "reason": "idle_socket_unavailable", "pid": None}
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(path))
            sock.sendall(b'{"verb": "idle", "protocol": 1}\n')
            buf = b""
            while b"\n" not in buf and time.monotonic() < deadline and len(buf) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        response = json.loads(buf.partition(b"\n")[0].decode("utf-8"))
    except (OSError, ValueError):
        return indeterminate
    if not isinstance(response, dict) or response.get("ok") is not True:
        return indeterminate
    result = response.get("result")
    if not isinstance(result, dict):
        return indeterminate
    idle = result.get("idle")
    pid = response.get("pid")
    return {"idle": idle if isinstance(idle, bool) else None, "reason": result.get("reason"),
            "pid": pid if isinstance(pid, int) else None}


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m hermes_cli.web_server_idle_socket --host H --port P``: exit 0 idle, 1 busy, 2 indeterminate."""
    import argparse

    from hermes_constants import get_hermes_home

    parser = argparse.ArgumentParser(description="Query a same-host dashboard's idle proof over its owner-only socket.")
    parser.add_argument("--host", required=True, help="the dashboard's --host")
    parser.add_argument("--port", required=True, type=int, help="the dashboard's bound --port")
    parser.add_argument("--timeout", type=float, default=_IO_TIMEOUT)
    args = parser.parse_args(argv)
    verdict = query_idle(get_hermes_home(), args.host, args.port, timeout=args.timeout)
    print(json.dumps(verdict))
    return {True: 0, False: 1}.get(verdict["idle"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
