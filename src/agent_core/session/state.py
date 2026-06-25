"""
Session runtime state — the long-lived container for a single interactive
session (REPL run or SDK session).

The agent's per-prompt machinery (``PermissionEngine``, the ``query`` turn
loop) is rebuilt on every prompt, so any state that must survive *across*
prompts cannot live there.  ``SessionState`` is created once by the entry
point (``cli._repl``) and injected down into each (re-created) ``Agent`` /
``query``, carrying:

  - ``messages``         — conversation history, so prompt N sees prompts 1..N-1
  - ``permission_store`` — session-remembered allow/deny decisions
  - ``_writer``          — optional ``SessionTranscriptWriter`` for Phase B
                           JSONL persistence (Item 10)

``transcript_path`` points to the JSONL file when persistence is active.
In-memory continuity (Phase A) is intentionally independent of disk
persistence (Phase B).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from ..permissions.store import SessionPermissionStore

if TYPE_CHECKING:
    from ..persistence.storage import CompactionRecord, SessionTranscriptWriter

logger = logging.getLogger("agent_core.session")


@dataclass
class SessionState:
    """Mutable runtime state for one interactive session."""

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    messages: list[dict[str, Any]] = field(default_factory=list)
    permission_store: SessionPermissionStore = field(
        default_factory=SessionPermissionStore
    )

    # Reserved for Item 10 (persistence / resume); set by begin_persistence().
    transcript_path: str | None = None

    # -- Phase B: persistence writer ------------------------------------------
    _writer: Any | None = field(default=None, repr=False)
    """``SessionTranscriptWriter`` or ``None``."""

    _written_count: int = field(default=0, repr=False)
    """Number of messages already persisted to disk.  Used as the watermark
    so that ``flush_new_messages`` only writes new messages and
    ``begin_persistence`` does not re-append history on resume (problem #1)."""

    _compaction_count: int = field(default=0, repr=False)
    """Number of compaction checkpoints written so far.  Pre-set by
    ``load_session`` on resume so ``segment_index`` keeps increasing across
    sessions instead of restarting at 1."""

    # ------------------------------------------------------------------
    # In-memory history (Phase A)
    # ------------------------------------------------------------------

    def snapshot_for_next_turn(self) -> list[dict[str, Any]]:
        """History to seed the next prompt's turn loop (``initial_messages``)."""
        return list(self.messages)

    def record_turn_result(self, final_messages: list[dict[str, Any]] | None) -> None:
        """Replace history with the turn's final message list.

        ``final_messages`` already contains the prior history plus everything
        produced this turn (and reflects any in-loop compression), so it is a
        full replacement rather than an append — avoiding double-counting.
        ``None`` (e.g. a stream that never reached ``done``) leaves history
        untouched.
        """
        if final_messages is not None:
            self.messages = list(final_messages)

    def clear_history(self) -> None:
        """Drop conversation history (e.g. a ``/clear`` command). Permission
        memory is left intact."""
        self.messages = []

    # ------------------------------------------------------------------
    # Phase B: persistence lifecycle
    # ------------------------------------------------------------------

    async def begin_persistence(self, path: str) -> None:
        """Start writing to *path* with a :class:`SessionTranscriptWriter`.

        Only writes messages **after** the ``_written_count`` watermark,
        so that a resumed session (whose ``_written_count`` was pre-set
        by ``load_session``) does not re-append already-stored history
        (problem #1).  For a brand-new session ``_written_count`` starts
        at 0 and all pre-existing messages (usually empty) are written.
        """
        from ..persistence.storage import SessionTranscriptWriter

        self.transcript_path = path
        self._writer = SessionTranscriptWriter(path, self.session_id)
        await self._writer.open()

        # Write only messages not yet on disk (problem #1)
        for msg in self.messages[self._written_count:]:
            await self._writer.write_entry(msg)
        self._written_count = len(self.messages)

    async def end_persistence(self) -> None:
        """Flush and close the writer.  Idempotent."""
        if self._writer is not None:
            await self._writer.close()
            self._writer = None

    async def flush_new_messages(
        self, final_messages: list[dict[str, Any]]
    ) -> None:
        """Persist messages from *final_messages* that are not yet on disk.

        **Compaction-aware (problem #3):** when in-loop compression has
        rewritten / shortened the message list, the prefix
        ``self.messages[:_written_count]`` no longer matches
        ``final_messages[:_written_count]``.  In that case we append a
        ``type="compaction"`` checkpoint entry (instead of blindly slicing
        and corrupting the append-only JSONL).  Otherwise we do a simple
        incremental append of the new tail.

        ``final_messages`` may be ``None`` when a turn stream ended without a
        clean ``done`` (mirrors ``record_turn_result``'s tolerance) — treated
        as a no-op so callers need not guard.
        """
        if final_messages is None:
            return
        if self._writer is None or not self._writer.is_open:
            return

        if self._is_compaction(final_messages):
            record = self._build_compaction_record(final_messages)
            await self._writer.write_checkpoint(record)
        else:
            for msg in final_messages[self._written_count:]:
                await self._writer.write_entry(msg)

        self._written_count = len(final_messages)
        # Keep ``messages`` aligned with the persisted watermark so the *next*
        # ``_is_compaction`` comparison is correct even if ``flush_new_messages``
        # is called repeatedly without an interleaving ``record_turn_result``
        # (otherwise pure growth gets misdetected as compaction).
        self.messages = list(final_messages)

    # ------------------------------------------------------------------
    # Compaction detection helpers (problem #3)
    # ------------------------------------------------------------------

    def _is_compaction(
        self, final_messages: list[dict[str, Any]]
    ) -> bool:
        """Return ``True`` if *final_messages* was shortened / rewritten
        relative to the previously persisted prefix, implying an in-loop
        compression occurred."""
        if len(final_messages) < self._written_count:
            return True
        # Prefix mismatch → head was replaced (summary takes its place)
        return (
            self.messages[:self._written_count]
            != final_messages[:self._written_count]
        )

    def _build_compaction_record(
        self, final_messages: list[dict[str, Any]]
    ) -> "CompactionRecord":
        """Construct a :class:`CompactionRecord` from the state before and
        after compression.

        The longest common suffix of ``self.messages`` and
        *final_messages* is the "kept tail"; the remaining prefix
        of *final_messages* is the summary.
        """
        from ..persistence.storage import CompactionRecord

        old = self.messages[:self._written_count]
        new = final_messages

        # Find longest common suffix
        kept_count = 0
        max_suffix = min(len(old), len(new))
        for k in range(1, max_suffix + 1):
            if old[-k] != new[-k]:
                break
            kept_count = k

        summary_count = len(new) - kept_count
        summarized_count = self._written_count - kept_count

        # Map UUID range for audit trace-back
        first_uuid: str | None = None
        last_uuid: str | None = None
        if summarized_count > 0 and self._writer is not None:
            # The writer tracks entries sequentially; the summarized range
            # corresponds to entries [written_count - summarized_count,
            # written_count - kept_count) in the persisted order.
            # For v1 we use sentinel values since we don't have a
            # per-entry UUID index in the writer.
            pass

        # Determine segment index
        prev_cp = (
            self._writer.last_checkpoint_uuid
            if self._writer is not None
            else None
        )
        # segment_index keeps increasing across resumes (pre-set by
        # load_session via _compaction_count).
        segment_index = self._compaction_count + 1
        self._compaction_count = segment_index

        return CompactionRecord(
            segment_index=segment_index,
            snapshot=list(new),  # self-contained snapshot
            summary_count=summary_count,
            kept_count=kept_count,
            summarized_uuid_first=first_uuid,
            summarized_uuid_last=last_uuid,
            summarized_count=summarized_count,
            prev_checkpoint_uuid=prev_cp,
            trigger="token_threshold",
            tokens_before=None,
            tokens_after=None,
        )
