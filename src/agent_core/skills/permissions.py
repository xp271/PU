"""
Skill permission engine — match skill names against allow/deny/ask rules.

Patterns use ``fnmatch`` globs (``*`` / ``?`` / ``[...]``).
"""

from __future__ import annotations

import fnmatch

from pydantic import BaseModel, Field


class SkillPermissionRule(BaseModel):
    """A single permission rule for skill execution."""

    pattern: str
    """fnmatch glob (e.g. ``"review:*"``, ``"dangerous-*"``, ``"*"``)."""

    action: str = "ask"  # allow | deny | ask
    priority: int = 100
    description: str = ""


class SkillPermissionEngine:
    """Checks whether a skill is allowed, denied, or needs confirmation.

    Rules are sorted by *priority* (ascending).  The first matching rule
    wins.  If no rule matches the skill name, the engine returns ``"ask"``
    (the secure default).
    """

    def __init__(
        self, rules: list[SkillPermissionRule] | None = None
    ) -> None:
        self._rules = sorted(
            rules or [],
            key=lambda r: r.priority,
        )
        # Ensure a default catch-all if none provided.
        if not self._rules:
            self._rules.append(
                SkillPermissionRule(pattern="*", action="ask", priority=1000)
            )

    def check(self, skill_name: str) -> str:
        """Return ``"allow"``, ``"deny"``, or ``"ask"`` for *skill_name*."""
        for rule in self._rules:
            if fnmatch.fnmatch(skill_name, rule.pattern):
                return rule.action
        return "ask"


def default_skill_permissions() -> SkillPermissionEngine:
    """Return the built-in ``SkillPermissionEngine`` with safe defaults."""
    return SkillPermissionEngine(
        rules=[SkillPermissionRule(pattern="*", action="ask")]
    )
