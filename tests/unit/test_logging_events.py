"""Unit tests for logging LogRecord data models (Item 12)."""

from __future__ import annotations

import json

import pytest

from agent_core.logging.events import (
    AssistantTextRecord,
    LogRecordAdapter,
    SessionEndRecord,
    SessionStartRecord,
    ToolUseRecord,
    TurnMetricRecord,
    UserPromptRecord,
    utc_now_iso,
)


@pytest.mark.agent
class TestLogRecordModels:
    def test_utc_now_iso_has_offset(self) -> None:
        ts = utc_now_iso()
        assert "T" in ts
        assert ts.endswith("+00:00")

    def test_envelope_required_fields(self) -> None:
        with pytest.raises(Exception):
            # trace_id / project_hash are required
            UserPromptRecord(text="hi")  # type: ignore[call-arg]

    def test_defaults(self) -> None:
        r = UserPromptRecord(trace_id="t", project_hash="p", text="hi")
        assert r.kind == "user_prompt"
        assert r.seq == -1
        assert r.session_id is None
        assert r.agent_depth == 0
        assert r.is_sidechain is False

    def test_roundtrip_each_variant(self) -> None:
        records = [
            SessionStartRecord(trace_id="t", project_hash="p", model="m", permission_mode="ask"),
            UserPromptRecord(trace_id="t", project_hash="p", text="hi"),
            AssistantTextRecord(trace_id="t", project_hash="p", text="yo"),
            ToolUseRecord(trace_id="t", project_hash="p", tool_use_id="u1", tool_name="Bash"),
            TurnMetricRecord(trace_id="t", project_hash="p", provider="anthropic", input_tokens=5),
            SessionEndRecord(trace_id="t", project_hash="p", stop_reason="end_turn"),
        ]
        for rec in records:
            raw = rec.model_dump_json()
            parsed = LogRecordAdapter.validate_python(json.loads(raw))
            assert parsed.kind == rec.kind
            assert parsed.trace_id == "t"

    def test_discriminator_routes_to_correct_type(self) -> None:
        data = {"kind": "turn_metric", "trace_id": "t", "project_hash": "p", "provider": "openai"}
        parsed = LogRecordAdapter.validate_python(data)
        assert isinstance(parsed, TurnMetricRecord)
        assert parsed.provider == "openai"

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(Exception):
            LogRecordAdapter.validate_python(
                {"kind": "nope", "trace_id": "t", "project_hash": "p"}
            )
