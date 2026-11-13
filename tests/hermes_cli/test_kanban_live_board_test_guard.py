"""Kanban test runs must not write a board inherited from a worker process."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db_connect as kbc


def test_test_harness_refuses_external_board_before_creating_or_mutating_it(tmp_path, monkeypatch):
    test_root = tmp_path / "test-root"
    test_root.mkdir()
    sentinel = tmp_path / "live" / "kanban.db"
    sentinel.parent.mkdir()
    sentinel.write_bytes(b"do not mutate")

    monkeypatch.setenv("HERMES_TEST_MODE", "1")
    monkeypatch.setenv("HERMES_KANBAN_TEST_ROOT", str(test_root))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(sentinel))

    with pytest.raises(PermissionError, match="HERMES_KANBAN_TEST_ROOT"):
        kbc.init_db()
    assert sentinel.read_bytes() == b"do not mutate"


def test_test_harness_accepts_board_beneath_its_tmp_root(tmp_path, monkeypatch):
    test_root = tmp_path / "test-root"
    test_root.mkdir()
    board = test_root / "kanban.db"

    monkeypatch.setenv("HERMES_TEST_MODE", "1")
    monkeypatch.setenv("HERMES_KANBAN_TEST_ROOT", str(test_root))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(board))

    assert kbc.init_db() == board
    assert board.exists()


def test_explicit_owned_integration_override_is_honored(tmp_path, monkeypatch):
    board = tmp_path / "owned-integration" / "kanban.db"
    monkeypatch.setenv("HERMES_TEST_MODE", "1")
    monkeypatch.setenv("HERMES_KANBAN_TEST_ROOT", str(tmp_path / "other-root"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(board))
    monkeypatch.setenv("HERMES_KANBAN_ALLOW_LIVE_IN_TESTS", "1")

    assert kbc.init_db() == board
    assert board.exists()
