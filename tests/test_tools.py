from __future__ import annotations

import asyncio
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, Field

from agent_core.tools import (
    BashTool,
    FileEditTool,
    FileReadTool,
    FileWriteTool,
    GlobTool,
    GrepTool,
    Tool,
    ToolCallResult,
    ToolUseContext,
    get_tool,
    register_tool,
    registered_tools,
)
from agent_core.tools._io import file_fingerprint, safe_decode_bytes
from agent_core.tools.registry import _registry as _raw_registry


class TestToolRegistry:
    def test_register_tool_decorator(self) -> None:
        @register_tool
        class _TestTool(Tool):
            name: ClassVar[str] = "TestRegistry"
            async def call(self, input, context):
                return ToolCallResult(content="ok")

            def validate_input(self, input):
                return {}

            @classmethod
            def input_schema(cls):
                return {"type": "object"}

        assert "TestRegistry" in _raw_registry
        assert _raw_registry["TestRegistry"] is _TestTool

    def test_register_tool_requires_name(self) -> None:
        with pytest.raises(TypeError, match="must define a 'name'"):
            @register_tool
            class _BadTool(Tool):
                async def call(self, input, context):
                    return ToolCallResult(content="ok")

                def validate_input(self, input):
                    return {}

    def test_get_tool_returns_instance(self) -> None:
        tool = get_tool("TestRegistry")
        assert isinstance(tool, Tool)
        assert tool.name == "TestRegistry"

    def test_get_tool_unknown_raises_keyerror(self) -> None:
        with pytest.raises(KeyError, match="not registered"):
            get_tool("NonExistentTool")

    def test_registered_tools_returns_copy(self) -> None:
        registered = registered_tools()
        assert "TestRegistry" in registered

    def test_get_tool_returns_same_instance(self) -> None:
        t1 = get_tool("TestRegistry")
        t2 = get_tool("TestRegistry")
        assert t1 is t2

    def test_re_register_evicts_cached_instance(self) -> None:
        old_tool = get_tool("TestRegistry")

        @register_tool
        class _TestToolRereg(Tool):
            name: ClassVar[str] = "TestRegistry"
            async def call(self, input, context):
                return ToolCallResult(content="re-registered")

            def validate_input(self, input):
                return {}

            @classmethod
            def input_schema(cls):
                return {"type": "object"}

        new_tool = get_tool("TestRegistry")
        assert type(new_tool) is _TestToolRereg
        assert new_tool is not old_tool


class TestToolBase:
    def test_tool_classvars_defaults(self) -> None:
        class _Minimal(Tool):
            name: ClassVar[str] = "Minimal"
            async def call(self, input, context):
                return ToolCallResult(content="ok")

            def validate_input(self, input):
                return {}

            @classmethod
            def input_schema(cls):
                return {"type": "object"}

        assert _Minimal.is_concurrency_safe is False
        assert _Minimal.is_read_only is False
        assert _Minimal.is_destructive is False

    def test_output_schema_default(self) -> None:
        class _T(Tool):
            name: ClassVar[str] = "T"
            async def call(self, input, context):
                return ToolCallResult(content="ok")

            def validate_input(self, input):
                return {}

            @classmethod
            def input_schema(cls):
                return {"type": "object"}

        schema = _T.output_schema()
        assert isinstance(schema, dict)
        assert "properties" in schema


class TestToolUseContext:
    def test_default_values(self) -> None:
        ctx = ToolUseContext()
        assert isinstance(ctx.abort_signal, asyncio.Event)
        assert ctx.read_file_state == {}
        assert ctx.messages == []
        assert ctx.agent_id == ""
        assert ctx.workspace_root is None

    def test_custom_values(self) -> None:
        event = asyncio.Event()
        ctx = ToolUseContext(
            abort_signal=event,
            read_file_state={"f": {}},
            messages=[{"role": "user"}],
            agent_id="abc-123",
            workspace_root="/workspace",
        )
        assert ctx.abort_signal is event
        assert ctx.read_file_state == {"f": {}}
        assert ctx.messages == [{"role": "user"}]
        assert ctx.agent_id == "abc-123"
        assert ctx.workspace_root == "/workspace"


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


class TestFileReadTool:
    def tool(self) -> FileReadTool:
        return get_tool("Read")  # type: ignore[return-value]

    def test_validate_input_parses_valid(self) -> None:
        tool = self.tool()
        result = tool.validate_input({"file_path": "/tmp/test.txt", "offset": 5, "limit": 100})
        assert result.file_path == "/tmp/test.txt"
        assert result.offset == 5
        assert result.limit == 100

    def test_input_schema_has_required_fields(self) -> None:
        schema = FileReadTool.input_schema()
        assert schema["type"] == "object"
        assert "file_path" in schema["properties"]
        assert "offset" in schema["properties"]
        assert "limit" in schema["properties"]

    @pytest.mark.asyncio
    async def test_read_text_file(self, tmp_path: Path) -> None:
        f = tmp_path / "hello.txt"
        f.write_text("line1\nline2\nline3\n")
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": str(f)}),
            ToolUseContext(),
        )
        assert not result.is_error
        assert "line1" in str(result.content)
        assert "line2" in str(result.content)
        assert "line3" in str(result.content)

    @pytest.mark.asyncio
    async def test_read_with_offset_and_limit(self, tmp_path: Path) -> None:
        f = tmp_path / "numbers.txt"
        lines = [f"line{i}\n" for i in range(20)]
        f.write_text("".join(lines))
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "offset": 5, "limit": 3}),
            ToolUseContext(),
        )
        assert not result.is_error
        content = str(result.content)
        assert "line5" in content

    @pytest.mark.asyncio
    async def test_read_file_not_found(self) -> None:
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": "/nonexistent/path/file.txt"}),
            ToolUseContext(),
        )
        assert result.is_error
        assert "File not found" in str(result.content)

    @pytest.mark.asyncio
    async def test_read_state_dedup_mtime_match(self, tmp_path: Path) -> None:
        f = tmp_path / "dedup.txt"
        f.write_text("hello world\n")
        tool = self.tool()
        ctx = ToolUseContext()
        fp = file_fingerprint(f)
        ctx.read_file_state[str(f)] = {
            "mtime_ns": fp["mtime_ns"],
            "size": fp["size"],
            "content": "hello world\n",
            "total_lines": 1,
            "offset": 0,
            "limit": 2000,
            "byte_truncated": False,
        }

        result = await tool.call(
            tool.validate_input({"file_path": str(f)}),
            ctx,
        )
        assert not result.is_error
        assert "hello world" in str(result.content)

    @pytest.mark.asyncio
    async def test_re_read_when_mtime_changed(self, tmp_path: Path) -> None:
        f = tmp_path / "changed.txt"
        f.write_text("original\n")
        tool = self.tool()
        ctx = ToolUseContext()
        ctx.read_file_state[str(f)] = {"offset": 0, "limit": 2000, "mtime_ns": 0, "size": 0}

        result = await tool.call(
            tool.validate_input({"file_path": str(f)}),
            ctx,
        )
        assert "original" in str(result.content)

    @pytest.mark.asyncio
    async def test_re_read_when_offset_differs(self, tmp_path: Path) -> None:
        f = tmp_path / "offset.txt"
        lines = [f"line{i}\n" for i in range(20)]
        f.write_text("".join(lines))
        tool = self.tool()
        ctx = ToolUseContext()
        fp = file_fingerprint(f)
        ctx.read_file_state[str(f)] = {
            "mtime_ns": fp["mtime_ns"],
            "size": fp["size"],
            "content": "".join(lines),
            "total_lines": 20,
            "offset": 0,
            "limit": 5,
            "byte_truncated": False,
        }

        result = await tool.call(
            tool.validate_input({"file_path": str(f), "offset": 10, "limit": 3}),
            ctx,
        )
        assert "line10" in str(result.content)

    @pytest.mark.asyncio
    async def test_dedup_when_range_fully_covered(self, tmp_path: Path) -> None:
        f = tmp_path / "cover.txt"
        lines = [f"line{i}\n" for i in range(30)]
        full_text = "".join(lines)
        f.write_text(full_text)
        tool = self.tool()
        ctx = ToolUseContext()
        fp = file_fingerprint(f)
        ctx.read_file_state[str(f)] = {
            "mtime_ns": fp["mtime_ns"],
            "size": fp["size"],
            "content": full_text,
            "total_lines": 30,
            "offset": 0,
            "limit": 30,
            "byte_truncated": False,
        }
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "offset": 5, "limit": 10}),
            ctx,
        )
        assert "line5" in str(result.content)

    @pytest.mark.asyncio
    async def test_re_read_when_range_exceeds_previous(self, tmp_path: Path) -> None:
        f = tmp_path / "exceed.txt"
        lines = [f"line{i}\n" for i in range(30)]
        f.write_text("".join(lines))
        tool = self.tool()
        ctx = ToolUseContext()
        ctx.read_file_state[str(f)] = {
            "mtime_ns": 0,
            "size": 0,
            "content": None,
            "total_lines": None,
            "offset": 0,
            "limit": 10,
            "byte_truncated": False,
        }
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "offset": 0, "limit": 20}),
            ctx,
        )
        assert "line15" in str(result.content)

    @pytest.mark.asyncio
    async def test_read_state_stores_fingerprint(self, tmp_path: Path) -> None:
        f = tmp_path / "mtime.txt"
        f.write_text("content\n")
        tool = self.tool()
        ctx = ToolUseContext()
        await tool.call(
            tool.validate_input({"file_path": str(f)}),
            ctx,
        )
        state = ctx.read_file_state.get(str(f))
        assert state is not None
        assert "mtime_ns" in state
        assert state["mtime_ns"] == f.stat().st_mtime_ns

    @pytest.mark.asyncio
    async def test_binary_file_returns_notice(self, tmp_path: Path) -> None:
        f = tmp_path / "image.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": str(f)}),
            ToolUseContext(),
        )
        assert "[binary file:" in str(result.content) or result.truncated

    @pytest.mark.asyncio
    async def test_read_gbk_file_decoded_correctly(self, tmp_path: Path) -> None:
        f = tmp_path / "gbk.txt"
        f.write_bytes("中文内容\n".encode("gbk"))
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": str(f)}),
            ToolUseContext(),
        )
        assert not result.is_error
        assert "中文内容" in str(result.content)

    @pytest.mark.asyncio
    async def test_read_truly_binary_returns_error(self, tmp_path: Path) -> None:
        f = tmp_path / "data.txt"
        f.write_bytes(b"\x00\x01\x02\x03" * 100)
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": str(f)}),
            ToolUseContext(),
        )
        assert result.is_error

    @pytest.mark.asyncio
    async def test_byte_truncation_max_bytes(self, tmp_path: Path) -> None:
        f = tmp_path / "big.txt"
        content = "\n".join(f"line{i}" for i in range(1000))
        f.write_text(content)
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "max_bytes": 100}),
            ToolUseContext(),
        )
        assert result.truncated
        assert result.truncation_notice is not None
        assert "truncated at 100 bytes" in str(result.truncation_notice)

    @pytest.mark.asyncio
    async def test_offset_beyond_total_lines_within_byte_cap(self, tmp_path: Path) -> None:
        f = tmp_path / "big.txt"
        # File larger than max_bytes so byte_truncated is True;
        # offset beyond the line count within the truncated bytes.
        f.write_text("\n".join(f"line{i}" for i in range(100)))
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"file_path": str(f), "offset": 1000, "limit": 10, "max_bytes": 50}),
            ToolUseContext(),
        )
        assert "beyond the file's" in str(result.content)
        assert "within the byte cap" in str(result.content)

    @pytest.mark.asyncio
    async def test_cache_miss_when_byte_truncated_prev(self, tmp_path: Path) -> None:
        f = tmp_path / "retry.txt"
        f.write_text("hello world\n")
        tool = self.tool()
        ctx = ToolUseContext()
        ctx.read_file_state[str(f)] = {
            "mtime_ns": f.stat().st_mtime_ns,
            "size": f.stat().st_size,
            "content": None,
            "byte_truncated": True,
        }
        result = await tool.call(
            tool.validate_input({"file_path": str(f)}),
            ctx,
        )
        assert not result.is_error
        assert "hello world" in str(result.content)


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


class TestBashTool:
    def tool(self) -> BashTool:
        return get_tool("Bash")  # type: ignore[return-value]

    @pytest.mark.asyncio
    async def test_echo_command(self) -> None:
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"command": "echo hello"}),
            ToolUseContext(),
        )
        assert not result.is_error
        assert "hello" in str(result.content)

    @pytest.mark.asyncio
    async def test_command_failure(self) -> None:
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"command": "nonexistent_command_xyz 2>&1 || exit 1"}),
            ToolUseContext(),
        )
        assert result.is_error

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        tool = self.tool()
        if os.name == "nt":
            cmd = "ping -n 30 127.0.0.1 > NUL"
        else:
            cmd = "sleep 10"
        result = await tool.call(
            tool.validate_input({"command": cmd, "timeout": 1}),
            ToolUseContext(),
        )
        assert result.is_error
        assert "timed out" in str(result.content)

    @pytest.mark.asyncio
    async def test_abort_signal_kills_process(self) -> None:
        tool = self.tool()
        event = asyncio.Event()
        event.set()
        ctx = ToolUseContext(abort_signal=event)
        if os.name == "nt":
            cmd = "ping -n 30 127.0.0.1 > NUL"
        else:
            cmd = "sleep 10"
        result = await tool.call(
            tool.validate_input({"command": cmd, "timeout": 30}),
            ctx,
        )
        assert result.is_error
        assert "aborted" in str(result.content)

    @pytest.mark.asyncio
    async def test_no_output_command(self) -> None:
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"command": 'python -c "pass"'}),
            ToolUseContext(),
        )
        assert not result.is_error
        assert result.content == "[no output]"

    @pytest.mark.asyncio
    async def test_both_stdout_and_stderr(self) -> None:
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"command": 'python -c "import sys; print(\'out\'); print(\'err\', file=sys.stderr)"'}),
            ToolUseContext(),
        )
        assert not result.is_error
        content = str(result.content)
        assert "out" in content
        assert "[stderr]" in content
        assert "err" in content

    @pytest.mark.asyncio
    async def test_exit_code_in_output(self) -> None:
        tool = self.tool()
        result = await tool.call(
            tool.validate_input({"command": 'python -c "import sys; sys.exit(42)"'}),
            ToolUseContext(),
        )
        assert result.is_error
        assert "(exit code: 42)" in str(result.content)


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


class TestSafeDecodeBytes:
    def test_utf8_decode(self) -> None:
        result = safe_decode_bytes("hello world".encode("utf-8"))
        assert result == "hello world"

    def test_gbk_decode_via_charset_normalizer(self) -> None:
        result = safe_decode_bytes("中文".encode("gbk"))
        assert "中文" in result

    def test_truly_binary_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot decode as text"):
            safe_decode_bytes(b"\x00\x01\x02\x03" * 20)


class TestToolCallResult:
    def test_defaults(self) -> None:
        result = ToolCallResult(content="ok")
        assert result.content == "ok"
        assert result.is_error is False
        assert result.truncated is False
        assert result.truncation_notice is None
        assert result.metadata is None

    def test_with_metadata(self) -> None:
        result = ToolCallResult(
            content="ok",
            metadata={"lines_added": 3, "lines_removed": 1},
        )
        assert result.metadata == {"lines_added": 3, "lines_removed": 1}

    def test_with_truncation_notice(self) -> None:
        result = ToolCallResult(
            content="partial",
            truncated=True,
            truncation_notice="Showing 50 of 200 results",
        )
        assert result.truncated
        assert result.truncation_notice == "Showing 50 of 200 results"
