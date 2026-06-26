"""Unit tests for log reading / reconstruction / export (Item 12)."""

from __future__ import annotations

import json

import pytest

from agent_core.logging.events import (
    AssistantTextRecord,
    ErrorRecord,
    SessionEndRecord,
    ToolResultRecord,
    ToolUseRecord,
    TurnMetricRecord,
    UserPromptRecord,
)
from agent_core.logging.reader import (
    aggregate_metrics,
    cleanup_old_logs,
    export_dataset,
    iter_conversations,
    load_log,
)


def _write_log(path, records) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for i, r in enumerate(records):
            r.seq = i
            fh.write(r.model_dump_json() + "\n")


def _sample_records():
    return [
        UserPromptRecord(trace_id="t1", project_hash="p", session_id="s1", text="hello"),
        AssistantTextRecord(trace_id="t1", project_hash="p", session_id="s1", turn_index=1, text="hi "),
        AssistantTextRecord(trace_id="t1", project_hash="p", session_id="s1", turn_index=1, text="there"),
        ToolUseRecord(trace_id="t1", project_hash="p", turn_index=1, tool_use_id="u1", tool_name="Bash", tool_input={"command": "ls"}),
        TurnMetricRecord(trace_id="t1", project_hash="p", turn_index=1, provider="anthropic", model="claude", input_tokens=100, output_tokens=50, latency_ms=10.0, tool_uses=1, tool_names=["Bash"]),
        ToolResultRecord(trace_id="t1", project_hash="p", turn_index=1, tool_use_id="u1", tool_name="Bash", content="file1"),
        SessionEndRecord(trace_id="t1", project_hash="p", session_id="s1", stop_reason="end_turn", total_tokens=150, tool_uses_total=1),
    ]


@pytest.mark.agent
class TestReader:
    @pytest.mark.asyncio
    async def test_load_log_missing_file(self, tmp_path) -> None:
        assert await load_log(str(tmp_path / "nope.jsonl")) == []

    @pytest.mark.asyncio
    async def test_load_log_skips_bad_lines(self, tmp_path) -> None:
        path = str(tmp_path / "s.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(UserPromptRecord(trace_id="t", project_hash="p", text="ok").model_dump_json() + "\n")
            fh.write("{not valid json\n")
            fh.write("\n")
        recs = await load_log(path)
        assert len(recs) == 1

    @pytest.mark.asyncio
    async def test_iter_conversations_rebuild(self, tmp_path) -> None:
        path = str(tmp_path / "s.jsonl")
        _write_log(path, _sample_records())
        recs = await load_log(path)
        convos = iter_conversations(recs)
        assert len(convos) == 1
        c = convos[0]
        assert c.trace_id == "t1"
        assert c.session_id == "s1"
        assert c.user_prompts == ["hello"]
        assert len(c.turns) == 1
        t = c.turns[0]
        assert t.assistant_text == "hi there"  # concatenated
        assert t.tool_calls == [("Bash", {"command": "ls"}, "file1")]
        assert t.metric is not None and t.metric.input_tokens == 100
        assert c.total_tokens == 150
        assert c.stop_reason == "end_turn"

    @pytest.mark.asyncio
    async def test_export_dataset_granularities(self, tmp_path) -> None:
        path = str(tmp_path / "s.jsonl")
        _write_log(path, _sample_records())
        recs = await load_log(path)

        turn_rows = export_dataset(recs, granularity="turn")
        assert len(turn_rows) == 1
        assert turn_rows[0]["provider"] == "anthropic"
        assert turn_rows[0]["tool_calls"] == ["Bash"]

        sess_rows = export_dataset(recs, granularity="session")
        assert len(sess_rows) == 1
        assert sess_rows[0]["total_tokens"] == 150
        assert sess_rows[0]["input_tokens"] == 100

        msg_rows = export_dataset(recs, granularity="message")
        assert len(msg_rows) == len(recs)

    def test_aggregate_metrics(self) -> None:
        recs = _sample_records()
        agg = aggregate_metrics(recs)
        assert agg["turns"] == 1
        assert agg["input_tokens"] == 100
        assert agg["output_tokens"] == 50
        assert agg["total_tokens"] == 150
        assert agg["tool_uses"] == 1
        assert agg["tool_frequency"] == {"Bash": 1}
        assert agg["tokens_by_provider"] == {"anthropic": 150}
        assert agg["errors"] == 0
        assert agg["latency_ms_avg"] == 10.0

    def test_aggregate_error_rate(self) -> None:
        recs = [
            TurnMetricRecord(trace_id="t", project_hash="p", turn_index=1, status="error", error_type="x"),
            ErrorRecord(trace_id="t", project_hash="p", message="boom"),
        ]
        agg = aggregate_metrics(recs)
        assert agg["errors"] == 1
        assert agg["error_rate"] == 1.0

    @pytest.mark.asyncio
    async def test_cleanup_old_logs(self, tmp_path) -> None:
        import os

        base = tmp_path / "ph"
        base.mkdir()
        old = base / "old.jsonl"
        new = base / "new.jsonl"
        old.write_text("{}\n")
        new.write_text("{}\n")
        os.utime(str(old), (1000, 1000))  # ancient
        removed = await cleanup_old_logs("ph", max_age_days=1, log_dir=str(tmp_path))
        assert any("old.jsonl" in p for p in removed)
        assert not old.exists()
        assert new.exists()
