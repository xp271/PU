"""
Permission rules — Pydantic models and default rule sets.

Each ``PermissionRule`` matches one or more tools by name pattern
(exact, prefix, or fnmatch wildcard) and prescribes an action.
Rules are ordered by priority (lower = higher priority).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

PermissionAction = Literal["allow", "deny", "ask"]

# Where a rule originated. ``session`` rules are remembered in-memory for the
# duration of an interactive session (see ``SessionPermissionStore``); they are
# ordinary rules distinguished only by source — never persisted to disk.
PermissionSource = Literal["builtin", "config", "session"]


class PermissionRule(BaseModel):
    """A single permission rule matching tool names by pattern.

    Matching modes (tried in order within the same priority tier):
      - Exact:   ``"Bash"`` matches only ``"Bash"``
      - Prefix:  ``"File*"`` is treated as ``file*`` fnmatch
      - Wildcard: ``"mcp__*"`` uses fnmatch glob semantics

    **Priority convention** — lower number = higher precedence:
      - session-deny    → 10
      - session-allow   → 20
      - builtin/config  → 100
    If a hard-deny builtin rule is ever added, give it ``priority < 10`` so a
    user's session-allow can never shadow it.
    """

    tool_pattern: str = Field(
        description=(
            "Tool name pattern: exact match, prefix* wildcard, "
            "or fnmatch glob (e.g. 'mcp__*', 'File*')."
        ),
    )
    action: PermissionAction = Field(
        default="ask",
        description="Action to take when this rule matches.",
    )
    priority: int = Field(
        default=0,
        description="Lower values = higher priority. Ties broken by exact > prefix > wildcard.",
    )
    path_constraint: str | None = Field(
        default=None,
        description="Optional path glob constraint (applies only to file/path tools).",
    )
    rule_content: str | None = Field(
        default=None,
        description=(
            "Optional content-level constraint (e.g. a Bash command prefix). "
            "Reserved for content-granular rules — not yet consulted by the matcher."
        ),
    )
    source: PermissionSource = Field(
        default="builtin",
        description="Rule origin. 'session' rules are remembered per-session, never persisted.",
    )
    description: str = Field(
        default="",
        description="Human-readable explanation for this rule.",
    )


def default_rules() -> list[PermissionRule]:
    """Return the built-in default permission rule set.

    These are always applied unless overridden by user-defined rules
    (which may have a higher priority, i.e., lower ``priority`` number).
    """
    return [
        PermissionRule(
            tool_pattern="Read",
            action="allow",
            priority=100,
            description="File reading is always safe (read-only).",
        ),
        PermissionRule(
            tool_pattern="Glob",
            action="allow",
            priority=100,
            description="Glob file listing is always safe (read-only).",
        ),
        PermissionRule(
            tool_pattern="Grep",
            action="allow",
            priority=100,
            description="Content searching is always safe (read-only).",
        ),
        PermissionRule(
            tool_pattern="Bash",
            action="ask",
            priority=100,
            description="Shell commands require explicit approval.",
        ),
        PermissionRule(
            tool_pattern="Edit",
            action="ask",
            priority=100,
            description="File editing requires approval.",
        ),
        PermissionRule(
            tool_pattern="Write",
            action="ask",
            priority=100,
            description="File writing requires approval.",
        ),
    ]
