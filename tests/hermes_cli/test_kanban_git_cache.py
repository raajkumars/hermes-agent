"""Regression coverage for shared immutable Git reference caches used by Kanban clones."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import subprocess
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_git_cache as cache


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=Test User", "-c", "user.email=test@example.com", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_remote(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True, text=True)
    (source / "README.md").write_text("one\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "initial")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(source), str(remote)], check=True, capture_output=True, text=True)
    _git(source, "remote", "add", "origin", str(remote))
    return source, remote


def _push_change(source: Path, text: str) -> str:
    (source / "README.md").write_text(text, encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", text.strip())
    _git(source, "push", "origin", "main")
    return _git(source, "rev-parse", "HEAD")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    return home


def test_reference_clone_isolated_and_fetches_new_remote_history(kanban_home, tmp_path):
    source, remote = _make_remote(tmp_path)
    first = cache.clone_with_reference(str(remote), tmp_path / "first")
    reference = cache.reference_cache_path(str(remote))
    alternates = first / ".git" / "objects" / "info" / "alternates"

    assert reference.is_dir()
    assert alternates.read_text(encoding="utf-8").strip() == str(reference / "objects")
    cached_head = _git(reference, "rev-parse", "HEAD")

    second_head = _push_change(source, "two\n")
    second = cache.clone_with_reference(str(remote), tmp_path / "second")
    assert _git(second, "rev-parse", "HEAD") == second_head
    (second / "README.md").write_text("second workspace only\n", encoding="utf-8")
    assert (first / "README.md").read_text(encoding="utf-8") == "one\n"
    # The shared cache is immutable; new remote history reached the second clone
    # through its own fetch, not by mutating a cache other workspaces may need.
    assert _git(reference, "rev-parse", "HEAD") == cached_head

    fetched_head = _push_change(source, "three\n")
    _git(first, "fetch", "origin")
    assert _git(first, "rev-parse", "origin/main") == fetched_head


def test_invalid_cache_path_is_not_deleted_or_replaced(kanban_home, tmp_path):
    _source, remote = _make_remote(tmp_path)
    reference = cache.reference_cache_path(str(remote))
    reference.mkdir(parents=True)
    sentinel = reference / "keep.txt"
    sentinel.write_text("do not delete\n", encoding="utf-8")

    with pytest.raises(cache.GitReferenceCacheError, match="not a bare repository"):
        cache.clone_with_reference(str(remote), tmp_path / "destination")

    assert sentinel.read_text(encoding="utf-8") == "do not delete\n"


def test_concurrent_creators_share_one_immutable_published_cache(kanban_home, tmp_path, monkeypatch):
    _source, remote = _make_remote(tmp_path)
    reference = cache.reference_cache_path(str(remote))
    original_rename = cache.os.rename
    original_rmtree = cache.shutil.rmtree
    publishers = 4
    publish_barrier = threading.Barrier(publishers)
    removed_paths: list[Path] = []

    def synchronized_rename(source, destination):
        if Path(destination) == reference:
            publish_barrier.wait(timeout=15)
        return original_rename(source, destination)

    def recording_rmtree(path, *args, **kwargs):
        removed_paths.append(Path(path))
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(cache.os, "rename", synchronized_rename)
    monkeypatch.setattr(cache.shutil, "rmtree", recording_rmtree)

    with ThreadPoolExecutor(max_workers=publishers) as executor:
        destinations = [tmp_path / f"workspace-{index}" for index in range(publishers)]
        clones = list(executor.map(lambda destination: cache.clone_with_reference(str(remote), destination), destinations))

    assert clones == destinations
    assert cache._is_bare_repository(reference)
    assert all((clone / ".git").is_dir() for clone in clones)
    assert all(_git(clone, "rev-parse", "HEAD") == _git(reference, "rev-parse", "HEAD") for clone in clones)
    # Creators discard only their private staging mirrors; the published cache
    # must never be selected for cleanup by a losing creator.
    assert reference not in removed_paths


def test_clone_command_uses_the_cache_backed_clone_path(kanban_home, tmp_path, capsys):
    _source, remote = _make_remote(tmp_path)
    destination = tmp_path / "workspace"
    parser = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(parser.add_subparsers())

    args = parser.parse_args(["kanban", "clone", str(remote), str(destination)])
    assert kc._cmd_clone(args) == 0
    assert capsys.readouterr().out.strip() == str(destination)
    assert (destination / ".git").is_dir()
    assert cache.reference_cache_path(str(remote)).is_dir()
