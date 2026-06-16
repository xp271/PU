"""
Permission engine — rule matching chain and ``PermissionMode`` integration.

The engine evaluates a tool-use request against an ordered rule set
and the configured ``PermissionMode``, optionally delegating to a
caller-provided callback for interactive approval.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models import PermissionMode
from ..tools.registry import registered_tools
from .rules import PermissionAction, PermissionRule, default_rules

# ---- Permission callback (shared with agent.py) ------------------------------

PermissionCallback = Callable[[str, dict[str, Any]], Awaitable[bool]]


# ---- Decision model ---------------------------------------------------------

class PermissionDecision(BaseModel):
    """Result of a permission check by the engine."""

    allowed: bool
    reason: str = ""
    source: Literal["rule", "mode", "callback", "default"] = "default"


# ---- Engine -----------------------------------------------------------------

class PermissionEngine:
    """Resolve tool-use permission via a priority-ordered rule chain.

    The matching chain (step 1 through 7):
      1. exact ``tool_pattern`` match
      2. prefix match (pattern ends with ``*``)
      3. fnmatch glob wildcard
      4. ``path_constraint`` check (if rule has one)
      5. ``PermissionMode`` override (bypass / acceptEdits / ask)
      6. read-only fallback (allow read-only tools)
      7. default — ask (delegate to callback if available, else deny for destructive)
    """

    def __init__(
        self,
        rules: list[PermissionRule] | None = None,
        mode: PermissionMode = "ask",
    ) -> None:
        rules = list(rules) if rules is not None else default_rules()
        # Sort by (priority ascending, then exact > prefix > wildcard)
        rules.sort(key=_rule_sort_key)
        self._rules = rules
        self._mode: PermissionMode = mode

    # ---- public API --------------------------------------------------------

    @property
    def mode(self) -> PermissionMode:
        return self._mode

    async def check(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        callback: PermissionCallback | None,
        tool_is_read_only: bool = False,
        tool_is_destructive: bool = False,
    ) -> PermissionDecision:
        """Run the full matching chain and return a decision.

        **Args:**
            tool_name: The tool being invoked (e.g. ``"Bash"``).
            tool_input: Raw (un-validated) tool input dict.
            callback: Optional async callback for interactive approval.
            tool_is_read_only: ``True`` when the tool declares ``is_read_only=True``.
            tool_is_destructive: ``True`` when ``is_destructive=True``.
        """
        # ---- step 1–4: rule matching ----------------------------------------
        decision = _match_rules(tool_name, tool_input, self._rules)
        if decision is not None:
            return decision

        # ---- step 5: PermissionMode override --------------------------------
        decision = _check_mode(
            self._mode, tool_is_read_only, tool_is_destructive
        )
        if decision is not None:
            return decision

        # ---- step 6: read-only fallback -------------------------------------
        if tool_is_read_only:
            return PermissionDecision(
                allowed=True,
                reason=f"'{tool_name}' is read-only — allowed by default.",
                source="default",
            )

        # ---- step 7: default → ask ------------------------------------------
        return await _ask_callback_or_default(
            tool_name, tool_input, callback, tool_is_destructive
        )

    @classmethod
    def from_config(
        cls,
        permission_mode: PermissionMode = "ask",
        tools: list[str] | None = None,
    ) -> "PermissionEngine":
        """Create an engine from ``AgentConfig`` values.

        ``tools`` is currently unused (rule matching handles filtering)
        but accepted for forward compatibility with agent-level tool
        allowlists.
        """
        return cls(mode=permission_mode)


# ---- internal helpers --------------------------------------------------------


def _rule_sort_key(rule: PermissionRule) -> tuple[int, int]:
    """Sort key: priority asc, then wildcard-ness asc (exact=0, prefix=1, glob=2)."""
    is_prefix = rule.tool_pattern.endswith("*")
    is_glob = _has_fnmatch_meta(rule.tool_pattern)
    # A pattern that is "exact" has fmt=0, prefix fmt=1, true wildcard fmt=2
    if is_prefix and not _has_fnmatch_meta(rule.tool_pattern[:-1]):
        fmt = 1  # plain prefix "File*"
    elif is_glob:
        fmt = 2  # true wildcard "mcp__*" or "File*" (both are glob)
    else:
        fmt = 0  # exact
    return (rule.priority, fmt)


def _has_fnmatch_meta(pattern: str) -> bool:
    """True when the pattern contains fnmatch meta characters beyond a trailing '*'."""
    # strip trailing '*' for prefix detection
    body = pattern.rstrip("*")
    return any(ch in body for ch in "*?[]")


def _match_rules(
    tool_name: str,
    tool_input: dict[str, Any],
    rules: list[PermissionRule],
) -> PermissionDecision | None:
    for rule in rules:
        if not _pattern_matches(rule.tool_pattern, tool_name):
            continue
        if rule.path_constraint:
            path = _extract_path(tool_input)
            if path and not fnmatch.fnmatch(path, rule.path_constraint):
                continue
        # Matched — map action
        return _decision_for(rule, tool_name)
    return None


def _pattern_matches(pattern: str, tool_name: str) -> bool:
    """Check if *pattern* matches *tool_name*.

    Tries, in order: exact → prefix → fnmatch.
    """
    if pattern == tool_name:
        return True
    if pattern.endswith("*") and tool_name.startswith(pattern[:-1]):
        return True
    if fnmatch.fnmatch(tool_name, pattern):
        return True
    return False


def _extract_path(tool_input: dict[str, Any]) -> str | None:
    """Extract a path-like value from tool input for path_constraint checks."""
    for key in ("file_path", "path", "working_dir"):
        val = tool_input.get(key)
        if isinstance(val, str):
            return val
    return None


def _decision_for(rule: PermissionRule, tool_name: str) -> PermissionDecision:
    if rule.action == "allow":
        return PermissionDecision(
            allowed=True,
            reason=f"Matched rule '{rule.tool_pattern}' (allow): {rule.description}".rstrip(": "),
            source="rule",
        )
    if rule.action == "deny":
        return PermissionDecision(
            allowed=False,
            reason=f"Matched rule '{rule.tool_pattern}' (deny): {rule.description}".rstrip(": "),
            source="rule",
        )
    # action == "ask" → fall through to callback/mode handling
    return None


def _check_mode(
    mode: PermissionMode,
    tool_is_read_only: bool,
    tool_is_destructive: bool,
) -> PermissionDecision | None:
    if mode == "bypassPermissions":
        return PermissionDecision(
            allowed=True,
            reason="Permission mode: bypassPermissions — all tools allowed.",
            source="mode",
        )
    if mode == "acceptEdits" and not tool_is_destructive:
        return PermissionDecision(
            allowed=True,
            reason="Permission mode: acceptEdits — non-destructive tools allowed.",
            source="mode",
        )
    # acceptEdits with destructive: fall through to ask
    # ask: fall through
    return None


async def _ask_callback_or_default(
    tool_name: str,
    tool_input: dict[str, Any],
    callback: PermissionCallback | None,
    tool_is_destructive: bool,
) -> PermissionDecision:
    if callback is not None:
        try:
            allowed = await callback(tool_name, tool_input)
        except Exception:
            return PermissionDecision(
                allowed=False,
                reason="Permission callback raised an exception.",
                source="callback",
            )
        if allowed:
            return PermissionDecision(
                allowed=True, reason="User approved.", source="callback"
            )
        return PermissionDecision(
            allowed=False, reason="User denied.", source="callback"
        )

    # No callback available
    if tool_is_destructive:
        return PermissionDecision(
            allowed=False,
            reason=(
                f"No permission callback configured and '{tool_name}' is "
                "destructive — denied by default."
            ),
            source="default",
        )
    # Non-destructive, no callback: allow by default
    return PermissionDecision(
        allowed=True,
        reason=f"'{tool_name}' is non-destructive and no callback is configured.",
        source="default",
    )
