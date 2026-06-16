"""
Sub-agent permission inheritance — pure-function contracts.

These functions define the rules for how a child agent's permissions
are derived from its parent's active permissions and the child's own
``AgentDefinition`` configuration.

**Invariants:**
- A child agent can **never** elevate its own permission level.
- A child agent can **never** use tools forbidden by its parent.
- A child agent can **only** use tools that both it and its parent allow.

When ``AgentDefinition`` is implemented (Item 7), these functions will
be called directly by the ``run_agent()`` entry point.
"""

from __future__ import annotations

from ..models import PermissionMode


# PermissionMode severity ordering (higher index = more permissive).
_MODE_RANK: dict[PermissionMode, int] = {
    "bypassPermissions": 3,
    "acceptEdits": 2,
    "ask": 1,
}


def compute_child_permission_mode(
    parent_mode: PermissionMode,
    child_definition_mode: PermissionMode | None,
) -> PermissionMode:
    """Compute the effective permission mode for a child agent.

    Rule: ``child_effective = min(parent_actual, child_definition)``

    *bypassPermissions* > *acceptEdits* > *ask* — the child can only
    be *less* (or equally) permissive than its parent.

    **Args:**
        parent_mode: The effective ``PermissionMode`` the parent is running under.
        child_definition_mode: The mode declared in the child's
            ``AgentDefinition``, or ``None`` to inherit the parent mode directly.

    **Returns:**
        The effective ``PermissionMode`` for the child agent.
    """
    if child_definition_mode is None:
        return parent_mode

    parent_rank = _MODE_RANK[parent_mode]
    child_rank = _MODE_RANK[child_definition_mode]
    # pick the less-permissive one
    if child_rank <= parent_rank:
        return child_definition_mode
    return parent_mode


def compute_child_allowed_tools(
    parent_allowed: frozenset[str],
    child_allowed_spec: list[str],
) -> frozenset[str]:
    """Compute the effective allowed-tools set for a child agent.

    Rule: ``child = parent_allowed ∩ child_definition_allowed``

    The child's specification ``["*"]`` means "all tools the parent
    itself is allowed to use".

    **Args:**
        parent_allowed: The set of tool names the parent agent may use.
        child_allowed_spec: The ``tools`` list from the child's
            ``AgentDefinition`` (e.g. ``["*"]`` or ``["Read", "Glob"]``).

    **Returns:**
        The effective allowed-tool set for the child.
    """
    if child_allowed_spec == ["*"]:
        return parent_allowed

    child_set = frozenset(child_allowed_spec)
    return parent_allowed & child_set


def compute_child_disallowed_tools(
    parent_disallowed: frozenset[str],
    child_disallowed_spec: list[str],
) -> frozenset[str]:
    """Compute the effective disallowed-tools set for a child agent.

    Rule: ``child = parent_disallowed ∪ child_definition_disallowed``

    The parent's forbidden tools are **always** forbidden to the child.
    The child can only add *more* restrictions.

    **Args:**
        parent_disallowed: The set of tool names the parent agent may NOT use.
        child_disallowed_spec: The ``disallowed_tools`` list from the child's
            ``AgentDefinition``.

    **Returns:**
        The effective disallowed-tool set for the child (union of both).
    """
    return parent_disallowed | frozenset(child_disallowed_spec)
