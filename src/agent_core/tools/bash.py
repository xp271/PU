from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from ._io import check_workspace
from .base import Tool, ToolCallResult, ToolUseContext
from .registry import register_tool


class BashInput(BaseModel):
    command: str = Field(description="Shell command to execute")
    timeout: float = Field(default=120.0, ge=1, le=600, description="Timeout in seconds")
    working_dir: str | None = Field(default=None, description="Working directory for the command")


@register_tool
class BashTool(Tool):
    name: ClassVar[str] = "Bash"
    is_concurrency_safe: ClassVar[bool] = False
    is_read_only: ClassVar[bool] = False
    is_destructive: ClassVar[bool] = True

    _MAX_OUTPUT_BYTES: ClassVar[int] = 100_000

    def validate_input(self, input: dict[str, Any]) -> BashInput:
        return BashInput(**input)

    @classmethod
    def input_schema(cls) -> dict[str, Any]:
        return BashInput.model_json_schema()

    async def call(self, input: BashInput, context: ToolUseContext) -> ToolCallResult:
        cwd = Path(input.working_dir).resolve() if input.working_dir else Path.cwd()
        try:
            check_workspace(cwd, context.workspace_root)
        except PermissionError as e:
            return ToolCallResult(content=str(e), is_error=True)
        cwd = str(cwd)

        try:
            proc = await asyncio.create_subprocess_shell(
                input.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
        except Exception as e:
            return ToolCallResult(content=f"Failed to spawn process: {e}", is_error=True)

        communicate_task = asyncio.create_task(proc.communicate())
        abort_task = asyncio.create_task(context.abort_signal.wait())

        done, pending = await asyncio.wait(
            [communicate_task, abort_task],
            timeout=input.timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if communicate_task in done:
            abort_task.cancel()
            stdout, stderr = communicate_task.result()
        elif abort_task in done:
            communicate_task.cancel()
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            return ToolCallResult(
                content="Command aborted by user",
                is_error=True,
            )
        else:
            communicate_task.cancel()
            abort_task.cancel()
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            return ToolCallResult(
                content=f"Command timed out after {input.timeout}s",
                is_error=True,
            )

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")

        def truncate_bytes(text: str, label: str) -> str:
            encoded = text.encode("utf-8")
            if len(encoded) <= self._MAX_OUTPUT_BYTES:
                return text
            clipped = encoded[: self._MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
            return clipped + f"\n[{label} truncated]"

        parts: list[str] = []
        if stdout_text:
            parts.append(truncate_bytes(stdout_text, "stdout"))
        if stderr_text:
            parts.append(f"[stderr]\n{truncate_bytes(stderr_text, 'stderr')}")

        output = "\n".join(parts) if parts else "[no output]"

        is_error = proc.returncode != 0
        if is_error and not output.endswith(f"(exit code: {proc.returncode})"):
            output += f"\n(exit code: {proc.returncode})"

        return ToolCallResult(content=output, is_error=is_error)
