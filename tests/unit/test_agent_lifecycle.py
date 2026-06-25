"""Unit tests for agents/lifecycle.py."""

from xml.sax.saxutils import escape as xml_escape

from agent_core.agents.lifecycle import (
    AgentLifecycleState,
    _extract_3_5_words,
    enqueue_agent_notification,
    finalize_agent_tool,
)


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
