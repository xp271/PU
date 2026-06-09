from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_core.tools import FileEditTool, ToolUseContext, get_tool


@pytest.mark.edit
class TestFileEditTool:
    def tool(self) -> FileEditTool:
        return get_tool("Edit")  # type: ignore[return-value]

    @pytest.mark.asyncio
    async def test_simple_replace(self, tmp_path: Path) -> None:
        f = tmp_path / "edit.txt"
        content = "hello world\n"
        f.write_text(content)
        tool = self.tool()
        ctx = ToolUseContext()
        ctx.read_file_state[str(f)] = {
            "mtime_ns": f.stat().st_mtime_ns,
            "size": f.stat().st_size,
            "content": content,
            "total_lines": 1,
            "offset": 0,
            "limit": 2000,
            "byte_truncated": False,
        }
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "old_string": "hello", "new_string": "hi"}),
            ctx,
        )
        assert not result.is_error
        content = str(result.content)
        assert "-hello" in content
        assert "+hi" in content
        assert f.read_text() == "hi world\n"

    @pytest.mark.asyncio
    async def test_old_string_not_found(self, tmp_path: Path) -> None:
        f = tmp_path / "edit.txt"
        content = "abc\n"
        f.write_text(content)
        tool = self.tool()
        ctx = ToolUseContext()
        ctx.read_file_state[str(f)] = {
            "mtime_ns": f.stat().st_mtime_ns,
            "size": f.stat().st_size,
            "content": content,
            "total_lines": 1,
            "offset": 0,
            "limit": 2000,
            "byte_truncated": False,
        }
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "old_string": "xyz", "new_string": "nope"}),
            ctx,
        )
        assert result.is_error
        assert "old_string not found" in str(result.content)

    @pytest.mark.asyncio
    async def test_multiple_matches(self, tmp_path: Path) -> None:
        f = tmp_path / "edit.txt"
        content = "dup\ndup\n"
        f.write_text(content)
        tool = self.tool()
        ctx = ToolUseContext()
        ctx.read_file_state[str(f)] = {
            "mtime_ns": f.stat().st_mtime_ns,
            "size": f.stat().st_size,
            "content": content,
            "total_lines": 2,
            "offset": 0,
            "limit": 2000,
            "byte_truncated": False,
        }
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "old_string": "dup", "new_string": "x"}),
            ctx,
        )
        assert result.is_error
        assert "multiple locations" in str(result.content)

    @pytest.mark.asyncio
    async def test_file_not_found(self) -> None:
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": "/nonexistent", "old_string": "a", "new_string": "b"}),
            ToolUseContext(),
        )
        assert result.is_error
        assert "File not found" in str(result.content)

    @pytest.mark.asyncio
    async def test_directory_not_file(self, tmp_path: Path) -> None:
        d = tmp_path / "subdir"
        d.mkdir()
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": str(d), "old_string": "a", "new_string": "b"}),
            ToolUseContext(),
        )
        assert result.is_error
        assert "directory" in str(result.content)

    @pytest.mark.asyncio
    async def test_reject_edit_when_file_modified_externally(self, tmp_path: Path) -> None:
        f = tmp_path / "stale.txt"
        f.write_text("original\n")
        ctx = ToolUseContext()
        ctx.read_file_state[str(f)] = {"mtime_ns": 0, "size": 0, "content": "original\n"}
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "old_string": "original", "new_string": "changed"}),
            ctx,
        )
        assert result.is_error
        assert "modified since last read" in str(result.content)
        assert f.read_text() == "original\n"

    @pytest.mark.asyncio
    async def test_allow_edit_when_mtime_matches(self, tmp_path: Path) -> None:
        f = tmp_path / "fresh.txt"
        content = "hello world\n"
        f.write_text(content)
        ctx = ToolUseContext()
        ctx.read_file_state[str(f)] = {
            "mtime_ns": f.stat().st_mtime_ns,
            "size": f.stat().st_size,
            "content": content,
        }
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "old_string": "hello", "new_string": "hi"}),
            ctx,
        )
        assert not result.is_error
        assert f.read_text() == "hi world\n"

    @pytest.mark.asyncio
    async def test_delete_with_empty_new_string(self, tmp_path: Path) -> None:
        f = tmp_path / "edit.txt"
        content = "hello world\n"
        f.write_text(content)
        tool = self.tool()
        ctx = ToolUseContext()
        ctx.read_file_state[str(f)] = {
            "mtime_ns": f.stat().st_mtime_ns,
            "size": f.stat().st_size,
            "content": content,
        }
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "old_string": "hello ", "new_string": ""}),
            ctx,
        )
        assert not result.is_error
        assert f.read_text() == "world\n"

    @pytest.mark.asyncio
    async def test_edit_returns_metadata(self, tmp_path: Path) -> None:
        f = tmp_path / "edit.txt"
        content = "hello world\n"
        f.write_text(content)
        tool = self.tool()
        ctx = ToolUseContext()
        ctx.read_file_state[str(f)] = {
            "mtime_ns": f.stat().st_mtime_ns,
            "size": f.stat().st_size,
            "content": content,
        }
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "old_string": "hello", "new_string": "hi"}),
            ctx,
        )
        assert not result.is_error
        assert result.metadata is not None
        assert "changed_hunks" in result.metadata
        assert "lines_added" in result.metadata
        assert "lines_removed" in result.metadata

    @pytest.mark.asyncio
    async def test_edit_without_prior_read(self, tmp_path: Path) -> None:
        f = tmp_path / "edit.txt"
        f.write_text("hello\n")
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "old_string": "hello", "new_string": "hi"}),
            ToolUseContext(),
        )
        assert result.is_error
        assert "must be read before editing" in str(result.content)
