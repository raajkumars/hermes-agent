"""Immutable Git object caches for disk-efficient Kanban workspace clones.

A clone made with ``--reference-if-able`` borrows objects through Git's
``alternates`` mechanism.  Deleting or pruning that reference later can corrupt
an active clone, so this module deliberately treats a populated cache as
immutable: it is created once and is never refreshed, pruned, or removed by
Kanban.  Each destination retains its own working tree, index, refs and remote;
normal ``git fetch`` continues to contact its origin.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from hermes_cli import kanban_db as kb


class GitReferenceCacheError(RuntimeError):
    """A reference cache or a clone operation could not be completed safely."""


def reference_cache_root() -> Path:
    """Return the board-independent, profile-scoped cache directory."""
    return kb.kanban_home() / "kanban" / "git-reference-cache"


def _repository_identity(repository: str) -> str:
    """Stable cache identity without resolving or rewriting remote URLs."""
    candidate = Path(repository).expanduser()
    if candidate.exists():
        return str(candidate.resolve())
    return repository.strip()


def reference_cache_path(repository: str) -> Path:
    """Return the immutable bare-cache location for *repository*."""
    identity = _repository_identity(repository)
    if not identity:
        raise GitReferenceCacheError("repository must not be empty")
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return reference_cache_root() / f"{digest}.git"


def _git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _is_bare_repository(path: Path) -> bool:
    result = _git(["-C", str(path), "rev-parse", "--is-bare-repository"])
    return result.returncode == 0 and result.stdout.strip() == "true"


def ensure_reference_cache(repository: str) -> Path:
    """Create (once) and return a self-contained bare cache for *repository*.

    The cache is intentionally immutable after publication.  It is safe for an
    active clone to use indefinitely because this function never prunes, fetches
    into, or deletes a published cache.  A concurrent creator may win the
    publish race; in that case the losing temporary cache is discarded and the
    already-published cache is used.
    """
    cache = reference_cache_path(repository)
    root = cache.parent
    root.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        if _is_bare_repository(cache):
            return cache
        raise GitReferenceCacheError(f"reference cache path exists but is not a bare repository: {cache}")

    temporary = Path(tempfile.mkdtemp(prefix=f".{cache.stem}.building-", dir=root))
    shutil.rmtree(temporary)
    try:
        # --no-local guarantees the mirror owns its objects rather than borrowing
        # from a transient source checkout through hard links or alternates.
        result = _git(["clone", "--mirror", "--no-local", repository, str(temporary)])
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise GitReferenceCacheError(f"could not create reference cache for {repository!r}: {detail}")
        try:
            os.rename(temporary, cache)
        except FileExistsError:
            # Another worker safely published the same immutable cache first.
            if not _is_bare_repository(cache):
                raise GitReferenceCacheError(f"published cache is not a bare repository: {cache}")
        if not _is_bare_repository(cache):
            raise GitReferenceCacheError(f"reference cache was not published as a bare repository: {cache}")
        return cache
    finally:
        # Only clean an unpublished temporary directory.  Published caches are
        # deliberately never removed, including by error handling.
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def clone_with_reference(repository: str, destination: str | Path, *, branch: str | None = None) -> Path:
    """Clone *repository* into *destination* using its immutable object cache."""
    cache = ensure_reference_cache(repository)
    target = Path(destination).expanduser()
    command = ["clone", "--reference-if-able", str(cache)]
    if branch:
        command.extend(["--branch", branch])
    command.extend([repository, str(target)])
    result = _git(command)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GitReferenceCacheError(f"git clone failed: {detail}")
    return target
