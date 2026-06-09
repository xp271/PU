from __future__ import annotations

import os
import time
from pathlib import Path
from typing import ClassVar

import pytest

from agent_core.tools import (
    FileReadTool,
    Tool,
    ToolCallResult,
    ToolUseContext,
    get_tool,
)
from agent_core.tools._io import file_fingerprint, safe_decode_bytes


@pytest.mark.read
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


@pytest.mark.read
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
