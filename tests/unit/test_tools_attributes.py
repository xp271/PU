from __future__ import annotations

import pytest

from agent_core.tools import (
    BashTool,
    FileEditTool,
    FileReadTool,
    FileWriteTool,
    GlobTool,
    GrepTool,
)


@pytest.mark.attributes
class TestToolClassAttributes:
    def test_file_read_tool_attributes(self) -> None:
        tool = FileReadTool()
        assert tool.name == "Read"
        assert tool.is_concurrency_safe is True
        assert tool.is_read_only is True
        assert tool.is_destructive is False

    def test_glob_tool_attributes(self) -> None:
        tool = GlobTool()
        assert tool.name == "Glob"
        assert tool.is_concurrency_safe is True
        assert tool.is_read_only is True

    def test_file_edit_tool_attributes(self) -> None:
        tool = FileEditTool()
        assert tool.name == "Edit"
        assert tool.is_concurrency_safe is False
        assert tool.is_read_only is False
        assert tool.is_destructive is True

    def test_file_write_tool_attributes(self) -> None:
        tool = FileWriteTool()
        assert tool.name == "Write"
        assert tool.is_concurrency_safe is False
        assert tool.is_read_only is False
        assert tool.is_destructive is True

    def test_bash_tool_attributes(self) -> None:
        tool = BashTool()
        assert tool.name == "Bash"
        assert tool.is_concurrency_safe is False
        assert tool.is_read_only is False
        assert tool.is_destructive is True

    def test_grep_tool_attributes(self) -> None:
        tool = GrepTool()
        assert tool.name == "Grep"
        assert tool.is_concurrency_safe is True
        assert tool.is_read_only is True
