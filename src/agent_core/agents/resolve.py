"""
Agent resolution functions — Item 7.6 consumption contract.

These pure functions compute the effective runtime configuration for a
child agent from its ``AgentDefinition`` and the parent's active state.

All functions are side-effect-free and independently testable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import McpServerConfig, ModelId, PermissionMode
from ..permissions.engine import PermissionEngine
from ..permissions.inheritance import (
    compute_child_allowed_tools,
    compute_child_disallowed_tools,
    compute_child_permission_mode,
)
from ..permissions.rules import PermissionRule, default_rules

if TYPE_CHECKING:
    from .definition import AgentDefinition


# ---------------------------------------------------------------------------
# Recursion guard
# ---------------------------------------------------------------------------

MAX_AGENT_DEPTH = 3
"""Maximum nesting depth for sub-agent creation (parent = 0)."""


def disallow_agent_at_depth_limit(
    allowed: frozenset[str], depth: int
) -> frozenset[str]:
    """Remove ``"Agent"`` from *allowed* when the depth limit is reached.

    Callers (``AgentTool.call`` / ``run_agent``) should check
    ``agent_depth >= MAX_AGENT_DEPTH`` **before** calling this function
    and reject the request outright.  This function provides the
    additional hard-gate inside the tool-pool computation so that even
    if the pre-check is bypassed, the tool is unavailable.
    """
    if depth >= MAX_AGENT_DEPTH:
        return allowed - {"Agent"}
    return allowed


# ---------------------------------------------------------------------------
# 7.6.1 — Tool pool filtering
# ---------------------------------------------------------------------------


def resolve_agent_tools(
    definition: AgentDefinition,
    parent_allowed: frozenset[str],
    parent_disallowed: frozenset[str],
) -> tuple[frozenset[str], frozenset[str]]:
    """Compute the effective (allowed, disallowed) tool sets for a child agent.

    .. code-block::

        child_allowed  = parent_allowed ∩ definition.tools
        child_disallowed = parent_disallowed ∪ definition.disallowed_tools
        child_allowed  = child_allowed - child_disallowed   (dedup)

    When ``definition.tools == ["*"]`` the child inherits the full parent
    allowed set (minus disallowed).  The safety invariants are enforced
    by the pure functions in ``permissions/inheritance.py``.
    """
    child_allowed = compute_child_allowed_tools(
        parent_allowed, definition.tools
    )
    child_disallowed = compute_child_disallowed_tools(
        parent_disallowed, definition.disallowed_tools
    )
    # Hard dedup: a tool in *both* sets is effectively disallowed.
    return child_allowed - child_disallowed, child_disallowed


# ---------------------------------------------------------------------------
# 7.6.2 — Permission engine construction
# ---------------------------------------------------------------------------


def build_child_engine(
    definition: AgentDefinition,
    parent_mode: PermissionMode,
    parent_disallowed: frozenset[str],
) -> PermissionEngine:
    """Build a ``PermissionEngine`` for the child agent.

    1. Compute effective mode via ``compute_child_permission_mode``
       (child can never be more permissive than parent).
    2. Compile ``auto_approve_tools`` entries into ``PermissionRule(action="allow")``
       rules, **after** filtering out any tool in *parent_disallowed*.
    3. Construct the engine with ``default_rules() + extra_rules``.

    .. note::

       ``auto_approve_tools`` only controls "pop-up or not" — the tool
       must still pass the tool-pool filter (``resolve_agent_tools``).
    """
    child_mode = compute_child_permission_mode(
        parent_mode, definition.permission_mode
    )

    # Safety: parent-forbidden tools are NEVER pre-approved, even if the
    # definition lists them in auto_approve_tools.
    safe_auto = [
        name
        for name in definition.auto_approve_tools
        if name not in parent_disallowed
    ]

    extra_rules = [
        PermissionRule(
            tool_pattern=name,
            action="allow",
            priority=0,  # highest priority tier — beats default rules
            description=f"pre-authorized for agent '{definition.agent_type}'",
        )
        for name in safe_auto
    ]

    return PermissionEngine(
        rules=default_rules() + extra_rules,
        mode=child_mode,
    )


# ---------------------------------------------------------------------------
# 7.6.3 — MCP server merging
# ---------------------------------------------------------------------------


def merge_mcp_servers(
    definition: AgentDefinition,
    parent_mcp: dict[str, McpServerConfig],
) -> dict[str, McpServerConfig]:
    """Shallow-merge MCP server configs.  Child wins on same-name key.

    Returns a new dict (does not mutate inputs).
    """
    merged: dict[str, McpServerConfig] = dict(parent_mcp)
    merged.update(definition.mcp_servers)
    return merged


# ---------------------------------------------------------------------------
# 7.6.4 — System prompt construction
# ---------------------------------------------------------------------------


def build_child_system_prompt(
    definition: AgentDefinition,
    parent_system: str,
    tool_names: frozenset[str],
) -> str:
    """Build the child agent's system prompt.

    Format::

        {definition.system_prompt}

        ---
        Available tools: {sorted tool_names}
        {parent_system}

    The child's own system prompt comes first (highest priority),
    followed by the tool listing, then the parent's system prompt
    (retained for context).
    """
    parts: list[str] = []

    if definition.system_prompt:
        parts.append(definition.system_prompt)

    if tool_names:
        parts.append(
            "Available tools: " + ", ".join(sorted(tool_names))
        )

    if parent_system:
        parts.append(parent_system)

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 7.6.5 — Model resolution
# ---------------------------------------------------------------------------


def resolve_model(
    definition: AgentDefinition,
    parent_model_id: ModelId,
) -> ModelId:
    """Resolve the model for a child agent.

    ``definition.model is None`` → inherit *parent_model_id*.
    Otherwise, parse the string via ``ModelId.parse``.
    """
    if definition.model is None:
        return parent_model_id
    return ModelId.parse(definition.model)
