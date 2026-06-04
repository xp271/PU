from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ._io import check_workspace
from .base import Tool, ToolCallResult, ToolUseContext
from .registry import register_tool


class GlobInput(BaseModel):
    pattern: str = Field(description="Glob pattern to match files (e.g. '**/*.py')")
    path: str = Field(default=".", description="Base directory for glob search")


@register_tool
class GlobTool(Tool):
    name: ClassVar[str] = "Glob"
    is_concurrency_safe: ClassVar[bool] = True
    is_read_only: ClassVar[bool] = True
    is_destructive: ClassVar[bool] = False

    _MAX_RESULTS: ClassVar[int] = 100

    def validate_input(self, input: dict[str, Any]) -> GlobInput:
        return GlobInput(**input)

    @classmethod
    def input_schema(cls) -> dict[str, Any]:
        return GlobInput.model_json_schema()

    async def call(self, input: GlobInput, context: ToolUseContext) -> ToolCallResult:
        base = Path(input.path).resolve()
        try:
            check_workspace(base, context.workspace_root)
        except PermissionError as e:
            return ToolCallResult(content=str(e), is_error=True)
        if not base.exists():
            return ToolCallResult(content=f"Path not found: {base}", is_error=True)

        if not base.is_dir():
            return ToolCallResult(content=f"Not a directory: {base}", is_error=True)

        matches: list[Path] = []
        for p in base.glob(input.pattern):
            if p.is_file() or p.is_dir():
                # Resolve to defeat symlinks that point outside the workspace,
                # then drop anything that escapes the configured root.
                if context.workspace_root is not None:
                    try:
                        check_workspace(p.resolve(), context.workspace_root)
                    except PermissionError:
                        continue
                matches.append(p)

        # Filter out hidden/dot-prefixed entries unless pattern starts with dot
        if not input.pattern.startswith("."):
            matches = [m for m in matches if not any(part.startswith(".") for part in m.parts)]

        # Sort by mtime descending
        matches.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)

        total = len(matches)
        truncated = total > self._MAX_RESULTS
        if truncated:
            matches = matches[: self._MAX_RESULTS]

        lines = [str(m) for m in matches]
        result_text = "\n".join(lines)

        return ToolCallResult(
            content=result_text,
            truncated=truncated,
            truncation_notice=f"Showing top {self._MAX_RESULTS} of {total} results" if truncated else None,
        )
