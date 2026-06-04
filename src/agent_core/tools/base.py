from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel


@dataclass
class ToolUseContext:
    abort_signal: asyncio.Event = field(default_factory=asyncio.Event)
    read_file_state: dict[str, Any] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    agent_id: str = ""
    workspace_root: str | None = None


class ToolCallResult(BaseModel):
    content: Any
    is_error: bool = False
    truncated: bool = False
    truncation_notice: str | None = None
    metadata: dict[str, Any] | None = None


class Tool(ABC):
    name: ClassVar[str]
    is_concurrency_safe: ClassVar[bool] = False
    is_read_only: ClassVar[bool] = False
    is_destructive: ClassVar[bool] = False

    @abstractmethod
    async def call(self, input: BaseModel, context: ToolUseContext) -> ToolCallResult: ...

    @abstractmethod
    def validate_input(self, input: dict[str, Any]) -> BaseModel: ...

    @classmethod
    def input_schema(cls) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def output_schema(cls) -> dict[str, Any]:
        return ToolCallResult.model_json_schema()
