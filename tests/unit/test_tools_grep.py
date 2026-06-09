from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent_core.tools import GrepTool, ToolUseContext, get_tool


@pytest.mark.grep
class TestGrepTool:
    def tool(self) -> GrepTool:
        return get_tool("Grep")  # type: ignore[return-value]

    def test_validate_input(self) -> None:
        tool = self.tool()
        inp = tool.validate_input({
            "pattern": "hello",
            "path": ".",
            "output_mode": "content",
            "head_limit": 50,
        })
        assert inp.pattern == "hello"
        assert inp.path == "."
        assert inp.output_mode == "content"
        assert inp.head_limit == 50
        assert inp.ignore_case is False
        assert inp.multiline is False

    def test_input_schema(self) -> None:
        schema = GrepTool.input_schema()
        assert schema["type"] == "object"
        assert "pattern" in schema["properties"]
        assert "output_mode" in schema["properties"]

    @pytest.mark.asyncio
    async def test_files_with_matches_dedup(self, tmp_path: Path) -> None:
        if not shutil.which("rg"):
            pytest.skip("ripgrep not installed")
        (tmp_path / "a.py").write_text("hello\nworld\n")
        (tmp_path / "b.py").write_text("hello\nfoo\n")
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({
                "pattern": "hello",
                "path": str(tmp_path),
                "output_mode": "files_with_matches",
            }),
            ToolUseContext(),
        )
        assert not result.is_error
        lines = str(result.content).strip().split("\n")
        assert len(lines) == 2

    @pytest.mark.asyncio
    async def test_truncation_notice_without_offset(self, tmp_path: Path) -> None:
        if not shutil.which("rg"):
            pytest.skip("ripgrep not installed")
        for i in range(120):
            (tmp_path / f"f{i}.py").write_text(f"match_{i}\n")
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({
                "pattern": "match_",
                "path": str(tmp_path),
                "output_mode": "content",
                "head_limit": 5,
            }),
            ToolUseContext(),
        )
        assert result.truncated
        assert result.truncation_notice is not None
        assert "of 120 results" in result.truncation_notice

    @pytest.mark.asyncio
    async def test_truncation_notice_with_offset(self, tmp_path: Path) -> None:
        if not shutil.which("rg"):
            pytest.skip("ripgrep not installed")
        for i in range(60):
            (tmp_path / f"f{i}.py").write_text(f"match_{i}\n")
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({
                "pattern": "match_",
                "path": str(tmp_path),
                "output_mode": "content",
                "offset": 10,
                "head_limit": 5,
            }),
            ToolUseContext(),
        )
        assert result.truncated
        assert result.truncation_notice is not None
        assert "offset" in result.truncation_notice
        assert "total 60" in result.truncation_notice

    @pytest.mark.asyncio
    async def test_no_truncation_notice_when_within_limit(self, tmp_path: Path) -> None:
        if not shutil.which("rg"):
            pytest.skip("ripgrep not installed")
        (tmp_path / "a.py").write_text("hello\n")
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({
                "pattern": "hello",
                "path": str(tmp_path),
                "output_mode": "content",
                "head_limit": 100,
            }),
            ToolUseContext(),
        )
        assert not result.truncated
        assert result.truncation_notice is None

    @pytest.mark.asyncio
    async def test_count_output_mode(self, tmp_path: Path) -> None:
        if not shutil.which("rg"):
            pytest.skip("ripgrep not installed")
        (tmp_path / "a.py").write_text("hello\nworld\n")
        (tmp_path / "b.py").write_text("hello\n")
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({
                "pattern": "hello",
                "path": str(tmp_path),
                "output_mode": "count",
            }),
            ToolUseContext(),
        )
        assert not result.is_error
        assert str(result.content).strip() == "2"

    @pytest.mark.asyncio
    async def test_ignore_case(self, tmp_path: Path) -> None:
        if not shutil.which("rg"):
            pytest.skip("ripgrep not installed")
        (tmp_path / "a.py").write_text("Hello\n")
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({
                "pattern": "hello",
                "path": str(tmp_path),
                "output_mode": "files_with_matches",
                "ignore_case": True,
            }),
            ToolUseContext(),
        )
        assert not result.is_error
        assert "a.py" in str(result.content)

    @pytest.mark.asyncio
    async def test_context_around(self, tmp_path: Path) -> None:
        if not shutil.which("rg"):
            pytest.skip("ripgrep not installed")
        (tmp_path / "a.py").write_text("line1\nhello\nline3\n")
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({
                "pattern": "hello",
                "path": str(tmp_path),
                "output_mode": "content",
                "context_around": 1,
            }),
            ToolUseContext(),
        )
        assert not result.is_error

    @pytest.mark.asyncio
    async def test_glob_filter(self, tmp_path: Path) -> None:
        if not shutil.which("rg"):
            pytest.skip("ripgrep not installed")
        (tmp_path / "a.py").write_text("hello\n")
        (tmp_path / "b.txt").write_text("hello\n")
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({
                "pattern": "hello",
                "path": str(tmp_path),
                "output_mode": "files_with_matches",
                "glob": "*.py",
            }),
            ToolUseContext(),
        )
        assert not result.is_error
        lines = str(result.content).strip().split("\n")
        assert any("a.py" in ln for ln in lines)
        assert not any("b.txt" in ln for ln in lines)

    @pytest.mark.asyncio
    async def test_files_with_matches_empty_result(self, tmp_path: Path) -> None:
        if not shutil.which("rg"):
            pytest.skip("ripgrep not installed")
        (tmp_path / "a.py").write_text("hello\n")
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({
                "pattern": "nonexistent_pattern_xyz",
                "path": str(tmp_path),
                "output_mode": "files_with_matches",
            }),
            ToolUseContext(),
        )
        assert not result.is_error
        assert "[no matches]" in str(result.content)

    @pytest.mark.asyncio
    async def test_multiline_search(self, tmp_path: Path) -> None:
        if not shutil.which("rg"):
            pytest.skip("ripgrep not installed")
        (tmp_path / "a.py").write_text("hello\nworld\n")
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({
                "pattern": "hello\nworld",
                "path": str(tmp_path),
                "output_mode": "files_with_matches",
                "multiline": True,
            }),
            ToolUseContext(),
        )
        assert not result.is_error
        content = str(result.content)
        if "[no matches]" in content:
            pytest.skip("ripgrep built without PCRE2, multiline mode not available")
