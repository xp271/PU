"""AgentDefinition — pure-data capability template for sub-agents.

``AgentDefinition`` is a Pydantic container with no execution logic.
It describes *what* an agent can do (tools, system_prompt, permissions);
the *how* lives in ``run_agent()`` (Item 8).
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..models import McpServerConfig, PermissionMode


# ---------------------------------------------------------------------------
# AgentMemory
# ---------------------------------------------------------------------------

class AgentMemory(str, Enum):
    """Sub-agent memory persistence mode."""
    USER = "user"          # persist across sessions to user memory
    PROJECT = "project"    # project-level memory
    LOCAL = "local"        # current session only
    NONE = "none"          # no memory


# ---------------------------------------------------------------------------
# AgentDefinition
# ---------------------------------------------------------------------------

class AgentDefinition(BaseModel):
    """Capability template for a (sub-)agent — pure data, no execution logic.

    Multiple sources defining the same *agent_type* are merged via a
    priority chain:  user > project > plugin > builtin.

    Scalar fields use "highest explicit-setter wins"; ``disallowed_tools``
    and ``auto_approve_tools`` are unioned across all layers.
    """

    agent_type: str
    """Unique identifier, e.g. ``"Explore"``, ``"my-custom-agent"``."""

    when_to_use: str | None = None
    """Human-readable description shown to the LLM for routing decisions
    (consumed by AgentTool in Item 8)."""

    tools: list[str] = Field(default_factory=lambda: ["*"])
    """Tool allow-list. ``["*"]`` means "all tools the parent allows".
    Effective tools = this list ∩ parent-allowed set."""

    disallowed_tools: list[str] = Field(default_factory=list)
    """Tool deny-list. Effective disallowed = this list ∪ parent-disallowed."""

    auto_approve_tools: list[str] = Field(default_factory=list)
    """Pre-authorized tool names.  Item 8 compiles each entry into a
    ``PermissionRule(action="allow")`` so the tool call bypasses the *ask*
    dialog.  Default ``[]`` — nothing is pre-approved.

    Safety invariants (enforced by Item 8, declared here for reference):
    - The tool must still pass the tool-pool filter (tools ∩ parent-allowed).
    - This only controls "whether a dialog pops up", NOT "whether the tool
      can be called at all".
    - Parent ``disallowed_tools`` take precedence; a parent-forbidden tool
      is never callable even if listed here.
    """

    system_prompt: str = ""
    """Injected into the sub-agent's system prompt (Item 8 prepends this to
    the parent prompt + tool-list block)."""

    model: str | None = None
    """Model identifier (e.g. ``"anthropic/claude-haiku-4-5"``).
    ``None`` inherits the parent's current model."""

    permission_mode: PermissionMode | None = None
    """Declared permission mode.  ``None`` inherits the parent mode.
    At runtime the effective mode is ``min(parent, this)`` — a child can
    **never** be more permissive than its parent (see ``inheritance.py``).

    .. note::
       ``permission_mode`` is **not** the mechanism for making edit tools
       approval-free.  Use ``auto_approve_tools`` for that.
    """

    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    """Agent-specific MCP servers.  Item 8 shallow-merges with parent
    servers (child wins on same-name key)."""

    hooks: list[Any] = Field(default_factory=list)
    """Agent-specific hook instances (wired in Item 9)."""

    skills: list[str] = Field(default_factory=list)
    """Pre-loaded skill names (wired in Item 9)."""

    memory: AgentMemory = AgentMemory.NONE
    """Sub-agent memory persistence mode (wired in Item 10)."""

    background: bool = False
    """If ``True``, the agent defaults to async background execution
    (Item 8)."""

    # -- validators -----------------------------------------------------------

    @field_validator("agent_type")
    @classmethod
    def _validate_agent_type(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("agent_type must not be empty")
        if not re.match(r"^[a-zA-Z0-9_-]+$", v):
            raise ValueError(
                f"agent_type '{v}' contains invalid characters. "
                "Only [a-zA-Z0-9_-] allowed."
            )
        return v.strip()

    @field_validator("tools")
    @classmethod
    def _validate_tools(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("tools must not be empty")
        return v
