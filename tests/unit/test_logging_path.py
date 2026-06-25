"""Unit tests for logging path computation (Item 12)."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from agent_core.logging import path as lpath
from agent_core.persistence import path as ppath


@pytest.mark.agent
class TestLogPath:
    def test_logs_root_default_vs_override(self, tmp_path) -> None:
        assert lpath.logs_root().endswith(os.path.join(".agent", "logs"))
        override = str(tmp_path / "mylogs")
        assert lpath.logs_root(override) == os.path.abspath(override)

    def test_per_session_layout(self, tmp_path) -> None:
        root = str(tmp_path / "logs")
        p = lpath.log_file_path("ph", "sess1", rolling="per_session", log_dir=root)
        assert p == os.path.join(os.path.abspath(root), "ph", "sess1.jsonl")

    def test_per_day_layout(self, tmp_path) -> None:
        root = str(tmp_path / "logs")
        now = datetime(2026, 6, 25, tzinfo=timezone.utc)
        p = lpath.log_file_path("ph", "sess1", rolling="per_day", log_dir=root, now=now)
        assert p == os.path.join(os.path.abspath(root), "ph", "2026-06-25", "sess1.jsonl")

    def test_falls_back_to_trace_id(self) -> None:
        p = lpath.log_file_path("ph", None, log_dir="/logs", trace_id="tr9")
        assert p.endswith(os.path.join("ph", "tr9.jsonl"))

    def test_isolation_from_persistence_root(self) -> None:
        # Log root and persistence project root must never coincide.
        assert lpath.AGENT_LOGS_ROOT != ppath.project_dir("ph")
        assert "logs" in lpath.AGENT_LOGS_ROOT
        assert "projects" in ppath.project_dir("ph")

    def test_list_log_files_empty_when_missing(self, tmp_path) -> None:
        assert lpath.list_log_files("nope", log_dir=str(tmp_path)) == []

    def test_list_log_files_sorted_by_mtime(self, tmp_path) -> None:
        base = tmp_path / "ph"
        base.mkdir(parents=True)
        f1 = base / "a.jsonl"
        f2 = base / "b.jsonl"
        f1.write_text("{}\n")
        f2.write_text("{}\n")
        os.utime(f1, (1000, 1000))
        os.utime(f2, (2000, 2000))
        files = lpath.list_log_files("ph", log_dir=str(tmp_path))
        assert files[0].endswith("b.jsonl")
        assert files[1].endswith("a.jsonl")

    def test_list_log_files_walks_per_day_subdir(self, tmp_path) -> None:
        day = tmp_path / "ph" / "2026-06-25"
        day.mkdir(parents=True)
        (day / "s.jsonl").write_text("{}\n")
        files = lpath.list_log_files("ph", log_dir=str(tmp_path))
        assert len(files) == 1 and files[0].endswith("s.jsonl")
