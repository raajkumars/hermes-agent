"""Kanban workers must resolve the installed ``hermes_cli``, never a cwd namespace.

Workers are spawned with cwd = task workspace. When that workspace is a checkout
whose top level holds a ``hermes_cli/`` directory without ``__init__.py`` (a
hermes-overlay worktree does), ``python -m hermes_cli.main`` used to put the cwd
at ``sys.path[0]`` and import that directory as a PEP 420 namespace package, so
the worker died with ``cannot import name '__version__' from 'hermes_cli'``
before its first board call.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db_dispatch as kbd

REPO_ROOT = Path(__file__).resolve().parents[2]


def _overlay_style_worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "overlay-worktree"
    shadow = wt / "hermes_cli"
    shadow.mkdir(parents=True)
    # Same shape as qwickapps/hermes-overlay: modules but no __init__.py.
    (shadow / "pacing_governor_session.py").write_text("X = 1\n")
    assert not (shadow / "__init__.py").exists()
    return wt


_EDITABLE_FINDER = """
import sys
from importlib.machinery import PathFinder

_ROOT = {root!r}


class _EditableFinder:
    # Mirrors setuptools' editable-install finder: a meta-path finder consulted
    # AFTER PathFinder, so a sys.path namespace portion is found first.
    @classmethod
    def find_spec(cls, name, path=None, target=None):
        if path is not None:
            return None
        spec = PathFinder.find_spec(name, [_ROOT])
        if spec is None or spec.origin is None:
            return None
        return spec


_i = sys.meta_path.index(PathFinder) + 1
sys.meta_path.insert(_i, _EditableFinder)
"""


def _run(argv: list[str], cwd: Path, home: Path) -> subprocess.CompletedProcess:
    """Run with this checkout installed the way production installs Hermes.

    Production (/opt/hermes/.venv) has an editable install, i.e. ``hermes_cli``
    is NOT on ``sys.path``; it is resolved by a meta-path finder that runs after
    ``PathFinder``. Putting the checkout on PYTHONPATH instead would mask the bug
    (a regular package anywhere on sys.path beats a namespace portion).
    """
    site = home.parent / "site"
    site.mkdir(exist_ok=True)
    (site / "sitecustomize.py").write_text(_EDITABLE_FINDER.format(root=str(REPO_ROOT)))
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(site)}
    env.pop("PYTHONSAFEPATH", None)
    env.pop("HERMES_BIN", None)
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=120)


_PROBE = (
    "import hermes_cli, hermes_cli.main as m; "
    "print(hermes_cli.__file__); print(hermes_cli.__version__)"
)


def test_unsafe_module_argv_is_shadowed(tmp_path):
    """Control: proves the fixture reproduces the defect without ``-P``."""
    wt = _overlay_style_worktree(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    r = _run([sys.executable, "-m", "hermes_cli.main", "--version"], wt, home)
    assert r.returncode != 0
    assert "cannot import name" in r.stderr


def test_worker_argv_uses_safe_path():
    monkey_env = os.environ.pop("HERMES_BIN", None)
    try:
        argv = kbd._resolve_hermes_argv()
    finally:
        if monkey_env is not None:
            os.environ["HERMES_BIN"] = monkey_env
    assert argv[:4] == [sys.executable, "-P", "-m", "hermes_cli.main"]


def test_cli_launch_from_overlay_worktree_resolves_installed_package(tmp_path):
    wt = _overlay_style_worktree(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    r = _run([*kbd._module_hermes_argv(), "--version"], wt, home)
    assert r.returncode == 0, r.stderr
    assert "cannot import name" not in r.stderr


def test_worker_interpreter_flags_resolve_real_package(tmp_path):
    """Same interpreter flags the worker gets, with an import probe instead of chat."""
    wt = _overlay_style_worktree(tmp_path)
    flags = kbd._module_hermes_argv()[:-2]  # [python, -P]
    home = tmp_path / "home"
    home.mkdir()
    r = _run([*flags, "-c", _PROBE], wt, home)
    assert r.returncode == 0, r.stderr
    first = r.stdout.splitlines()[0]
    assert first == str(REPO_ROOT / "hermes_cli" / "__init__.py")
    assert str(wt) not in first


@pytest.mark.parametrize("subcmd", [["kanban", "--help"]])
def test_worker_entrypoint_from_overlay_worktree(tmp_path, subcmd):
    """The exact worker argv prefix boots the kanban CLI from the shadowing cwd."""
    wt = _overlay_style_worktree(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    r = _run([*kbd._resolve_hermes_argv(), *subcmd], wt, home)
    assert r.returncode == 0, r.stderr
    assert "cannot import name" not in r.stderr
