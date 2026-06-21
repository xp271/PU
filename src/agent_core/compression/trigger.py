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

    A *round* starts with a ``role: "user"`` message and includes zero or
    more following ``role: "assistant"`` messages.  Tool-result user
    messages also start new rounds.

    This is the shared partitioning function used by both ``hard_truncate``
    and ``compress``.
    """
    rounds: list[list[dict[str, Any]]] = []
    current_round: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role", "")
        if role == "user" and current_round:
            rounds.append(current_round)
            current_round = []
        current_round.append(msg)

    if current_round:
        rounds.append(current_round)

    return rounds


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
