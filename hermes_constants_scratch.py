"""Scratch-dir retention: idle detection plus reaping needed before deletion.

Deleting an idle ``cache/scratch/<entry>`` alone leaves processes whose current
working directory was inside it and dangling git-worktree registrations.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_REAP_GRACE_SECONDS = 3.0
_GIT_FILE_MAX_DEPTH = 4


def subtree_touched_since(path: Path, cutoff: float) -> bool:
    """Whether *path* or an entry below it was written at or after *cutoff*.

    Symlinks are not followed. An unreadable entry is retained because its
    idleness cannot be established safely.
    """
    try:
        if os.lstat(path).st_mtime >= cutoff:
            return True
        if not path.is_dir() or path.is_symlink():
            return False
    except OSError:
        return True
    stack = [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for child in entries:
                    try:
                        if child.stat(follow_symlinks=False).st_mtime >= cutoff:
                            return True
                    except OSError:
                        return True
                    if child.is_dir(follow_symlinks=False):
                        stack.append(child.path)
        except OSError:
            return True
    return False


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _own_lineage() -> set[int]:
    """This process and its ancestors, which must never be reaped."""
    import psutil

    pids: set[int] = set()
    try:
        proc = psutil.Process()
        while proc is not None and proc.pid not in pids:
            pids.add(proc.pid)
            proc = proc.parent()
    except (psutil.Error, OSError):
        pass
    return pids


def reap_processes_rooted_in(scratch_root: Path, doomed: list[Path]) -> int:
    """Reap same-user processes rooted in paths about to be deleted."""
    import psutil

    root = os.path.realpath(str(scratch_root))
    targets = [os.path.realpath(str(path)) for path in doomed]
    skip = _own_lineage()
    uid = os.getuid() if hasattr(os, "getuid") else None
    victims: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid"]):
        if proc.pid in skip:
            continue
        try:
            if uid is not None and proc.uids().real != uid:
                continue
            cwd = proc.cwd()
        except (psutil.Error, OSError):
            continue
        if not cwd:
            continue
        deleted = cwd.endswith(" (deleted)")
        cwd_path = cwd[: -len(" (deleted)")] if deleted else cwd
        if not _under(cwd_path, root):
            continue
        if deleted or not os.path.exists(cwd_path) or any(_under(cwd_path, target) for target in targets):
            victims.append(proc)
    for proc in victims:
        try:
            proc.terminate()
        except (psutil.Error, OSError):
            continue
    _, alive = psutil.wait_procs(victims, timeout=_REAP_GRACE_SECONDS)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.Error, OSError):
            continue
    if victims:
        logger.info("scratch prune: reaped %d process(es) rooted in pruned entries", len(victims))
    return len(victims)


def _linked_worktree_repos(entry: Path) -> set[str]:
    """Find repositories with linked worktrees below *entry*."""
    repos: set[str] = set()
    stack = [(str(entry), 0)]
    while stack:
        current, depth = stack.pop()
        try:
            with os.scandir(current) as entries:
                for child in entries:
                    if child.name == ".git" and child.is_file(follow_symlinks=False):
                        try:
                            line = Path(child.path).read_text(encoding="utf-8", errors="replace").strip()
                        except OSError:
                            continue
                        if line.startswith("gitdir:"):
                            gitdir = Path(line[len("gitdir:"):].strip())
                            if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
                                repos.add(str(gitdir.parent.parent.parent))
                    elif (child.is_dir(follow_symlinks=False) and depth < _GIT_FILE_MAX_DEPTH
                          and child.name not in ("node_modules", ".venv", "venv")):
                        stack.append((child.path, depth + 1))
        except OSError:
            continue
    return repos


def release_git_worktrees(repos: set[str]) -> None:
    """Prune registrations for linked worktrees that were deleted."""
    for repo in sorted(repos):
        if not os.path.isdir(repo):
            continue
        try:
            subprocess.run(
                ["git", "-C", repo, "worktree", "prune"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("git worktree prune in %s failed: %s", repo, exc)


def prune_idle_entries(root: Path, max_idle_hours: float, skip_names: frozenset[str]) -> int:
    """Delete top-level entries idle throughout their subtree and return their count."""
    cutoff = time.time() - max_idle_hours * 3600
    try:
        entries = [entry for entry in root.iterdir() if entry.name not in skip_names]
    except OSError:
        return 0
    doomed = [entry for entry in entries if not subtree_touched_since(entry, cutoff)]
    try:
        reap_processes_rooted_in(root, doomed)
    except Exception as exc:
        logger.debug("scratch prune: process reap skipped: %s", exc)
    if not doomed:
        return 0
    repos: set[str] = set()
    removed = 0
    for entry in doomed:
        try:
            if entry.is_dir() and not entry.is_symlink():
                repos |= _linked_worktree_repos(entry)
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
            removed += 1
        except OSError:
            continue
    release_git_worktrees(repos)
    return removed
