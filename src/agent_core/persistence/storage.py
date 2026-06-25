"""
JSONL read/write — ``TranscriptEntry`` model, ``CompactionRecord`` model,
``SessionTranscriptWriter``, and loading helpers.

The writer is **append-only** — existing entries are never modified or
deleted.  Compaction (in-loop compression) does not rewrite the file;
instead a ``type="compaction"`` checkpoint entry is appended that marks
the segment boundary.  The recoverable "live" message list is always
*last checkpoint snapshot* + *subsequent message entries*.
"""

from __future__ import annotations

import json
import logging
import os
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Literal

import aiofiles
import aiofiles.os
from pydantic import BaseModel, Field

from .path import sidechain_dir as _sidechain_dir_fn

logger = logging.getLogger("agent_core.persistence.storage")


# ---------------------------------------------------------------------------
# CompactionRecord — checkpoint payload  (problem #3)
# ---------------------------------------------------------------------------


class CompactionRecord(BaseModel):
    """Boundary checkpoint for one in-loop compression.

    Design goal: **append-only — never rewrite / delete pre-compaction
    original entries**.  When compression fires, a single
    ``type="compaction"`` checkpoint entry is appended.  Its
    ``snapshot`` field carries the self-contained "live" message list
    after compression (= summary + kept tail); the pre-compaction
    original message entries remain untouched on disk for audit.

    * LIVE recovery: last checkpoint's ``snapshot`` + subsequent entries.
    * FULL audit: all original message entries preserved; checkpoints
      mark segment boundaries (see ``reconstruct_full_history``).
    """

    segment_index: int
    """1-based compaction occurrence count."""

    snapshot: list[dict[str, Any]]
    """Self-contained live message list right after compression
    (= summary prefix + kept tail).  Copying the kept tail into the
    snapshot makes LIVE recovery O(1) and non-recursive (see
    ``reconstruct_live_messages``).  The tail is bounded (~last N
    messages), so the space overhead is bounded."""

    summary_count: int
    """Number of messages in *snapshot* that belong to the summary."""

    kept_count: int
    """Number of messages at the tail of *snapshot* that were preserved
    verbatim from the pre-compaction live list."""

    summarized_uuid_first: str | None = None
    summarized_uuid_last: str | None = None
    """UUID range (inclusive) of the original message entries that the
    summary replaced (for audit trace-back)."""

    summarized_count: int = 0
    """Number of original live messages that were replaced by the
    summary."""

    prev_checkpoint_uuid: str | None = None
    """UUID of the preceding checkpoint entry (segment chain)."""

    trigger: str = "token_threshold"
    """What triggered this compaction."""

    tokens_before: int | None = None
    tokens_after: int | None = None


# ---------------------------------------------------------------------------
# TranscriptEntry
# ---------------------------------------------------------------------------


class TranscriptEntry(BaseModel):
    """Single JSONL entry — cc-haha compatible envelope."""

    uuid: str
    parentUuid: str | None = None
    sessionId: str
    timestamp: str
    isSidechain: bool = False
    version: int = 1

    # -- entry kind discriminant (problem #3: segment chain / checkpoint) --
    type: Literal["message", "compaction"] = "message"

    message: dict[str, Any] | None = None
    """Present when ``type="message"`` — ``{role, content}``."""

    compaction: CompactionRecord | None = None
    """Present when ``type="compaction"`` — checkpoint payload."""

    # ---- factories ----

    @classmethod
    def from_message(
        cls,
        message: dict[str, Any],
        session_id: str,
        parent_uuid: str | None = None,
        is_sidechain: bool = False,
    ) -> "TranscriptEntry":
        """Create a ``message`` entry."""
        return cls(
            uuid=_uuid.uuid4().hex,
            parentUuid=parent_uuid,
            sessionId=session_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            isSidechain=is_sidechain,
            type="message",
            message=message,
        )

    @classmethod
    def from_checkpoint(
        cls,
        record: CompactionRecord,
        session_id: str,
        parent_uuid: str | None = None,
    ) -> "TranscriptEntry":
        """Create a ``compaction`` checkpoint entry."""
        return cls(
            uuid=_uuid.uuid4().hex,
            parentUuid=parent_uuid,
            sessionId=session_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            type="compaction",
            compaction=record,
        )


# ---------------------------------------------------------------------------
# SessionTranscriptWriter
# ---------------------------------------------------------------------------


class SessionTranscriptWriter:
    """Append-only JSONL writer — one per ``SessionState``.

    Write semantics:

    * ``write_entry()`` — append a ``type="message"`` entry.
    * ``write_checkpoint()`` — append a ``type="compaction"`` entry
      (never rewrites / truncates earlier content — problem #3).
    * Every ``write_*`` call is followed by an immediate ``flush()``,
      giving crash-consistency where at most the very last line can be
      lost.
    """

    def __init__(self, path: str, session_id: str) -> None:
        self._path = path
        self._session_id = session_id
        self._fh: Any = None
        self._entry_count: int = 0
        self._last_uuid: str | None = None
        self._last_checkpoint_uuid: str | None = None
        self._is_open: bool = False

    # -- open / close --------------------------------------------------------

    async def open(self) -> None:
        """Open (or re-open) the underlying file for appending.

        **Crash guard (problem #4):** if the file exists but its last
        byte is not a newline, a previous write was interrupted mid-line,
        leaving a truncated, unparseable fragment.  We append a solitary
        ``"\\n"`` to isolate the fragment on its own line (it will be
        skipped as a bad line by ``load_entries``).  This prevents the
        next ``write_entry`` from being concatenated onto the fragment
        and corrupting *two* lines.
        """
        await aiofiles.os.makedirs(os.path.dirname(self._path), exist_ok=True)

        # Crash guard — isolate dangling half-line (problem #4)
        if os.path.isfile(self._path) and os.path.getsize(self._path) > 0:
            async with aiofiles.open(self._path, "rb") as probe:
                await probe.seek(-1, os.SEEK_END)
                last_byte = await probe.read(1)
            if last_byte != b"\n":
                async with aiofiles.open(self._path, "ab") as fixup:
                    await fixup.write(b"\n")

        # Chain recovery (resume) — seed _last_uuid / _last_checkpoint_uuid
        # from the existing entries so the parentUuid chain stays continuous
        # across a reopen (invariant #5).  Without this, the first entry
        # appended after a resume would have ``parentUuid=None`` and start a
        # second root, breaking ``walk_entry_chain`` / segment-chain audit.
        if os.path.isfile(self._path) and os.path.getsize(self._path) > 0:
            existing = await load_entries(self._path)
            if existing:
                self._last_uuid = existing[-1].uuid
                self._entry_count = len(existing)
                for e in existing:
                    if e.type == "compaction":
                        self._last_checkpoint_uuid = e.uuid

        self._fh = await aiofiles.open(self._path, "a", encoding="utf-8")
        self._is_open = True

    async def close(self) -> None:
        """Flush + close.  Idempotent — safe to call multiple times."""
        if not self._is_open or self._fh is None:
            return
        try:
            await self._fh.flush()
        except Exception:
            pass
        try:
            await self._fh.close()
        except Exception:
            pass
        self._fh = None
        self._is_open = False

    # -- write ---------------------------------------------------------------

    async def write_entry(
        self, message: dict[str, Any], *, is_sidechain: bool = False
    ) -> str:
        """Append a ``type="message"`` entry.

        Returns the new entry's UUID.
        """
        parent = self._last_uuid
        entry = TranscriptEntry.from_message(
            message=message,
            session_id=self._session_id,
            parent_uuid=parent,
            is_sidechain=is_sidechain,
        )
        await self._append_line(entry.model_dump_json())
        self._last_uuid = entry.uuid
        return entry.uuid

    async def write_checkpoint(self, record: CompactionRecord) -> str:
        """Append a ``type="compaction"`` checkpoint entry.

        **Never rewrites / truncates earlier content.**  The pre-compaction
        entries remain unchanged on disk.  This entry sits in the same
        linear ``parentUuid`` chain so that FULL-audit walk can traverse
        segments without missing boundaries (problem #3).

        Returns the checkpoint entry's UUID.
        """
        parent = self._last_uuid
        record.prev_checkpoint_uuid = self._last_checkpoint_uuid
        entry = TranscriptEntry.from_checkpoint(
            record=record,
            session_id=self._session_id,
            parent_uuid=parent,
        )
        await self._append_line(entry.model_dump_json())
        self._last_uuid = entry.uuid
        self._last_checkpoint_uuid = entry.uuid
        return entry.uuid

    async def flush(self) -> None:
        if self._is_open and self._fh is not None:
            await self._fh.flush()

    # -- properties ----------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def entry_count(self) -> int:
        return self._entry_count

    @property
    def last_uuid(self) -> str | None:
        return self._last_uuid

    @property
    def last_checkpoint_uuid(self) -> str | None:
        return self._last_checkpoint_uuid

    # -- internal ------------------------------------------------------------

    async def _append_line(self, json_str: str) -> None:
        if self._fh is None:
            raise RuntimeError("SessionTranscriptWriter is not open")
        await self._fh.write(json_str + "\n")
        await self._fh.flush()
        self._entry_count += 1


# ---------------------------------------------------------------------------
# File-level helpers
# ---------------------------------------------------------------------------


async def load_entries(path: str) -> list[TranscriptEntry]:
    """Read JSONL line by line, parse each as ``TranscriptEntry``.

    Invalid lines (JSON decode errors, validation failures) are skipped
    with a debug log.  Missing file → empty list.
    """
    if not os.path.isfile(path):
        return []

    entries: list[TranscriptEntry] = []
    async with aiofiles.open(path, "r", encoding="utf-8") as fh:
        async for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw = json.loads(stripped)
                entries.append(TranscriptEntry(**raw))
            except Exception as exc:
                logger.debug(
                    "Skipping unparseable line in %s: %s", path, exc
                )
    return entries


async def save_entries(path: str, entries: list[TranscriptEntry]) -> None:
    """Atomically overwrite *path* with *entries* (used for migration).

    Writes to a temp file in the same directory, then ``os.replace`` so
    the operation is atomic on the target path.
    """
    await aiofiles.os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + f".tmp.{_uuid.uuid4().hex[:8]}"
    try:
        async with aiofiles.open(tmp, "w", encoding="utf-8") as fh:
            for e in entries:
                await fh.write(e.model_dump_json() + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
