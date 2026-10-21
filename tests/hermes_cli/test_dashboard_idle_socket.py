"""Owner-only same-host idle proof socket (hermes_cli.web_server_idle_socket).

Regression for the deployed systemd shape: ``hermes dashboard`` under a unit whose Environment has
HERMES_HOME/HOME/PATH and NO session token. ``GET /api/health/idle`` must stay token-gated (401),
while a same-user supervisor gets an affirmative idle/busy answer over the local socket.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from hermes_cli import web_server_idle_socket as mod

REPO_ROOT = Path(__file__).resolve().parents[2]
posix_peercred = pytest.mark.linux_only  # SO_PEERCRED transport; other OSes fail closed (tested below)


def test_response_drops_detail_and_session_ids():
    out = mod.build_idle_response(lambda: {"idle": False, "reason": "turn_in_flight", "detail": "session:abc123"})
    assert out == {"idle": False, "reason": "turn_in_flight"}


@pytest.mark.parametrize("proof", [lambda: {"idle": 1}, lambda: {"idle": "true"}, lambda: None,
                                   lambda: {"idle": None, "reason": "turn_probe_unavailable"}])
def test_non_boolean_or_missing_idle_is_indeterminate(proof):
    assert mod.build_idle_response(proof)["idle"] is None


def test_raising_probe_is_indeterminate_never_idle():
    def boom():
        raise RuntimeError("ledger unreadable")
    assert mod.build_idle_response(boom) == {"idle": None, "reason": "probe_error"}


def test_unknown_verb_is_refused():
    body = json.loads(mod.handle_request_line(b'{"verb": "status"}', lambda: {"idle": True}))
    assert body["ok"] is False and "result" not in body
    body = json.loads(mod.handle_request_line(b"not json", lambda: {"idle": True}))
    assert body["ok"] is False


def test_path_is_keyed_by_host_and_port(tmp_path):
    a = mod.idle_socket_path(tmp_path, "127.0.0.1", 9120)
    b = mod.idle_socket_path(tmp_path, "100.127.85.87", 9120)
    assert a != b and a.parent == tmp_path and a.name.startswith("dashboard-idle-9120-")


def _short_home(tmp_path) -> Path:
    # pytest tmp paths can exceed sun_path; the server refuses those (covered below).
    import tempfile
    return Path(tempfile.mkdtemp(prefix="hidl", dir="/tmp"))  # no-tmp: ok — sun_path limit


@posix_peercred
@pytest.mark.parametrize("proof,expected", [({"idle": True, "reason": None}, True),
                                            ({"idle": False, "reason": "awaiting_human_input"}, False),
                                            ({"idle": None, "reason": "input_probe_unavailable"}, None)])
def test_roundtrip_same_user(tmp_path, proof, expected):
    home = _short_home(tmp_path)
    srv = mod.DashboardIdleSocket(home, "127.0.0.1", 9999, proof_fn=lambda: dict(proof))
    assert srv.start()
    try:
        assert stat.S_IMODE(os.stat(srv.path).st_mode) == 0o600
        got = mod.query_idle(home, "127.0.0.1", 9999)
        assert got["idle"] is expected and got["pid"] == os.getpid()
        # A different host:port is a different backend: never answered by this one.
        assert mod.query_idle(home, "127.0.0.2", 9999)["idle"] is None
    finally:
        srv.stop()
    assert not srv.path.exists()
    assert mod.query_idle(home, "127.0.0.1", 9999)["idle"] is None


@posix_peercred
def test_foreign_uid_peer_gets_no_answer(tmp_path, monkeypatch):
    home = _short_home(tmp_path)
    srv = mod.DashboardIdleSocket(home, "127.0.0.1", 9998, proof_fn=lambda: {"idle": True})
    assert srv.start()
    try:
        monkeypatch.setattr(mod, "peer_uid", lambda _sock: os.geteuid() + 4242)
        if os.geteuid() + 4242 == 0:  # pragma: no cover
            pytest.skip("uid arithmetic hit root")
        assert mod.query_idle(home, "127.0.0.1", 9998) == {"idle": None, "reason": "idle_socket_unavailable", "pid": None}
    finally:
        srv.stop()


def test_peer_authorization_rules():
    assert mod.peer_authorized(os.geteuid())
    assert mod.peer_authorized(0)
    assert not mod.peer_authorized(None)
    assert not mod.peer_authorized(os.geteuid() + 4242) or os.geteuid() + 4242 == 0


def test_platform_without_peercred_does_not_serve(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "peer_credentials_supported", lambda: False)
    srv = mod.DashboardIdleSocket(_short_home(tmp_path), "127.0.0.1", 9997)
    assert srv.start() is False and not srv.path.exists()


def test_overlong_home_does_not_serve(tmp_path):
    srv = mod.DashboardIdleSocket(tmp_path / ("x" * 120), "127.0.0.1", 9996)
    assert srv.start() is False


# ---------------------------------------------------------------------------
# Live: the deployed systemd unit shape (no session token in Environment)
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn_unit_shaped(home: Path, port: int, *, busy: bool) -> subprocess.Popen:
    """Environment mirrors hermes-dashboard-loopback.service: HERMES_HOME, HOME, PATH only."""
    env = {"HERMES_HOME": str(home), "HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "PYTHONUNBUFFERED": "1", "HERMES_SERVE_HEADLESS": "1"}
    hold = ("import cron.scheduler as s\n"
            "with s._running_lock:\n"
            "    s._running_job_ids.add(s._inflight_key('unit-shape-busy'))\n") if busy else ""
    code = hold + ("from hermes_cli.web_server import start_server\n"
                   f"start_server(host='127.0.0.1', port={port}, open_browser=False, headless=True)\n")
    return subprocess.Popen([sys.executable, "-c", code], cwd=str(REPO_ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _http_idle_status(port: int) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health/idle", timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return -1


def _await(fn, accept, timeout=90.0):
    deadline = time.monotonic() + timeout
    seen = []
    while True:
        v = fn()
        seen.append(v)
        if accept(v) or time.monotonic() >= deadline:
            return v, seen
        time.sleep(0.25)


def _cli(home: Path, port: int) -> subprocess.CompletedProcess:
    env = {"HERMES_HOME": str(home), "HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    return subprocess.run([sys.executable, "-m", "hermes_cli.web_server_idle_socket", "--host", "127.0.0.1",
                           "--port", str(port)], cwd=str(REPO_ROOT), env=env, capture_output=True, timeout=60)


def test_cli_without_a_running_dashboard_is_indeterminate(tmp_path):
    home = _short_home(tmp_path)
    proc = _cli(home, _free_port())
    assert proc.returncode == 2
    assert json.loads(proc.stdout)["idle"] is None


@posix_peercred
@pytest.mark.parametrize("busy", [False, True])
def test_live_systemd_unit_shape_gets_affirmative_proof_without_a_token(tmp_path, busy):
    home = _short_home(tmp_path)
    port = _free_port()
    proc = _spawn_unit_shaped(home, port, busy=busy)
    try:
        status, _ = _await(lambda: _http_idle_status(port), lambda s: s != -1)
        # The defect's precondition still holds: the HTTP idle route stays token-gated.
        assert status == 401
        want = False if busy else True
        verdict, seen = _await(lambda: mod.query_idle(home, "127.0.0.1", port), lambda v: v["idle"] is want)
        assert verdict["idle"] is want, seen
        assert verdict["pid"] == proc.pid
        if busy:
            assert verdict["reason"] == "turn_in_flight" and "detail" not in verdict
            assert not any(v["idle"] is True for v in seen), seen
        # The shell-facing contract a supervisor script uses: exit 0 idle / 1 busy.
        assert _cli(home, port).returncode == (1 if busy else 0)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    # Clean shutdown removes the socket so a later supervisor never talks to a dead path.
    assert not mod.idle_socket_path(home, "127.0.0.1", port).exists()
