"""
Hard-truncation logic for the agent query loop.

When the message history exceeds 80 % of the model's context window,
older rounds are dropped.  A *round* is defined as one ``role: "user"``
message together with zero or more following ``role: "assistant"``
messages.  Truncation always cuts at round boundaries so that the
remaining history is structurally valid.
"""

from __future__ import annotations

from typing import Any

from .budget import estimate_tokens_from_messages
from ..compression.trigger import _partition_rounds


def hard_truncate(
    messages: list[dict[str, Any]],
    max_tokens: int,
    min_rounds: int = 2,
) -> list[dict[str, Any]]:
    """Drop the oldest rounds until the history fits within *max_tokens*.

    Parameters
    ----------
    messages:
        The full message list in chronological order (oldest first).
    max_tokens:
        Maximum estimated token count for the truncated result.
    min_rounds:
        Minimum number of complete rounds to preserve, even if they
        exceed *max_tokens*.

    Returns
    -------
    list[dict]
        A (possibly shorter) message list.  When truncation actually
        drops messages a ``<system-reminder>`` user message is
        prepended.
    """
    if not messages:
        return list(messages)

    # -- 1. Partition messages into rounds (oldest → newest) ---------------
    rounds = _partition_rounds(messages)

    # -- 2. Walk backwards, accumulating the most recent rounds ------------
    kept_rounds: list[list[dict[str, Any]]] = []
    cumulative: list[dict[str, Any]] = []

    for rnd in reversed(rounds):
        candidate = rnd + cumulative  # prepend this round
        est = estimate_tokens_from_messages(candidate)
        if est > max_tokens and len(kept_rounds) >= min_rounds:
            break
        kept_rounds.insert(0, rnd)
        cumulative = candidate

    # -- 3. If nothing was dropped, return unchanged -----------------------
    if len(kept_rounds) == len(rounds):
        return list(messages)

    # -- 4. Build truncated result with a notice ---------------------------
    truncated: list[dict[str, Any]] = []
    notice = _TRUNCATION_NOTICE.format(
        rounds=len(kept_rounds), total_rounds=len(rounds)
    )
    truncated.append({
        "role": "user",
        "content": [{"type": "text", "text": notice}],
    })
    for rnd in kept_rounds:
        truncated.extend(rnd)

    return truncated


_TRUNCATION_NOTICE = (
    "<system-reminder>\n"
    "Earlier conversation history has been truncated to stay within "
    "the model's context window.  The most recent {rounds} of "
    "{total_rounds} exchange rounds have been preserved.\n"
    "</system-reminder>"
)
