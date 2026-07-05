"""
Compression trigger logic — round partitioning and compressibility checks.

Does **not** check token thresholds — that is the responsibility of
``TokenBudget.needs_truncation()``.  This module only answers: "given the
current messages, is compression applicable right now?"
"""

from __future__ import annotations

from typing import Any

from ..models import CompressionConfig


def _partition_rounds(
    messages: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Split a flat message list into conversation rounds.

    A round starts with a user message that contains actual text content
    (not just tool results).  This ensures that a complete tool cycle
    (assistant:tool_use → user:tool_result) stays together in the same
    round, preventing orphaned tool messages after compression.
    """
    rounds: list[list[dict[str, Any]]] = []
    current_round: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role", "")
        # Only start a new round when the user message has real text
        # (tool_result-only messages are continuations of the current round)
        if role == "user" and current_round and _has_user_text(msg):
            rounds.append(current_round)
            current_round = []
        current_round.append(msg)

    if current_round:
        rounds.append(current_round)

    return rounds


def _has_user_text(msg: dict[str, Any]) -> bool:
    """Return True if the user message contains any text (not just tool results)."""
    content = msg.get("content", [])
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return any(
            isinstance(block, dict) and block.get("type") == "text"
            for block in content
        )
    return False


def can_compress(
    messages: list[dict[str, Any]],
    config: CompressionConfig,
) -> bool:
    """Return True if compression should be attempted for *messages*.

    Conditions (all must be satisfied):
    1. ``config.enabled`` is True
    2. There are more rounds than ``config.keep_recent_rounds``
       (i.e. there *are* old rounds worth compressing)

    Token-threshold checks are deliberately absent — they are the
    responsibility of ``TokenBudget.needs_truncation()``.
    """
    if not config.enabled:
        return False
    rounds = _partition_rounds(messages)
    return len(rounds) > config.keep_recent_rounds
