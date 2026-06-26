"""
Log output sinks.

:class:`JsonlLogSink` is append-only and writes on a **background drain task**
so that ``emit`` never blocks the conversation hot path (the turn loop).  Any
write failure is swallowed and downgraded to a warning + ``dropped`` count —
a logging fault must never propagate into the user's conversation (invariant
#5).
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from typing import Any

import aiofiles
import aiofiles.os

from .events import LogRecord

logger = logging.getLogger("agent_core.logging.sink")

__all__ = ["LogSink", "NullSink", "JsonlLogSink"]


class LogSink(ABC):
    """Pluggable output endpoint for :class:`LogRecord`."""

    @abstractmethod
    async def emit(self, record: LogRecord) -> None: ...

    @abstractmethod
    async def aclose(self) -> None: ...

    @property
    @abstractmethod
    def dropped(self) -> int: ...


class NullSink(LogSink):
    """No-op sink (used when logging is disabled, so callers need not branch)."""

    async def emit(self, record: LogRecord) -> None:  # noqa: D102
        return None

    async def aclose(self) -> None:  # noqa: D102
        return None

    @property
    def dropped(self) -> int:
        return 0


class JsonlLogSink(LogSink):
    """Append-only JSONL sink backed by an :class:`asyncio.Queue` + drain task.

    Mechanics:
      * ``emit`` only does ``put_nowait`` — never awaits disk I/O.  When the
        queue is full the oldest record is dropped and ``dropped`` incremented
        (back-pressure must never stall the conversation).
      * a single background ``_drain`` task serializes records to JSONL,
        appends via ``aiofiles`` and flushes every ``flush_every`` records.
      * ``seq`` is assigned here (monotonic per sink/session).
      * ``open`` applies the same trailing-newline crash guard as the
        persistence writer so a crashed half-line corrupts at most one line.
    """

    def __init__(
        self,
        path: str,
        *,
        flush_every: int = 1,
        queue_maxsize: int = 10_000,
    ) -> None:
        self._path = path
        self._flush_every = max(1, flush_every)
        self._queue: asyncio.Queue[LogRecord | None] = asyncio.Queue(
            maxsize=max(1, queue_maxsize)
        )
        self._fh: Any = None
        self._task: asyncio.Task[None] | None = None
        self._seq = 0
        self._dropped = 0
        self._since_flush = 0
        self._is_open = False

    # -- lifecycle ---------------------------------------------------------

    async def open(self) -> None:
        await aiofiles.os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)

        # Crash guard — isolate a dangling half-line from a previous crash.
        if os.path.isfile(self._path) and os.path.getsize(self._path) > 0:
            async with aiofiles.open(self._path, "rb") as probe:
                await probe.seek(-1, os.SEEK_END)
                last_byte = await probe.read(1)
            if last_byte != b"\n":
                async with aiofiles.open(self._path, "ab") as fixup:
                    await fixup.write(b"\n")

        self._fh = await aiofiles.open(self._path, "a", encoding="utf-8")
        self._is_open = True
        self._task = asyncio.create_task(self._drain())

    async def emit(self, record: LogRecord) -> None:
        """Assign a seq and enqueue.  Non-blocking; drops on a full queue."""
        if not self._is_open:
            return
        record.seq = self._seq
        self._seq += 1
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self._dropped += 1

    async def aclose(self) -> None:
        """Drain the queue, flush, and close.  Idempotent."""
        if not self._is_open:
            return
        self._is_open = False
        # Signal the drain task to finish, then wait for it.
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            # Make room for the sentinel.
            try:
                self._queue.get_nowait()
                self._dropped += 1
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        if self._task is not None:
            try:
                await self._task
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("log sink drain task error on close: %s", exc)
            self._task = None
        await self._close_fh()

    # -- background drain --------------------------------------------------

    async def _drain(self) -> None:
        while True:
            record = await self._queue.get()
            if record is None:  # sentinel → finish remaining then stop
                break
            await self._write_one(record)
        # Flush whatever is still buffered after the sentinel.
        await self._flush()

    async def _write_one(self, record: LogRecord) -> None:
        if self._fh is None:
            return
        try:
            line = record.model_dump_json()
            await self._fh.write(line + "\n")
            self._since_flush += 1
            if self._since_flush >= self._flush_every:
                await self._flush()
        except Exception as exc:
            # A logging fault must never reach the conversation.
            self._dropped += 1
            logger.warning("log sink write error (dropped 1 record): %s", exc)

    async def _flush(self) -> None:
        if self._fh is None:
            return
        try:
            await self._fh.flush()
            self._since_flush = 0
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("log sink flush error: %s", exc)

    async def _close_fh(self) -> None:
        if self._fh is None:
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

    # -- properties --------------------------------------------------------

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def next_seq(self) -> int:
        return self._seq

    @property
    def is_open(self) -> bool:
        return self._is_open
