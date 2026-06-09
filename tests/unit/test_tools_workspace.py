from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.tools import (
    BashTool,
    FileEditTool,
    FileReadTool,
    FileWriteTool,
    GlobTool,
    ToolUseContext,
)


@pytest.mark.workspace
class TestWorkspaceBoundary:
    @pytest.mark.asyncio
    async def test_read_outside_workspace_rejected(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret\n")
        ctx = ToolUseContext(workspace_root=str(workspace))
        tool = FileReadTool()
        result = await tool.call(
            tool.validate_input({"file_path": str(outside)}),
            ctx,
        )
        assert result.is_error
        assert "outside workspace" in str(result.content)

    @pytest.mark.asyncio
    async def test_write_outside_workspace_rejected(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "outside.txt"
        ctx = ToolUseContext(workspace_root=str(workspace))
        tool = FileWriteTool()
        result = await tool.call(
            tool.validate_input({"file_path": str(outside), "content": "x"}),
            ctx,
        )
        assert result.is_error
        assert "outside workspace" in str(result.content)

    @pytest.mark.asyncio
    async def test_edit_outside_workspace_rejected(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("hello\n")
        ctx = ToolUseContext(workspace_root=str(workspace))
        tool = FileEditTool()
        result = await tool.call(
            tool.validate_input({"file_path": str(outside), "old_string": "h", "new_string": "x"}),
            ctx,
        )
        assert result.is_error
        assert "outside workspace" in str(result.content)

    @pytest.mark.asyncio
    async def test_glob_outside_workspace_rejected(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        outside = tmp_path / "other"
        outside.mkdir()
        ctx = ToolUseContext(workspace_root=str(workspace))
        tool = GlobTool()
        result = await tool.call(
            tool.validate_input({"pattern": "*", "path": str(outside)}),
            ctx,
        )
        assert result.is_error
        assert "outside workspace" in str(result.content)

    @pytest.mark.asyncio
    async def test_file_inside_workspace_allowed(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        f = workspace / "allowed.txt"
        f.write_text("ok\n")
        ctx = ToolUseContext(workspace_root=str(workspace))
        tool = FileReadTool()
        result = await tool.call(
            tool.validate_input({"file_path": str(f)}),
            ctx,
        )
        assert "ok" in str(result.content)

    @pytest.mark.asyncio
    async def test_bash_cwd_outside_workspace_rejected(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        ctx = ToolUseContext(workspace_root=str(workspace))
        tool = BashTool()
        result = await tool.call(
            tool.validate_input({"command": "echo hello", "working_dir": str(tmp_path)}),
            ctx,
        )
        assert result.is_error
        assert "outside workspace" in str(result.content)

    @pytest.mark.asyncio
    async def test_bash_cwd_inside_workspace_allowed(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        ctx = ToolUseContext(workspace_root=str(workspace))
        tool = BashTool()
        result = await tool.call(
            tool.validate_input({"command": "echo hello", "working_dir": str(workspace)}),
            ctx,
        )
        assert not result.is_error
        assert "hello" in str(result.content)
