"""Unit tests for agents/lifecycle.py."""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_core.agents.definition import AgentDefinition
from agent_core.agents.lifecycle import (
    AgentLifecycleState,
    _extract_3_5_words,
    _update_state,
    enqueue_agent_notification,
    finalize_agent_tool,
    run_async_lifecycle,
)
from agent_core.models import AgentConfig
from agent_core.tools.base import ToolUseContext


class TestAgentLifecycleState:
    def test_defaults(self):
        state = AgentLifecycleState(agent_id="abc123")
        assert state.agent_id == "abc123"
        assert state.status == "running"
        assert state.total_tokens == 0
        assert state.tool_use_count == 0
        assert state.latest_activity == "Initializing..."
        assert state.started_at > 0
        assert state.completed_at is None
        assert state.result == ""
        assert state.error is None


class TestFinalizeAgentTool:
    def test_sets_completed_at(self):
        state = AgentLifecycleState(agent_id="test")
        state.result = "Task completed successfully."
        result = finalize_agent_tool(state)
        assert result.completed_at is not None
        assert result.result == "Task completed successfully."

    def test_fallback_result_from_activity(self):
        state = AgentLifecycleState(agent_id="test")
        state.latest_activity = "Analyzing files..."
        result = finalize_agent_tool(state)
        assert result.result == "Analyzing files..."


class TestEnqueueAgentNotification:
    def test_xml_format(self):
        state = AgentLifecycleState(
            agent_id="agent-001",
            status="completed",
            total_tokens=1234,
            result="All tests passed",
        )
        queue: list[str] = []
        enqueue_agent_notification(state, queue)

        assert len(queue) == 1
        note = queue[0]
        assert "<task-notification>" in note
        assert "<agent_id>agent-001</agent_id>" in note
        assert "<status>completed</status>" in note
        assert "<total_tokens>1234</total_tokens>" in note
        assert "<result>All tests passed</result>" in note

    def test_appends_to_queue(self):
        state = AgentLifecycleState(agent_id="a", status="completed")
        queue = ["<existing>"]
        enqueue_agent_notification(state, queue)
        assert len(queue) == 2

    def test_xml_escapes_special_chars(self):
        state = AgentLifecycleState(
            agent_id="a",
            status="completed",
            result="Score < 50 & price > 100",
        )
        queue: list[str] = []
        enqueue_agent_notification(state, queue)
        note = queue[0]
        # XML special chars should be escaped
        assert "&lt;" in note
        assert "&gt;" in note
        assert "&amp;" in note


class TestExtract3To5Words:
    def test_single_sentence(self):
        result = _extract_3_5_words(["I am reading the README file now"])
        assert "I am reading" in result

    def test_empty_texts(self):
        assert _extract_3_5_words([]) == "Working..."

    def test_multiple_texts_last_non_empty(self):
        texts = ["hello", "world foo bar baz qux extra"]
        result = _extract_3_5_words(texts)
        assert result.startswith("world foo bar baz qux...")

    def test_short_text(self):
        result = _extract_3_5_words(["OK"])
        assert result == "OK"


# ============================================================================
# _update_state
# ============================================================================


class TestUpdateState:
    def test_tool_use_increments_count(self):
        state = AgentLifecycleState(agent_id="a")
        _update_state(state, SimpleNamespace(type="tool_use"))
        _update_state(state, SimpleNamespace(type="tool_use"))
        assert state.tool_use_count == 2

    def test_assistant_text_recorded_and_capped_at_10(self):
        state = AgentLifecycleState(agent_id="a")
        for i in range(15):
            _update_state(state, SimpleNamespace(type="assistant_text", content=f"t{i}"))
        assert len(state.recent_texts) == 10
        assert state.recent_texts[-1] == "t14"  # most recent kept

    def test_done_sets_total_tokens(self):
        state = AgentLifecycleState(agent_id="a")
        _update_state(state, SimpleNamespace(type="done", total_tokens=99))
        assert state.total_tokens == 99


# ============================================================================
# run_async_lifecycle — completion & failure fallbacks (plan §5.1)
# ============================================================================


def _ctx(queue):
    return ToolUseContext(
        agent_id="parent",
        parent_config=AgentConfig(),
        parent_trace_id="trace",
        notification_queue=queue,
    )


def _fake_stream(*events):
    async def _gen(*args, **kwargs):
        for ev in events:
            yield ev
    return _gen


class TestRunAsyncLifecycle:
    def test_completed_path_enqueues_notification(self):
        queue: list[str] = []
        stream = _fake_stream(
            SimpleNamespace(type="assistant_text", content="hello world done"),
            SimpleNamespace(type="done", stop_reason="end_turn", total_tokens=42),
        )
        with patch("agent_core.agents.lifecycle.run_agent", stream):
            asyncio.run(
                run_async_lifecycle("ag-1", AgentDefinition(agent_type="t"), "p", _ctx(queue))
            )
        assert len(queue) == 1
        assert "<status>completed</status>" in queue[0]
        assert "<total_tokens>42</total_tokens>" in queue[0]

    def test_done_with_error_marks_failed(self):
        queue: list[str] = []
        stream = _fake_stream(
            SimpleNamespace(type="done", stop_reason="error", total_tokens=7),
        )
        with patch("agent_core.agents.lifecycle.run_agent", stream):
            asyncio.run(
                run_async_lifecycle("ag-2", AgentDefinition(agent_type="t"), "p", _ctx(queue))
            )
        assert len(queue) == 1
        assert "<status>failed</status>" in queue[0]

    def test_child_exception_still_delivers_failed_notification(self):
        """If run_agent raises mid-stream, the parent must still be notified."""
        queue: list[str] = []

        async def _boom(*args, **kwargs):
            if False:
                yield  # make this an async generator
            raise RuntimeError("child crashed")

        with patch("agent_core.agents.lifecycle.run_agent", _boom):
            asyncio.run(
                run_async_lifecycle("ag-3", AgentDefinition(agent_type="t"), "p", _ctx(queue))
            )
        assert len(queue) == 1
        assert "<status>failed</status>" in queue[0]

    def test_no_notification_queue_does_not_crash(self):
        """A missing notification_queue is tolerated (no delivery, no error)."""
        ctx = ToolUseContext(parent_config=AgentConfig(), parent_trace_id="x")
        stream = _fake_stream(
            SimpleNamespace(type="done", stop_reason="end_turn", total_tokens=1),
        )
        with patch("agent_core.agents.lifecycle.run_agent", stream):
            asyncio.run(
                run_async_lifecycle("ag-4", AgentDefinition(agent_type="t"), "p", ctx)
            )  # must not raise
