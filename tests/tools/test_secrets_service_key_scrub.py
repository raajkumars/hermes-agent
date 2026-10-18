"""The secrets-service master key never reaches a child process.

The hermes-secrets-plugin reads the key in-process (``os.getenv``); one key reads every stored
secret, so an ``env`` dump in the terminal tool exposed the whole store. The terminal scrub drops
only blocklisted names, so the key has to sit in the provider blocklist (Tier 2) as well as the
always-strip set (Tier 1) that covers ``inherit_credentials`` children.
"""

import os
from unittest.mock import patch

import pytest

from tools.environments.local import (
    LocalEnvironment, _sanitize_subprocess_env, hermes_subprocess_env,
)
from tools.environments.local_env_policy import _SECRETS_SERVICE_KEY_ENV_VARS

_FAKE = {name: f"fake-{name.lower()}" for name in _SECRETS_SERVICE_KEY_ENV_VARS}


@pytest.mark.linux_only
def test_terminal_child_env_dump_does_not_show_secrets_service_key(tmp_path, monkeypatch):
    for name, value in _FAKE.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("QA_HARMLESS_MARKER", "visible")

    output = LocalEnvironment(cwd=str(tmp_path)).execute("env")["output"]

    assert "QA_HARMLESS_MARKER=visible" in output  # the dump itself worked
    leaked = [name for name in _FAKE if f"{name}=" in output or _FAKE[name] in output]
    assert not leaked, f"terminal child saw {leaked}"


def test_every_child_env_builder_drops_secrets_service_key():
    base = {"PATH": "/usr/bin:/bin", "HOME": "/home/user", **_FAKE}
    with patch.dict(os.environ, base, clear=True):
        built = {
            "background/pty": _sanitize_subprocess_env(dict(os.environ)),
            "background/pty extra": _sanitize_subprocess_env({}, dict(_FAKE)),
            "non-terminal": hermes_subprocess_env(),
            "non-terminal inherit_credentials": hermes_subprocess_env(inherit_credentials=True),
        }
    leaked = {surface: sorted(set(_FAKE) & set(env)) for surface, env in built.items()}
    assert not any(leaked.values()), leaked
