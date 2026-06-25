"""Permission system — rule engine, inheritance contracts, bash safety."""

from .bash_guard import BashSafetyResult, analyze_bash_command
from .engine import PermissionCallback, PermissionDecision, PermissionEngine, PermissionOutcome
from .inheritance import (
    compute_child_allowed_tools,
    compute_child_disallowed_tools,
    compute_child_permission_mode,
)
from .rules import PermissionRule
from .store import SessionPermissionStore

__all__ = [
    "BashSafetyResult",
    "PermissionCallback",
    "PermissionDecision",
    "PermissionEngine",
    "PermissionOutcome",
    "PermissionRule",
    "SessionPermissionStore",
    "analyze_bash_command",
    "compute_child_allowed_tools",
    "compute_child_disallowed_tools",
    "compute_child_permission_mode",
]
