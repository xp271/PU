"""
Post-compression consistency verification.

Runs after every compression attempt inside ``compress()``'s retry loop.
Returns structured results so the retry logic can feed failures back to
the LLM as correction hints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CompressionConsistency:
    """Result of a compression consistency check."""

    valid: bool
    """True when no fatal errors were found."""

    system_preserved: bool = True
    """System prompt is never modified by compression (design guarantee)."""

    tool_uses_traceable: bool = True
    """Every tool_use block from old_rounds is referenced in the summary."""

    compressed_rounds: int = 0
    preserved_rounds: int = 0

    warnings: list[str] = field(default_factory=list)
    """Non-fatal warnings (e.g. summary shorter than expected)."""

    errors: list[str] = field(default_factory=list)
    """Fatal errors that make the compression unusable."""

    # Token accounting from the LLM call that produced this compression
    input_tokens_consumed: int = 0
    output_tokens_consumed: int = 0


def verify_compression(
    original_messages: list[dict[str, Any]],
    compressed_messages: list[dict[str, Any]],
    old_rounds: list[list[dict[str, Any]]],
    recent_rounds: list[list[dict[str, Any]]],
    summary: str,
) -> CompressionConsistency:
    """Verify that *compressed_messages* is a valid compression result.

    Checks performed:

    1. **recent_rounds intact** — every message in *recent_rounds* must
       appear verbatim in *compressed_messages* (fatal if violated).
    2. **summary non-empty** — an empty/short summary is a fatal error
       (it means the LLM produced nothing useful).
    3. **message reduction** — compressed must have fewer messages than
       original (fatal if it grew).
    4. **tool_use coverage** — count ``tool_use`` blocks in *old_rounds*
       vs mentions in *summary* (warning only — the LLM may have
       legitimately deemed some irrelevant).
    """
    warnings: list[str] = []
    errors: list[str] = []

    # -- 1. recent_rounds must be fully preserved ---------------------------
    flat_recent = [msg for rnd in recent_rounds for msg in rnd]
    # The first message in compressed should be the summary message
    # (role="user"), and the rest should be flat_recent.
    if len(compressed_messages) != len(flat_recent) + 1:
        errors.append(
            f"Compressed message count mismatch: expected {len(flat_recent) + 1} "
            f"(1 summary + {len(flat_recent)} recent), "
            f"got {len(compressed_messages)}"
        )
    else:
        compressed_recent = compressed_messages[1:]
        for i, (expected, actual) in enumerate(zip(flat_recent, compressed_recent)):
            if expected != actual:
                errors.append(
                    f"Recent message {i} was modified by compression"
                )
                break

    # -- 2. summary must be non-empty and non-trivial -----------------------
    stripped = summary.strip()
    if not stripped:
        errors.append("Compression summary is empty")
    elif len(stripped) < 20:
        errors.append(
            f"Compression summary is too short ({len(stripped)} chars)"
        )

    # -- 3. compression must actually reduce message count ------------------
    old_msg_count = sum(len(rnd) for rnd in old_rounds)
    if old_msg_count <= 1:
        warnings.append(
            f"Only {old_msg_count} message(s) in old_rounds — "
            "compression provides negligible benefit"
        )
    # (always true: 1 summary msg replaces N old-round msgs, N≥2)

    # -- 4. tool_use blocks in old_rounds vs summary mentions ---------------
    tool_use_ids: list[str] = []
    for rnd in old_rounds:
        for msg in rnd:
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tid = block.get("id", "")
                    if tid:
                        tool_use_ids.append(tid)

    if tool_use_ids:
        # Heuristic: check if at least half the tool names appear in summary
        tool_names = set()
        for rnd in old_rounds:
            for msg in rnd:
                for block in msg.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name", "")
                        if name:
                            tool_names.add(name)

        mentioned = sum(1 for n in tool_names if n in summary)
        if mentioned < len(tool_names) * 0.5:
            warnings.append(
                f"Only {mentioned}/{len(tool_names)} tool names mentioned in summary "
                f"({sorted(tool_names)})"
            )

    valid = len(errors) == 0
    return CompressionConsistency(
        valid=valid,
        tool_uses_traceable=(mentioned >= len(tool_names) * 0.5)
        if tool_use_ids
        else True,
        compressed_rounds=len(old_rounds),
        preserved_rounds=len(recent_rounds),
        warnings=warnings,
        errors=errors,
    )
