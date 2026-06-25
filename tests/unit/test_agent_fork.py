"""Unit tests for agents/fork.py."""

from agent_core.agents.fork import (
    filter_incomplete_tool_calls,
    fork_messages,
)


class TestFilterIncompleteToolCalls:
    def test_complete_conversation_unchanged(self):
        messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "read file"}],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"},
                ],
            },
        ]
        result = filter_incomplete_tool_calls(messages)
        assert len(result) == 3

    def test_dangling_tool_use_removed(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "do thing"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu_1", "name": "Write", "input": {}},
                    {"type": "tool_use", "id": "tu_2", "name": "Read", "input": {}},
                ],
            },
            # tu_1 has result, tu_2 does not
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "done"},
                ],
            },
        ]
        result = filter_incomplete_tool_calls(messages)
        # tu_2 should be gone; tu_1 should remain
        assistant_content = result[1]["content"]
        assert len(assistant_content) == 1
        assert assistant_content[0]["id"] == "tu_1"

    def test_empty_assistant_removed(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {}},
                ],
            },
        ]
        # No tool_result at all, so assistant becomes empty → removed
        result = filter_incomplete_tool_calls(messages)
        assert len(result) == 1
        assert result[0]["role"] == "user"

    def test_empty_input(self):
        result = filter_incomplete_tool_calls([])
        assert result == []


class TestForkMessages:
    def test_does_not_mutate_input(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        ]
        result = fork_messages(messages)
        assert result is not messages
        assert result == messages

    def test_filters_dangling(self):
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu_99", "name": "Read", "input": {}},
                ],
            },
        ]
        result = fork_messages(messages)
        # The dangling tool_use is removed, assistant becomes empty → dropped
        assert len(result) == 0
