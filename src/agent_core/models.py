from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Shared type aliases (single source of truth for repeated literal unions)
# ---------------------------------------------------------------------------

PermissionMode = Literal["bypassPermissions", "acceptEdits", "ask"]
StopReason = Literal["end_turn", "max_turns", "permission_denied", "error"]


class ClaudeModel(str, Enum):
    SONNET_4_5 = "claude-sonnet-4-5"
    OPUS_4_7 = "claude-opus-4-7"
    HAIKU_4_5 = "claude-haiku-4-5"

    @classmethod
    def known_values(cls) -> set[str]:
        return {m.value for m in cls}


class AgentConfig(BaseModel):
    model: str = Field(
        default=ClaudeModel.SONNET_4_5.value,
        description="LLM model identifier",
    )
    max_turns: int = Field(default=20, description="Maximum number of turn iterations")
    permission_mode: PermissionMode = Field(
        default="ask",
        description="Default permission handling mode",
    )
    tools: list[str] = Field(
        default_factory=lambda: ["*"],
        description="List of tool names available to the agent",
    )
    mcp_servers: dict[str, Any] = Field(
        default_factory=dict,
        description="MCP server configurations keyed by server name",
    )
    skills_dir: str | None = Field(
        default=None,
        description="Directory path for skill definitions",
    )
    hooks: list[Any] = Field(
        default_factory=list,
        description="Hook instances registered at agent bootstrap",
    )
    allow_unknown_model: bool = Field(
        default=False,
        description=(
            "If True, accept model identifiers outside the built-in ClaudeModel "
            "enum (e.g. when routing through a proxy or self-hosted base_url). "
            "Unknown names are still warned about, not rejected."
        ),
    )

    @model_validator(mode="after")
    def validate_model(self) -> "AgentConfig":
        if self.model in ClaudeModel.known_values():
            return self
        known = ", ".join(sorted(ClaudeModel.known_values()))
        if self.allow_unknown_model:
            import warnings

            warnings.warn(
                f"Model '{self.model}' is not a known built-in model ({known}); "
                "proceeding because allow_unknown_model=True.",
                stacklevel=2,
            )
            return self
        raise ValueError(
            f"Unknown model '{self.model}'. Known models: {known}. "
            "Set allow_unknown_model=True to use a custom identifier."
        )

    @field_validator("tools")
    @classmethod
    def validate_tools(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("tools list must not be empty")
        return v

    @field_validator("max_turns")
    @classmethod
    def validate_max_turns(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_turns must be >= 1")
        if v > 500:
            raise ValueError("max_turns must be <= 500")
        return v


class AgentResult(BaseModel):
    content: str
    total_tokens: int
    tool_uses: int
    stop_reason: StopReason
    error_detail: str | None = None


# ---------------------------------------------------------------------------
# Streaming message variants (discriminated union on `type`)
# ---------------------------------------------------------------------------

class AssistantText(BaseModel):
    type: Literal["assistant_text"] = "assistant_text"
    content: str


class ToolUse(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    tool_use_id: str
    tool_name: str
    tool_input: dict[str, Any]


class ToolResult(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    tool_name: str
    content: Any
    is_error: bool = False


class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    message: str
    error_type: str | None = None


class DoneMessage(BaseModel):
    """Terminal message emitted once at the end of a stream.

    Carries the authoritative stop reason and run-level token accounting so
    that run() can aggregate without inferring them from stream termination.
    """

    type: Literal["done"] = "done"
    stop_reason: StopReason
    total_tokens: int = 0
    error_detail: str | None = None


AgentMessage = Annotated[
    AssistantText | ToolUse | ToolResult | ErrorMessage | DoneMessage,
    Field(discriminator="type"),
]
