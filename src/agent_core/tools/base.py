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
    # -- §1.5: parent-agent handles, populated by query() for AgentTool --
    parent_config: Any = None               # AgentConfig | None
    parent_trace_id: str = ""
    parent_permission_callback: Any = None  # PermissionCallback | None
    parent_system: str = ""
    parent_allowed_tools: frozenset[str] = field(default_factory=frozenset)
    parent_disallowed_tools: frozenset[str] = field(default_factory=frozenset)
    agent_depth: int = 0                    # §2.6 recursion guard
    notification_queue: list[str] | None = None  # §5.5 async notification delivery
    hook_registry: Any = None  # HookRegistry | None (§Item 9)


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
