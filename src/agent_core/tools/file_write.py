from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ._io import MAX_CACHE_BYTES, async_atomic_write, build_state, check_workspace, fingerprint_changed
from .base import Tool, ToolCallResult, ToolUseContext
from .registry import register_tool


class FileWriteInput(BaseModel):
    file_path: str = Field(description="Absolute path to the file to create or overwrite")
    content: str = Field(description="Content to write to the file")


@register_tool
class FileWriteTool(Tool):
    name: ClassVar[str] = "Write"
    is_concurrency_safe: ClassVar[bool] = False
    is_read_only: ClassVar[bool] = False
    is_destructive: ClassVar[bool] = True

    def validate_input(self, input: dict[str, Any]) -> FileWriteInput:
        return FileWriteInput(**input)

    @classmethod
    def input_schema(cls) -> dict[str, Any]:
        return FileWriteInput.model_json_schema()

    async def call(self, input: FileWriteInput, context: ToolUseContext) -> ToolCallResult:
        path = Path(input.file_path).resolve()

        try:
            check_workspace(path, context.workspace_root)
        except PermissionError as e:
            return ToolCallResult(content=str(e), is_error=True)

        if path.is_dir():
            return ToolCallResult(
                content=f"Path is a directory, cannot write: {path}",
                is_error=True,
            )

        if str(path) in context.read_file_state and path.exists():
            state = context.read_file_state[str(path)]
            if state.get("mtime_ns") is not None and fingerprint_changed(path, state):
                return ToolCallResult(
                    content=f"File {path} has been modified since last read; re-read before writing",
                    is_error=True,
                )

        await async_atomic_write(path, input.content, encoding="utf-8")

        # Cache the written content (within budget) so subsequent Read/Edit
        # operate without disk access and see current content.
        cache_content = (
            input.content if len(input.content.encode("utf-8")) <= MAX_CACHE_BYTES else None
        )
        context.read_file_state[str(path)] = build_state(
            path,
            content=cache_content,
            total_lines=len(input.content.splitlines()),
        )

        return ToolCallResult(content=f"Wrote {len(input.content)} chars to {path}")
