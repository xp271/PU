from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

from ._io import check_workspace
from .base import Tool, ToolCallResult, ToolUseContext
from .registry import register_tool

OutputMode = Literal["files_with_matches", "content", "count"]


class GrepInput(BaseModel):
    pattern: str = Field(description="Regular expression pattern to search for")
    path: str = Field(default=".", description="File or directory to search in")
    glob: str | None = Field(default=None, description="Glob pattern to filter files (e.g. '*.py')")
    output_mode: OutputMode = Field(default="files_with_matches", description="Output mode")
    head_limit: int = Field(default=100, ge=1, le=1000, description="Maximum number of output lines")
    offset: int = Field(default=0, ge=0, description="Skip first N matches before applying head_limit")
    context_around: int = Field(default=0, ge=0, le=10, description="Context lines around match (-C)")
    ignore_case: bool = Field(default=False, description="Case insensitive search (-i)")
    multiline: bool = Field(default=False, description="Enable multiline mode (-U --multiline-dotall)")


@register_tool
class GrepTool(Tool):
    name: ClassVar[str] = "Grep"
    is_concurrency_safe: ClassVar[bool] = True
    is_read_only: ClassVar[bool] = True
    is_destructive: ClassVar[bool] = False

    _MAX_OUTPUT_BYTES: ClassVar[int] = 100_000

    def _rg(self) -> str:
        rg = shutil.which("rg")
        if rg is None:
            raise RuntimeError("ripgrep (rg) not found on PATH")
        return rg

    def validate_input(self, input: dict[str, Any]) -> GrepInput:
        return GrepInput(**input)

    @classmethod
    def input_schema(cls) -> dict[str, Any]:
        return GrepInput.model_json_schema()

    async def call(self, input: GrepInput, context: ToolUseContext) -> ToolCallResult:
        search_path = Path(input.path).resolve()
        try:
            check_workspace(search_path, context.workspace_root)
        except PermissionError as e:
            return ToolCallResult(content=str(e), is_error=True)

        try:
            rg = self._rg()
        except RuntimeError as e:
            return ToolCallResult(content=str(e), is_error=True)

        cmd = [rg, "--json"]

        if input.ignore_case:
            cmd.append("-i")

        if input.multiline:
            cmd.extend(["-U", "--multiline-dotall"])

        if input.context_around > 0:
            cmd.extend(["-C", str(input.context_around)])

        if input.glob is not None:
            cmd.extend(["--glob", input.glob])

        cmd.extend(["--", input.pattern, str(search_path)])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return ToolCallResult(content="ripgrep (rg) executable not found", is_error=True)

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=30
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolCallResult(content="Grep timed out after 30 seconds", is_error=True)

        if proc.returncode not in (0, 1):
            err = stderr.decode("utf-8", errors="replace").strip()
            return ToolCallResult(content=f"Grep error: {err}", is_error=True)

        output_lines: list[str] = []
        seen_paths: set[str] = set()
        total_matches = 0

        for line in stdout.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                output_lines.append(line)
                continue

            rtype = record.get("type")
            if rtype == "match":
                match_data = record.get("data", {})
                path_text = match_data.get("path", {}).get("text", "")
                line_number = match_data.get("line_number", 0)
                lines_data = match_data.get("lines", {})
                match_text = lines_data.get("text", "").rstrip("\n")

                if input.output_mode == "files_with_matches":
                    if path_text and path_text not in seen_paths:
                        seen_paths.add(path_text)
                        output_lines.append(path_text)
                elif input.output_mode == "count":
                    total_matches += 1
                else:
                    output_lines.append(f"{path_text}:{line_number}:{match_text}")
            elif rtype == "begin":
                pass
            elif rtype == "end":
                pass
            elif rtype == "summary":
                pass
            else:
                output_lines.append(line)

        if input.output_mode == "count":
            output_lines = [str(total_matches)]

        if input.output_mode == "files_with_matches" and not output_lines:
            output_lines = ["[no matches]"]

        total_before_offset = len(output_lines)
        if input.offset > 0:
            output_lines = output_lines[input.offset:]

        truncated_by_count = len(output_lines) > input.head_limit
        if truncated_by_count:
            output_lines = output_lines[: input.head_limit]

        # Accumulate whole lines up to the byte cap so we never cut mid-line.
        kept: list[str] = []
        running = 0
        truncated_by_bytes = False
        for ln in output_lines:
            add = len(ln.encode("utf-8")) + 1  # +1 for the joining newline
            if running + add > self._MAX_OUTPUT_BYTES:
                truncated_by_bytes = True
                break
            kept.append(ln)
            running += add
        output_lines = kept

        result_text = "\n".join(output_lines)
        truncated = truncated_by_count or truncated_by_bytes

        if truncated_by_bytes:
            notice = (
                f"Showing {len(output_lines)} results (output capped at "
                f"{self._MAX_OUTPUT_BYTES} bytes, total {total_before_offset})"
            )
        elif truncated_by_count and input.offset > 0:
            notice = f"Showing {len(output_lines)} results (offset {input.offset}, total {total_before_offset})"
        elif truncated_by_count:
            notice = f"Showing {len(output_lines)} of {total_before_offset} results"
        else:
            notice = None

        return ToolCallResult(
            content=result_text,
            truncated=truncated,
            truncation_notice=notice,
        )
