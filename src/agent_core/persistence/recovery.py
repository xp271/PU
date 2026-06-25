"""
Recovery & cleaning — reconstruct message lists from ``TranscriptEntry``
sequences.

Two reconstruction paths:

* **LIVE** (``reconstruct_live_messages`` / ``reconstruct_messages``):
  segment-chain algorithm that returns the current "active" message list
  suitable for feeding back into the LLM.  Compaction checkpoints reset
  the base; pre-compaction entries are logically discarded (but remain
  on disk for audit).

* **FULL** (``reconstruct_full_history``): preserves every original
  message entry split into ``HistorySegment``\\s demarcated by
  checkpoints.  Useful for audit, export, and debugging.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .storage import CompactionRecord, TranscriptEntry

logger = logging.getLogger("agent_core.persistence.recovery")


# ---------------------------------------------------------------------------
# walk_entry_chain — retained for future fork / audit chain validation
# ---------------------------------------------------------------------------


def walk_entry_chain(
    entries: list[TranscriptEntry],
) -> list[TranscriptEntry]:
    """Walk from the last entry back to root via ``parentUuid``.

    Returns entries ordered root→leaf.  In v1 the chain is strictly
    linear, so this is equivalent to a simple copy — retained for
    future fork/merge support and for FULL-audit chain validation.
    """
    if not entries:
        return []

    index: dict[str, TranscriptEntry] = {}
    for e in entries:
        index[e.uuid] = e

    chain_ids: list[str] = []
    seen: set[str] = set()
    cursor = entries[-1]

    while cursor is not None and cursor.uuid not in seen:
        chain_ids.append(cursor.uuid)
        seen.add(cursor.uuid)
        puid = cursor.parentUuid
        cursor = index.get(puid) if puid else None

    chain_ids.reverse()
    result: list[TranscriptEntry] = []
    for uid in chain_ids:
        entry = index.get(uid)
        if entry is not None:
            result.append(entry)
    return result


# ---------------------------------------------------------------------------
# Cleaning helpers
# ---------------------------------------------------------------------------


def filter_unresolved_tool_uses(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop ``tool_use`` blocks with no matching ``tool_result``.

    Delegates to the single existing implementation in :mod:`agents.fork`.
    """
    from agent_core.agents.fork import filter_incomplete_tool_calls

    return filter_incomplete_tool_calls(messages)


def filter_orphaned_thinking(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove assistant messages whose *only* content is orphaned
    ``thinking`` blocks.

    **Safety constraints (problem #8):**

    * A message with ``thinking + tool_use`` (even without text) is the
      normal extended-thinking shape and must NOT be removed — doing so
      would orphan its ``tool_result``.
    * A message with ``thinking + non-empty text`` is also valid.
    * Only messages whose content list is **solely** ``thinking`` blocks
      (no ``tool_use``, no non-empty ``text``) are removed.

    Returns a new list without the orphaned-thinking messages.
    """
    result: list[dict[str, Any]] = []
    for msg in messages:
        if _is_orphaned_thinking(msg):
            logger.debug("Removing orphaned-thinking assistant message")
            continue
        result.append(msg)
    return result


def _is_orphaned_thinking(msg: dict[str, Any]) -> bool:
    """Return True when *msg* is an assistant message whose content is
    **solely** thinking/redacted_thinking blocks."""
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content")
    if not isinstance(content, list) or not content:
        return True  # empty assistant → orphaned

    has_thinking = False
    for block in content:
        if not isinstance(block, dict):
            return False
        bt = block.get("type", "")
        if bt in ("thinking", "redacted_thinking"):
            has_thinking = True
        elif bt == "tool_use":
            return False  # NOT orphaned — has tool_use  (problem #8)
        elif bt == "text":
            text = block.get("text", "")
            if text.strip():
                return False  # NOT orphaned — has meaningful text
    return has_thinking  # only thinking blocks → orphaned


# ---------------------------------------------------------------------------
# Turn interruption detection
# ---------------------------------------------------------------------------


@dataclass
class TurnInterruption:
    interrupted: bool
    last_user_message: dict[str, Any] | None = None
    pending_tool_uses: list[str] = field(default_factory=list)
    detail: str = ""


def detect_turn_interruption(
    messages: list[dict[str, Any]],
) -> TurnInterruption:
    """Detect whether the session was interrupted mid-turn.

    Conditions:
    1. Last message ``role="user"`` (not ``tool_result``) → user sent a
       prompt but the LLM never responded.
    2. Last assistant message contains ``tool_use`` blocks whose results
       are missing from the subsequent user message → tools were called
       but never executed.

    The caller can optionally inject a ``"Continue…"`` user prompt so
    the agent resumes where it left off.
    """
    if not messages:
        return TurnInterruption(interrupted=False)

    last = messages[-1]

    # Condition 1: user message as last → prompt sent, no LLM response
    if last.get("role") == "user":
        # Check if it's a tool_result user message → NOT interrupted
        content = last.get("content")
        if isinstance(content, list) and content:
            has_tr = any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            )
            if not has_tr:
                return TurnInterruption(
                    interrupted=True,
                    last_user_message=last,
                    detail="Last message is a user prompt with no assistant response",
                )

    # Condition 2: incomplete tool_uses
    # Collect all tool_use IDs and tool_result tool_use_ids
    tool_use_ids: set[str] = set()
    completed_ids: set[str] = set()
    for msg in messages:
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tid = block.get("id", "")
                if tid:
                    tool_use_ids.add(tid)
            elif block.get("type") == "tool_result":
                tid = block.get("tool_use_id", "")
                if tid:
                    completed_ids.add(tid)

    pending = sorted(tool_use_ids - completed_ids)
    if pending:
        return TurnInterruption(
            interrupted=True,
            pending_tool_uses=pending,
            detail=f"{len(pending)} tool use(s) without results",
        )

    return TurnInterruption(interrupted=False)


# ---------------------------------------------------------------------------
# LIVE reconstruction — segment chain (problem #3)
# ---------------------------------------------------------------------------


def reconstruct_live_messages(
    entries: list[TranscriptEntry],
) -> list[dict[str, Any]]:
    """Build the *current live* message list via segment-chain algorithm.

    Algorithm (single pass, O(n))::

        base = []           # last checkpoint snapshot
        tail = []           # message entries after last checkpoint
        for e in entries (file order, isSidechain filtered out):
            if e.type == "compaction":
                base = e.compaction.snapshot   # reset
                tail = []
            elif e.type == "message" and e.message is not None:
                tail.append(e.message)
        return base + tail

    The ``snapshot`` is self-contained, so the algorithm is **not
    recursive** — we can jump to any checkpoint's snapshot without
    re-reading earlier segments.

    When no checkpoints exist, this degenerates to concatenating all
    message entries in file order.
    """
    base: list[dict[str, Any]] = []
    tail: list[dict[str, Any]] = []

    for e in entries:
        if e.isSidechain:
            continue
        if e.type == "compaction" and e.compaction is not None:
            base = list(e.compaction.snapshot)
            tail = []
        elif e.type == "message" and e.message is not None:
            tail.append(e.message)

    return base + tail


# ---------------------------------------------------------------------------
# FULL reconstruction — audit view (problem #3)
# ---------------------------------------------------------------------------


@dataclass
class HistorySegment:
    """One contiguous segment of the transcript, ending at a checkpoint
    (or the end of file for the final segment)."""

    index: int
    messages: list[dict[str, Any]]
    compaction: CompactionRecord | None = None
    """The checkpoint that closes this segment.  ``None`` for the last
    (still-open) segment."""


def reconstruct_full_history(
    entries: list[TranscriptEntry],
) -> list[HistorySegment]:
    """Reconstruct the **complete, unpruned** history split into segments
    by compaction checkpoints.

    Unlike LIVE reconstruction, no original messages are discarded.
    Each segment's ``compaction`` field records *what* happened to that
    segment's messages (they were summarized into a checkpoint), enabling
    audit tools to trace "these N messages → this summary".
    """
    if not entries:
        return []

    # Filter to only message/compaction entries (no sidechain)
    filtered = [e for e in entries if not e.isSidechain]

    segments: list[HistorySegment] = []
    current_msgs: list[dict[str, Any]] = []
    seg_idx = 0

    for e in filtered:
        if e.type == "compaction" and e.compaction is not None:
            segments.append(
                HistorySegment(
                    index=seg_idx,
                    messages=current_msgs,
                    compaction=e.compaction,
                )
            )
            seg_idx += 1
            current_msgs = []
        elif e.type == "message" and e.message is not None:
            current_msgs.append(e.message)

    # Final open segment
    segments.append(
        HistorySegment(index=seg_idx, messages=current_msgs, compaction=None)
    )

    return segments


# ---------------------------------------------------------------------------
# Clean-and-rebuild one-stop entry point
# ---------------------------------------------------------------------------


@dataclass
class RecoveryResult:
    messages: list[dict[str, Any]]
    """LIVE message list (after cleaning), ready for the next turn."""

    interruption: TurnInterruption | None

    entries_loaded: int
    entries_kept: int
    tool_uses_removed: int
    warnings: list[str]

    # -- segment chain info (problem #3) --
    compaction_count: int = 0
    written_count: int = 0
    """LIVE length *before* cleaning — used to pre-set
    ``SessionState._written_count`` so ``begin_persistence``
    does not re-append history (problem #1)."""

    full_segments: list[HistorySegment] | None = None
    """FULL audit segments (only populated when ``want_full=True``)."""


async def reconstruct_messages(
    entries: list[TranscriptEntry],
) -> tuple[list[dict[str, Any]], TurnInterruption | None]:
    """LIVE reconstruction + cleaning (convenience wrapper)."""
    live = reconstruct_live_messages(entries)
    cleaned = filter_unresolved_tool_uses(live)
    cleaned = filter_orphaned_thinking(cleaned)
    interruption = detect_turn_interruption(cleaned)
    return cleaned, interruption


async def clean_and_rebuild(
    entries: list[TranscriptEntry],
    *,
    include_sidechain: bool = False,
    want_full: bool = False,
) -> RecoveryResult:
    """Full clean-and-rebuild pipeline.

    Parameters:
        entries: Parsed ``TranscriptEntry`` list.
        include_sidechain: If ``True``, sidechain entries are included
            in the LIVE reconstruction (for sub-agent recovery).
        want_full: If ``True``, also populate ``full_segments`` (audit).
    """
    warnings: list[str] = []

    if not include_sidechain:
        relevant = [e for e in entries if not e.isSidechain]
    else:
        relevant = list(entries)

    # ---- LIVE reconstruction ----
    live = reconstruct_live_messages(relevant)

    # written_count = LIVE length BEFORE cleaning (problem #1)
    written_count = len(live)

    before_cleaning = len(live)
    cleaned = filter_unresolved_tool_uses(live)
    tool_uses_removed = before_cleaning - len(cleaned)

    cleaned = filter_orphaned_thinking(cleaned)
    if len(cleaned) < before_cleaning - tool_uses_removed:
        warnings.append(
            f"Removed {before_cleaning - tool_uses_removed - len(cleaned)} "
            "orphaned-thinking messages"
        )

    interruption = detect_turn_interruption(cleaned)

    # ---- Compaction count ----
    compaction_count = sum(
        1 for e in relevant if e.type == "compaction"
    )

    # ---- FULL audit (optional) ----
    full_segments = None
    if want_full:
        full_segments = reconstruct_full_history(relevant)

    return RecoveryResult(
        messages=cleaned,
        interruption=interruption,
        entries_loaded=len(entries),
        entries_kept=len(cleaned),
        tool_uses_removed=tool_uses_removed,
        warnings=warnings,
        compaction_count=compaction_count,
        written_count=written_count,
        full_segments=full_segments,
    )
