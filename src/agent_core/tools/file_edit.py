from __future__ import annotations

import difflib
import tokenize
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ._io import (
    MAX_CACHE_BYTES,
    async_atomic_write,
    async_read_text,
    build_state,
    check_workspace,
    fingerprint_changed,
)
from .base import Tool, ToolCallResult, ToolUseContext
from .registry import register_tool


class FileEditInput(BaseModel):
    file_path: str = Field(description="Absolute path to the file to edit")
    old_string: str = Field(description="Exact text to search for and replace")
    new_string: str = Field(description="Replacement text")


@register_tool
class FileEditTool(Tool):
    name: ClassVar[str] = "Edit"
    is_concurrency_safe: ClassVar[bool] = False
    is_read_only: ClassVar[bool] = False
    is_destructive: ClassVar[bool] = True

    def _detect_encoding(self, path: Path) -> str:
        try:
            with open(path, "rb") as f:
                encoding, _ = tokenize.detect_encoding(f.readline)
            return encoding
        except Exception:
            return "utf-8"

    def validate_input(self, input: dict[str, Any]) -> FileEditInput:
        return FileEditInput(**input)

    @classmethod
    def input_schema(cls) -> dict[str, Any]:
        return FileEditInput.model_json_schema()

    @staticmethod
    def _compute_change_metadata(old_text: str, new_text: str) -> dict[str, Any]:
        """Compute line-level change info from old/new full text.

        Uses SequenceMatcher opcodes to produce precise hunks (1-based line
        numbers) distinguishing replace/insert/delete, plus added/removed
        counts. This is the structured channel for programmatic consumers;
        LLM consumers can rely on the unified diff in the result content.
        """
        old_lines = old_text.splitlines()
        new_lines = new_text.splitlines()
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)

        hunks: list[dict[str, Any]] = []
        added = 0
        removed = 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            hunks.append(
                {
                    "type": tag,  # 'replace' | 'insert' | 'delete'
                    "old_start": i1 + 1,
                    "old_count": i2 - i1,
                    "new_start": j1 + 1,
                    "new_count": j2 - j1,
                }
            )
            if tag in ("replace", "insert"):
                added += j2 - j1
            if tag in ("replace", "delete"):
                removed += i2 - i1
        return {"changed_hunks": hunks, "lines_added": added, "lines_removed": removed}

    async def call(self, input: FileEditInput, context: ToolUseContext) -> ToolCallResult:
        path = Path(input.file_path).resolve()

        try:
            check_workspace(path, context.workspace_root)
        except PermissionError as e:
            return ToolCallResult(content=str(e), is_error=True)

        if not path.exists():
            return ToolCallResult(
                content=f"File not found: {path}",
                is_error=True,
            )

        if path.is_dir():
            return ToolCallResult(
                content=f"Path is a directory, not a file: {path}",
                is_error=True,
            )

        if str(path) not in context.read_file_state:
            return ToolCallResult(
                content=(
                    f"File {path} must be read before editing. "
                    "Use the Read tool first so the edit is based on current content."
                ),
                is_error=True,
            )

        state = context.read_file_state[str(path)]
        if state.get("mtime_ns") is not None and fingerprint_changed(path, state):
            return ToolCallResult(
                content=f"File {path} has been modified since last read; re-read before editing",
                is_error=True,
            )

        encoding = self._detect_encoding(path)

        # Prefer the cached full text (fingerprint already verified unchanged);
        # only hit disk when the file was too large to cache.
        cached_content = state.get("content")
        if cached_content is not None:
            original_text = cached_content
        else:
            try:
                original_text = await async_read_text(path, encoding=encoding)
            except UnicodeDecodeError:
                return ToolCallResult(
                    content=f"Cannot decode file as {encoding}; the file may be binary",
                    is_error=True,
                )

        if input.old_string not in original_text:
            return ToolCallResult(
                content=(
                    "old_string not found in file. "
                    "The text must match exactly, including whitespace. "
                    "Re-read the file if needed."
                ),
                is_error=True,
            )

        if original_text.count(input.old_string) > 1:
            return ToolCallResult(
                content=(
                    "old_string matches multiple locations in the file. "
                    "Provide a larger string with more surrounding context to uniquely identify the match."
                ),
                is_error=True,
            )

        new_text = original_text.replace(input.old_string, input.new_string, 1)

        diff = difflib.unified_diff(
            original_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
        diff_text = "".join(diff)

        metadata = self._compute_change_metadata(original_text, new_text)

        await async_atomic_write(path, new_text, encoding=encoding)

        # We hold the new full text — cache it (within budget) so the next
        # Edit/Write/Read needs no disk access and sees current content.
        cache_content = new_text if len(new_text.encode(encoding)) <= MAX_CACHE_BYTES else None
        context.read_file_state[str(path)] = build_state(
            path,
            content=cache_content,
            total_lines=len(new_text.splitlines()),
        )

        return ToolCallResult(content=diff_text, metadata=metadata)
