from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_core.tools import FileWriteTool, ToolUseContext, get_tool


@pytest.mark.write
class TestFileWriteTool:
    def tool(self) -> FileWriteTool:
        return get_tool("Write")  # type: ignore[return-value]

    @pytest.mark.asyncio
    async def test_write_new_file(self, tmp_path: Path) -> None:
        f = tmp_path / "new.txt"
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "content": "created\n"}),
            ToolUseContext(),
        )
        assert not result.is_error
        assert f.read_text() == "created\n"

    @pytest.mark.asyncio
    async def test_create_parent_directories(self, tmp_path: Path) -> None:
        f = tmp_path / "a" / "b" / "c.txt"
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "content": "deep\n"}),
            ToolUseContext(),
        )
        assert not result.is_error
        assert f.read_text() == "deep\n"

    @pytest.mark.asyncio
    async def test_overwrite_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "exist.txt"
        f.write_text("old\n")
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "content": "new\n"}),
            ToolUseContext(),
        )
        assert not result.is_error
        assert f.read_text() == "new\n"

    @pytest.mark.asyncio
    async def test_write_to_directory_fails(self, tmp_path: Path) -> None:
        d = tmp_path / "adir"
        d.mkdir()
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": str(d), "content": "x"}),
            ToolUseContext(),
        )
        assert result.is_error
        assert "directory" in str(result.content)

    @pytest.mark.asyncio
    async def test_reject_write_when_file_modified_externally(self, tmp_path: Path) -> None:
        f = tmp_path / "stale_write.txt"
        f.write_text("original\n")
        ctx = ToolUseContext()
        ctx.read_file_state[str(f)] = {"mtime_ns": 0, "size": 0}
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "content": "overwritten\n"}),
            ctx,
        )
        assert result.is_error
        assert "modified since last read" in str(result.content)
        assert f.read_text() == "original\n"

    @pytest.mark.asyncio
    async def test_allow_write_when_mtime_matches(self, tmp_path: Path) -> None:
        f = tmp_path / "fresh_write.txt"
        f.write_text("old\n")
        ctx = ToolUseContext()
        ctx.read_file_state[str(f)] = {
            "mtime_ns": f.stat().st_mtime_ns,
            "size": f.stat().st_size,
        }
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "content": "new\n"}),
            ctx,
        )
        assert not result.is_error
        assert f.read_text() == "new\n"

    @pytest.mark.asyncio
    async def test_overwrite_preserves_permissions(self, tmp_path: Path) -> None:
        f = tmp_path / "perm.txt"
        f.write_text("original\n")
        if os.name == "nt":
            f.chmod(0o444)
        else:
            os.chmod(str(f), 0o755)
        original_mode = f.stat().st_mode
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "content": "changed\n"}),
            ToolUseContext(),
        )
        assert not result.is_error
        assert f.stat().st_mode == original_mode

    @pytest.mark.asyncio
    async def test_edit_preserves_permissions(self, tmp_path: Path) -> None:
        f = tmp_path / "perm_edit.txt"
        content = "hello world\n"
        f.write_text(content)
        if os.name == "nt":
            f.chmod(0o444)
        else:
            os.chmod(str(f), 0o755)
        original_mode = f.stat().st_mode
        tool = get_tool("Edit")
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
        assert f.stat().st_mode == original_mode

    @pytest.mark.asyncio
    async def test_write_empty_content(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.txt"
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "content": ""}),
            ToolUseContext(),
        )
        assert not result.is_error
        assert f.exists()
        assert f.read_text() == ""

    @pytest.mark.asyncio
    async def test_write_large_content_not_cached(self, tmp_path: Path) -> None:
        f = tmp_path / "large.txt"
        large = "x" * (256 * 1024 + 1)
        tool = self.tool()
        ctx = ToolUseContext()
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "content": large}),
            ctx,
        )
        assert not result.is_error
        assert ctx.read_file_state[str(f)]["content"] is None
