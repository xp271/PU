from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_core.tools import GlobTool, ToolUseContext, get_tool


@pytest.mark.glob
class TestGlobTool:
    def tool(self) -> GlobTool:
        return get_tool("Glob")  # type: ignore[return-value]

    @pytest.mark.asyncio
    async def test_glob_finds_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        (tmp_path / "c.txt").write_text("")
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"pattern": "*.py", "path": str(tmp_path)}),
            ToolUseContext(),
        )
        content = str(result.content)
        assert "a.py" in content
        assert "b.py" in content
        assert "c.txt" not in content

    @pytest.mark.asyncio
    async def test_glob_path_not_found(self) -> None:
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"pattern": "*", "path": "/nonexistent"}),
            ToolUseContext(),
        )
        assert result.is_error

    @pytest.mark.asyncio
    async def test_glob_not_directory(self, tmp_path: Path) -> None:
        f = tmp_path / "file.txt"
        f.write_text("")
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"pattern": "*", "path": str(f)}),
            ToolUseContext(),
        )
        assert result.is_error

    def test_input_schema(self) -> None:
        schema = GlobTool.input_schema()
        assert schema["type"] == "object"
        assert "pattern" in schema["properties"]

    @pytest.mark.asyncio
    async def test_truncation_notice_shows_correct_total(self, tmp_path: Path) -> None:
        base = tmp_path / "many"
        base.mkdir()
        for i in range(150):
            (base / f"file_{i:04d}.py").write_text("")
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"pattern": "*.py", "path": str(base)}),
            ToolUseContext(),
        )
        assert result.truncated
        notice = result.truncation_notice
        assert notice is not None
        assert "of 150 results" in notice

    @pytest.mark.asyncio
    async def test_no_truncation_notice_when_under_limit(self, tmp_path: Path) -> None:
        base = tmp_path / "few"
        base.mkdir()
        for i in range(5):
            (base / f"f{i}.py").write_text("")
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"pattern": "*.py", "path": str(base)}),
            ToolUseContext(),
        )
        assert not result.truncated
        assert result.truncation_notice is None

    @pytest.mark.asyncio
    async def test_hidden_files_filtered(self, tmp_path: Path) -> None:
        base = tmp_path / "hidden_test"
        base.mkdir()
        (base / ".hidden.py").write_text("")
        (base / "visible.py").write_text("")
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"pattern": "*.py", "path": str(base)}),
            ToolUseContext(),
        )
        content = str(result.content)
        assert "visible.py" in content
        assert ".hidden.py" not in content

    @pytest.mark.asyncio
    async def test_dot_pattern_includes_hidden(self, tmp_path: Path) -> None:
        base = tmp_path / "dot_test"
        base.mkdir()
        (base / ".hidden_file").write_text("")
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"pattern": ".*", "path": str(base)}),
            ToolUseContext(),
        )
        content = str(result.content)
        assert ".hidden_file" in content

    @pytest.mark.asyncio
    async def test_results_sorted_by_mtime_descending(self, tmp_path: Path) -> None:
        base = tmp_path / "sorted"
        base.mkdir()
        a = base / "a.py"
        b = base / "b.py"
        a.write_text("")
        time.sleep(0.05)
        b.write_text("")
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"pattern": "*.py", "path": str(base)}),
            ToolUseContext(),
        )
        lines = str(result.content).strip().split("\n")
        assert len(lines) == 2
        assert "b.py" in lines[0]
