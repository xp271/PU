"""Unit tests for persistence/path.py."""

import os
from unittest.mock import patch

from agent_core.persistence.path import (
    compute_project_hash,
    session_jsonl_path,
    sidechain_dir,
    sidechain_jsonl_path,
    project_dir,
    list_session_files,
)


class TestComputeProjectHash:
    def test_deterministic(self):
        a = compute_project_hash("/foo/bar")
        b = compute_project_hash("/foo/bar")
        assert a == b
        assert len(a) == 16

    def test_different_paths_different_hash(self):
        a = compute_project_hash("/foo/bar")
        b = compute_project_hash("/baz/qux")
        assert a != b

    def test_none_workspace_uses_cwd(self):
        h = compute_project_hash(None)
        assert len(h) == 16

    def test_relative_path_absolutized(self):
        h = compute_project_hash(".")
        assert len(h) == 16


class TestPathFunctions:
    def test_project_dir(self):
        d = project_dir("abc123")
        assert d.endswith(os.path.join(".agent", "sessions", "abc123"))

    def test_session_jsonl_path(self):
        p = session_jsonl_path("abc123", "session-1")
        assert p.endswith(os.path.join("abc123", "session-1.jsonl"))

    def test_sidechain_dir(self):
        d = sidechain_dir("abc123", "session-1")
        assert d.endswith(os.path.join("abc123", "session-1"))

    def test_sidechain_jsonl_path(self):
        p = sidechain_jsonl_path("abc123", "session-1", "agent-1")
        assert p.endswith(os.path.join("abc123", "session-1", "agent-1.jsonl"))


class TestListSessionFiles:
    def test_empty_for_nonexistent_dir(self, tmp_path):
        h = compute_project_hash(str(tmp_path / "nonexistent"))
        result = list_session_files(h)
        assert result == []

    def test_returns_sorted_by_mtime(self, tmp_path):
        import json, time

        d = tmp_path / "proj"
        d.mkdir()
        h = "fakehash123"
        # Patch project_dir to point to our temp dir
        with patch(
            "agent_core.persistence.path.project_dir", return_value=str(d)
        ):
            # Create session files
            for i, sid in enumerate(["c", "a", "b"]):
                fpath = d / f"{sid}.jsonl"
                fpath.write_text(json.dumps({"test": i}))
                # Stagger mtime
                os.utime(str(fpath), (1000 + i, 1000 + i))

            result = list_session_files(h)
            assert result == ["b", "a", "c"]  # most recent first

    def test_ignores_non_jsonl_files(self, tmp_path):
        d = tmp_path / "proj2"
        d.mkdir()
        h = "fakehash456"
        (d / "notes.txt").write_text("hello")
        (d / "session.jsonl").write_text("{}")

        with patch(
            "agent_core.persistence.path.project_dir", return_value=str(d)
        ):
            result = list_session_files(h)
            assert "session" in result
            assert "notes" not in result  # .txt not .jsonl
