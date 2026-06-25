"""
Permission engine — rule matching chain and ``PermissionMode`` integration.

The engine evaluates a tool-use request against an ordered rule set
and the configured ``PermissionMode``, optionally delegating to a
caller-provided callback for interactive approval.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models import PermissionMode
from ..tools.registry import registered_tools
from .rules import PermissionAction, PermissionRule, default_rules
from .store import SessionPermissionStore


# ---- Permission outcome ------------------------------------------------------

class PermissionOutcome(str, Enum):
    """Richer result of an interactive permission prompt.

    ``*_ONCE`` apply only to the current call; ``*_SESSION`` additionally
    record a session-scoped rule (via :class:`SessionPermissionStore`) so the
    same tool is auto-decided for the rest of the session.
    """

    ALLOW_ONCE = "allow_once"        # y
    DENY_ONCE = "deny_once"          # n
    ALLOW_SESSION = "allow_session"  # s
    DENY_SESSION = "deny_session"    # r


def _coerce_outcome(raw: Any) -> PermissionOutcome:
    """Map a callback return value to a :class:`PermissionOutcome`.

    Accepts a :class:`PermissionOutcome` directly, or a ``bool`` for backward
    compatibility with the legacy ``Awaitable[bool]`` callback signature.
    """
    if isinstance(raw, PermissionOutcome):
        return raw
    return PermissionOutcome.ALLOW_ONCE if raw else PermissionOutcome.DENY_ONCE


# ---- Permission callback (shared with agent.py) ------------------------------

# New callbacks return a ``PermissionOutcome``; legacy callbacks returning
# ``bool`` are still accepted (see ``_coerce_outcome``).
PermissionCallback = Callable[
    [str, dict[str, Any]], Awaitable["PermissionOutcome | bool"]
]


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
        session_store: SessionPermissionStore | None = None,
    ) -> None:
        rules = list(rules) if rules is not None else default_rules()
        # Sort by (priority ascending, then exact > prefix > wildcard)
        rules.sort(key=_rule_sort_key)
        self._rules = rules
        self._mode: PermissionMode = mode
        # Borrowed, not owned: the store outlives this engine instance so
        # session-remembered decisions survive across per-prompt rebuilds.
        self._session_store = session_store

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
        # Session-remembered rules participate as ordinary rules — they sort
        # ahead of builtins by their lower priority number (10 / 20 < 100),
        # so a remembered allow/deny is consulted before defaults but does not
        # bypass the chain entirely.
        rules = self._rules
        if self._session_store is not None:
            session_rules = self._session_store.rules()
            if session_rules:
                rules = sorted(self._rules + session_rules, key=_rule_sort_key)
        decision = _match_rules(tool_name, tool_input, rules)
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
        return await self._ask(
            tool_name, tool_input, callback, tool_is_destructive
        )

    async def _ask(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        callback: PermissionCallback | None,
        tool_is_destructive: bool,
    ) -> PermissionDecision:
        """Delegate to the interactive callback (if any), else apply defaults.

        Translates the callback's :class:`PermissionOutcome` into a decision,
        and — for ``*_SESSION`` outcomes — records a rule in the session store
        so the same tool is auto-decided on subsequent calls.
        """
        if callback is not None:
            try:
                raw = await callback(tool_name, tool_input)
            except BaseException:
                # Covers KeyboardInterrupt (Ctrl+C) too — treat cancel as a
                # one-time deny rather than letting it crash the turn loop.
                return PermissionDecision(
                    allowed=False,
                    reason="Permission prompt cancelled / callback error.",
                    source="callback",
                )

            outcome = _coerce_outcome(raw)
            if self._session_store is not None:
                if outcome is PermissionOutcome.ALLOW_SESSION:
                    self._session_store.add_allow(tool_name)
                elif outcome is PermissionOutcome.DENY_SESSION:
                    self._session_store.add_deny(tool_name)

            allowed = outcome in (
                PermissionOutcome.ALLOW_ONCE,
                PermissionOutcome.ALLOW_SESSION,
            )
            return PermissionDecision(
                allowed=allowed,
                reason="User approved." if allowed else "User denied.",
                source="callback",
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

    @classmethod
    def from_config(
        cls,
        permission_mode: PermissionMode = "ask",
        tools: list[str] | None = None,
        session_store: SessionPermissionStore | None = None,
    ) -> "PermissionEngine":
        """Create an engine from ``AgentConfig`` values.

        ``tools`` is currently unused (rule matching handles filtering)
        but accepted for forward compatibility with agent-level tool
        allowlists.

        ``session_store`` is the long-lived session-permission memory; pass it
        through so remembered ``s`` / ``r`` decisions survive engine rebuilds.
        """
        return cls(mode=permission_mode, session_store=session_store)


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


