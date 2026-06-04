from __future__ import annotations

import mimetypes
import stat
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ._io import (
    MAX_CACHE_BYTES,
    async_read_bytes,
    build_state,
    check_workspace,
    file_fingerprint,
    safe_decode_bytes,
)
from .base import Tool, ToolCallResult, ToolUseContext
from .registry import register_tool


class FileReadInput(BaseModel):
    file_path: str = Field(description="Absolute path to the file to read")
    offset: int = Field(default=0, ge=0, description="Line offset to start reading from")
    limit: int = Field(default=2000, ge=1, le=10000, description="Maximum number of lines to read")
    max_bytes: int = Field(default=500_000, ge=1, description="Maximum bytes to read (safety cap)")


@register_tool
class FileReadTool(Tool):
    name: ClassVar[str] = "Read"
    is_concurrency_safe: ClassVar[bool] = True
    is_read_only: ClassVar[bool] = True
    is_destructive: ClassVar[bool] = False

    _TEXT_MIMES: ClassVar[set[str]] = {
        "text/", "application/json", "application/xml", "application/javascript",
        "application/x-yaml", "application/x-httpd-php", "application/x-sh",
        "application/x-python", "application/x-python-code",
        "application/sql", "application/x-sql",
    }

    def _detect_mime(self, path: Path) -> str:
        mime, _ = mimetypes.guess_type(str(path))
        if mime is None:
            return "application/octet-stream"
        return mime

    def _is_text(self, mime: str) -> bool:
        if mime.startswith("text/"):
            return True
        return any(mime.startswith(prefix) for prefix in self._TEXT_MIMES)

    def _check_path_safety(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        try:
            st = path.stat()
        except OSError:
            raise PermissionError(f"Cannot stat file: {path}")

        if stat.S_ISBLK(st.st_mode) or stat.S_ISCHR(st.st_mode):
            raise ValueError(f"Device file not supported: {path}")

    def validate_input(self, input: dict[str, Any]) -> FileReadInput:
        return FileReadInput(**input)

    @classmethod
    def input_schema(cls) -> dict[str, Any]:
        return FileReadInput.model_json_schema()

    def _slice_lines(
        self, text: str, offset: int, limit: int
    ) -> tuple[str, int, bool, str | None]:
        """Apply offset/limit to text. Returns (result_text, total_lines,
        truncated, truncation_notice)."""
        lines = text.splitlines(keepends=True)
        total_lines = len(lines)
        truncated = False
        notice: str | None = None

        if offset > 0:
            lines = lines[offset:]
        if len(lines) > limit:
            lines = lines[:limit]
            truncated = True
            notice = f"Showing lines {offset + 1}-{offset + limit} of {total_lines}"

        result_text = "".join(lines)
        if result_text and not result_text.endswith("\n"):
            result_text += "\n"
        return result_text, total_lines, truncated, notice

    async def call(self, input: FileReadInput, context: ToolUseContext) -> ToolCallResult:
        path = Path(input.file_path).resolve()

        try:
            check_workspace(path, context.workspace_root)
        except PermissionError as e:
            return ToolCallResult(content=str(e), is_error=True)
        try:
            self._check_path_safety(path)
        except (FileNotFoundError, PermissionError, ValueError) as e:
            return ToolCallResult(content=str(e), is_error=True)

        if str(path) in context.read_file_state:
            prev = context.read_file_state[str(path)]
            current = file_fingerprint(path)
            unchanged = (
                prev.get("mtime_ns") == current["mtime_ns"]
                and prev.get("size") == current["size"]
            )
            cached_content = prev.get("content")
            # If the file is unchanged and we hold a trustworthy full-text cache
            # (not byte-truncated), serve directly from cache without disk I/O.
            if unchanged and cached_content is not None and not prev.get("byte_truncated", False):
                result_text, total_lines, truncated, notice = self._slice_lines(
                    cached_content, input.offset, input.limit
                )
                if input.offset >= total_lines and total_lines > 0:
                    return ToolCallResult(
                        content=f"[offset {input.offset} is beyond the file's {total_lines} available lines]",
                    )
                return ToolCallResult(
                    content=result_text,
                    truncated=truncated,
                    truncation_notice=notice,
                )

        mime = self._detect_mime(path)

        if not self._is_text(mime):
            if mime == "application/pdf":
                # TODO: PDF reading support (requires pymupdf or pdfplumber)
                pass
            elif mime and mime.startswith("image/"):
                # TODO: Image reading support (requires Pillow)
                pass
            return ToolCallResult(
                content=f"[binary file: {mime}, size: {path.stat().st_size} bytes]",
                truncated=True,
                truncation_notice=f"Binary file ({mime}), not readable as text",
            )

        raw = await async_read_bytes(path)

        byte_truncated = len(raw) > input.max_bytes
        if byte_truncated:
            raw = raw[:input.max_bytes]
            truncated = True
            truncation_notice = f"File truncated at {input.max_bytes} bytes"
        else:
            truncated = False
            truncation_notice = None

        try:
            text = safe_decode_bytes(raw)
        except (UnicodeDecodeError, ValueError):
            if byte_truncated:
                # The byte cap may have split a multibyte character at the tail;
                # retry tolerating an incomplete final character before giving up.
                try:
                    text = raw.decode("utf-8", errors="ignore")
                except Exception:
                    return ToolCallResult(
                        content=f"[Cannot decode file as text: {mime}]",
                        is_error=True,
                    )
            else:
                return ToolCallResult(
                    content=f"[Cannot decode file as text: {mime}]",
                    is_error=True,
                )

        result_text, total_lines, line_truncated, line_notice = self._slice_lines(
            text, input.offset, input.limit
        )

        if input.offset >= total_lines and total_lines > 0:
            return ToolCallResult(
                content=f"[offset {input.offset} is beyond the file's {total_lines} available lines"
                + (" within the byte cap" if byte_truncated else "")
                + "]",
                truncated=truncated,
                truncation_notice=truncation_notice,
            )

        if line_truncated and not truncated:
            truncated = True
            truncation_notice = line_notice

        # Cache full text only when not byte-truncated and within the budget;
        # otherwise record fingerprint-only state (content=None) so Edit/Write
        # and later Reads fall back to disk safely.
        cache_content = (
            text
            if not byte_truncated and len(raw) <= MAX_CACHE_BYTES
            else None
        )
        context.read_file_state[str(path)] = build_state(
            path,
            content=cache_content,
            total_lines=total_lines,
            offset=input.offset,
            limit=input.limit,
            byte_truncated=byte_truncated,
        )

        return ToolCallResult(
            content=result_text,
            truncated=truncated,
            truncation_notice=truncation_notice,
        )
