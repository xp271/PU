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

    workspace_override: str | None = None
    """QQBot can dynamically set workspace override.

    When non-None, Agent creation uses this instead of config.workspace_root.
    CLI/REPL never sets this field, so existing behavior is unaffected.
    """

    # -- checkpoint metadata (v1) ---------------------------------------------
    _created_at: str | None = field(default=None, repr=False)
    """ISO timestamp set on first ``save_checkpoint``.  Read-only after set."""

    _last_active_at: str | None = field(default=None, repr=False)
    """ISO timestamp updated on every ``save_checkpoint``."""

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
        self._written_count = 0

    # ------------------------------------------------------------------
    # Checkpoint (v1): lightweight snapshot for cross-process resume
    # ------------------------------------------------------------------

    def checkpoint_dict(self) -> dict[str, object]:
        """Generate a JSON-serializable checkpoint snapshot.

        Extracts the first user message text as *title* (truncated 100 chars).
        """
        title = "(新会话)"
        for msg in self.messages:
            if msg.get("role") == "user":
                content = msg.get("content", [])
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text":
                            t = b.get("text", "") or "(空消息)"
                            title = t[:100].replace("\n", " ")
                            break
                elif isinstance(content, str):
                    title = content[:100].replace("\n", " ")
                break

        return {
            "version": 1,
            "session_id": self.session_id,
            "title": title,
            "created_at": self._created_at,
            "last_active_at": self._last_active_at,
            "message_count": len(self.messages),
            "written_count": self._written_count,
            "compaction_count": self._compaction_count,
            "workspace_override": self.workspace_override,
            "permission_rules": self.permission_store.to_rules_list(),
        }

    def restore_checkpoint(self, data: dict[str, object]) -> None:
        """Apply checkpoint metadata to this session (in-place, idempotent).

        Only restores fields NOT already set by ``load_session`` from JSONL.
        ``_written_count`` and ``_compaction_count`` are **not** overwritten —
        JSONL is the authoritative source for message counts; checkpoint
        values are a snapshot that may be stale if JSONL was rebuilt.
        """
        self._created_at = data.get("created_at") or self._created_at
        self._last_active_at = data.get("last_active_at") or self._last_active_at
        if data.get("workspace_override") is not None:
            self.workspace_override = str(data["workspace_override"])
        rules = data.get("permission_rules", [])
        if rules:
            self.permission_store.restore_from_rules(rules)  # type: ignore[arg-type]

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
