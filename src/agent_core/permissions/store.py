"""
Session-scoped permission memory.

A :class:`SessionPermissionStore` holds the allow/deny decisions a user chose
to *remember* for the duration of an interactive session (the ``s`` / ``r``
options in the permission prompt).  It models each remembered decision as an
ordinary :class:`PermissionRule` tagged ``source="session"`` so the existing
rule-matching chain consumes them with no special-casing — the only thing that
makes a memory "session-scoped" is *where it lives* (this in-memory container,
held by the long-lived session runtime) and that it is never persisted to disk.

The store is created once per session (by the REPL / SDK entry point) and
injected down into every (re-created) :class:`PermissionEngine`, so the memory
survives across prompts even though the engine itself is rebuilt each turn.
"""

from __future__ import annotations

from .rules import PermissionAction, PermissionRule

# Priority convention (see PermissionRule docstring): session decisions outrank
# builtin/config rules (priority 100); deny outranks allow.
_SESSION_DENY_PRIORITY = 10
_SESSION_ALLOW_PRIORITY = 20


class SessionPermissionStore:
    """In-memory container of session-remembered permission rules.

    Lifetime equals the interactive session.  Not persisted (cross-session
    persistence is a separate, orthogonal concern — Item 10).
    """

    def __init__(self) -> None:
        self._rules: list[PermissionRule] = []

    def rules(self) -> list[PermissionRule]:
        """Return a shallow copy of the remembered rules."""
        return list(self._rules)

    def add_allow(self, tool_name: str, rule_content: str | None = None) -> None:
        self._add("allow", tool_name, rule_content)

    def add_deny(self, tool_name: str, rule_content: str | None = None) -> None:
        self._add("deny", tool_name, rule_content)

    def clear(self) -> None:
        self._rules.clear()

    # -- internal -----------------------------------------------------------

    def _add(
        self,
        action: PermissionAction,
        tool_name: str,
        rule_content: str | None,
    ) -> None:
        priority = (
            _SESSION_DENY_PRIORITY if action == "deny" else _SESSION_ALLOW_PRIORITY
        )
        rule = PermissionRule(
            tool_pattern=tool_name,
            action=action,
            priority=priority,
            rule_content=rule_content,
            source="session",
            description=f"Session {action} for '{tool_name}'.",
        )
        # Idempotent: skip if an identical rule is already remembered.
        dumped = rule.model_dump()
        if any(r.model_dump() == dumped for r in self._rules):
            return
        self._rules.append(rule)
