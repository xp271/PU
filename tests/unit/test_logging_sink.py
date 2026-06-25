"""Unit tests for log sinks (Item 12)."""

from __future__ import annotations

import os

import pytest

from agent_core.logging.events import UserPromptRecord
from agent_core.logging.sink import JsonlLogSink, NullSink


def _rec(text: str = "x") -> UserPromptRecord:
    return UserPromptRecord(trace_id="t", project_hash="p", text=text)


@pytest.mark.agent
class TestNullSink:
    @pytest.mark.asyncio
    async def test_noop(self) -> None:
        sink = NullSink()
        await sink.emit(_rec())
        await sink.aclose()
        assert sink.dropped == 0


@pytest.mark.agent
class TestJsonlLogSink:
    @pytest.mark.asyncio
    async def test_write_and_read_back(self, tmp_path) -> None:
        path = str(tmp_path / "s.jsonl")
        sink = JsonlLogSink(path, flush_every=1)
        await sink.open()
        await sink.emit(_rec("a"))
        await sink.emit(_rec("b"))
        await sink.aclose()

        lines = [l for l in open(path, encoding="utf-8").read().splitlines() if l]
        assert len(lines) == 2

    @pytest.mark.asyncio
    async def test_seq_monotonic(self, tmp_path) -> None:
        path = str(tmp_path / "s.jsonl")
        sink = JsonlLogSink(path)
        await sink.open()
        r0, r1 = _rec(), _rec()
        await sink.emit(r0)
        await sink.emit(r1)
        await sink.aclose()
        assert r0.seq == 0 and r1.seq == 1

    @pytest.mark.asyncio
    async def test_aclose_idempotent(self, tmp_path) -> None:
        path = str(tmp_path / "s.jsonl")
        sink = JsonlLogSink(path)
        await sink.open()
        await sink.aclose()
        await sink.aclose()  # no error

    @pytest.mark.asyncio
    async def test_emit_after_close_is_noop(self, tmp_path) -> None:
        path = str(tmp_path / "s.jsonl")
        sink = JsonlLogSink(path)
        await sink.open()
        await sink.aclose()
        await sink.emit(_rec())  # should not raise

    @pytest.mark.asyncio
    async def test_trailing_newline_crash_guard(self, tmp_path) -> None:
        path = str(tmp_path / "s.jsonl")
        # Simulate a crashed half-line with no trailing newline.
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"partial": ')
        sink = JsonlLogSink(path)
        await sink.open()
        await sink.emit(_rec("after"))
        await sink.aclose()

        content = open(path, encoding="utf-8").read()
        # The half-line is isolated on its own line; the new record is intact.
        lines = content.splitlines()
        assert lines[0] == '{"partial": '
        assert '"after"' in lines[-1]

    @pytest.mark.asyncio
    async def test_queue_full_drops_and_counts(self, tmp_path) -> None:
        # tiny queue; emit many without letting the drain task run.
        path = str(tmp_path / "s.jsonl")
        sink = JsonlLogSink(path, queue_maxsize=1)
        # don't open() — we test the queue accounting directly.
        sink._is_open = True  # type: ignore[attr-defined]
        for _ in range(5):
            await sink.emit(_rec())
        assert sink.dropped >= 1
        sink._is_open = False  # type: ignore[attr-defined]
