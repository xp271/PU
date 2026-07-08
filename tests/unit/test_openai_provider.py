"""Unit tests for the OpenAI LLM provider."""

from __future__ import annotations

import json

import httpx
import openai
import pytest

from agent_core.llm.openai import OpenAIClient, _OPENAI_STOP_MAP
from agent_core.models import ModelProvider


def _make_status_error(status_code: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status_code=status_code, request=request)
    return openai.APIStatusError(
        message=f"Error {status_code}",
        body={"error": {"message": f"Error {status_code}"}},
        response=response,
    )


# ---------------------------------------------------------------------------
# classify_error
# ---------------------------------------------------------------------------


@pytest.mark.model
class TestClassifyError:
    def test_429_is_rate_limit(self) -> None:
        assert OpenAIClient.classify_error(_make_status_error(429)) == "rate_limit"

    def test_503_is_overloaded(self) -> None:
        assert OpenAIClient.classify_error(_make_status_error(503)) == "overloaded"

    def test_500_is_transient(self) -> None:
        assert OpenAIClient.classify_error(_make_status_error(500)) == "transient"

    def test_502_is_transient(self) -> None:
        assert OpenAIClient.classify_error(_make_status_error(502)) == "transient"

    def test_413_is_context_length(self) -> None:
        assert (
            OpenAIClient.classify_error(_make_status_error(413)) == "context_length"
        )

    def test_400_with_context_is_context_length(self) -> None:
        err = openai.APIStatusError(
            message="context length exceeded",
            body={"error": {"message": "context length exceeded"}},
            response=httpx.Response(
                status_code=400,
                request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
            ),
        )
        assert OpenAIClient.classify_error(err) == "context_length"

    def test_400_with_content_filter_is_content_filter(self) -> None:
        err = openai.APIStatusError(
            message="content_filter triggered",
            body={"error": {"message": "content_filter triggered"}},
            response=httpx.Response(
                status_code=400,
                request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
            ),
        )
        assert OpenAIClient.classify_error(err) == "content_filter"

    def test_401_is_fatal(self) -> None:
        assert OpenAIClient.classify_error(_make_status_error(401)) == "fatal"

    def test_403_is_fatal(self) -> None:
        assert OpenAIClient.classify_error(_make_status_error(403)) == "fatal"

    def test_other_4xx_is_fatal(self) -> None:
        assert OpenAIClient.classify_error(_make_status_error(404)) == "fatal"
        assert OpenAIClient.classify_error(_make_status_error(422)) == "fatal"

    def test_non_api_error_is_transient(self) -> None:
        assert OpenAIClient.classify_error(ValueError("something")) == "transient"
        assert OpenAIClient.classify_error(ConnectionError("refused")) == "transient"


# ---------------------------------------------------------------------------
# normalize_stop_reason
# ---------------------------------------------------------------------------


@pytest.mark.model
class TestNormalizeStopReason:
    def test_stop_maps_to_end_turn(self) -> None:
        mapped, detail = OpenAIClient.normalize_stop_reason("stop")
        assert mapped == "end_turn"
        assert detail is None

    def test_length_maps_to_output_truncated(self) -> None:
        mapped, detail = OpenAIClient.normalize_stop_reason("length")
        assert mapped == "output_truncated"
        assert detail is None

    def test_tool_calls_maps_to_end_turn(self) -> None:
        mapped, detail = OpenAIClient.normalize_stop_reason("tool_calls")
        assert mapped == "end_turn"
        assert detail is None

    def test_content_filter_maps_to_permission_denied_with_detail(self) -> None:
        mapped, detail = OpenAIClient.normalize_stop_reason("content_filter")
        assert mapped == "permission_denied"
        assert detail == "openai finish_reason=content_filter"

    def test_unknown_stop_reason_falls_back_to_end_turn(self) -> None:
        mapped, detail = OpenAIClient.normalize_stop_reason("unknown_reason")
        assert mapped == "end_turn"
        assert detail is None

    def test_all_expected_keys_in_stop_map(self) -> None:
        expected = {"stop", "length", "tool_calls", "content_filter"}
        assert set(_OPENAI_STOP_MAP.keys()) == expected


# ---------------------------------------------------------------------------
# convert_messages
# ---------------------------------------------------------------------------


@pytest.mark.message
class TestConvertMessages:
    def test_system_only(self) -> None:
        result = OpenAIClient.convert_messages([], "You are helpful.")
        assert result == [{"role": "system", "content": "You are helpful."}]

    def test_no_system(self) -> None:
        result = OpenAIClient.convert_messages([], None)
        assert result == []

    def test_simple_user_message(self) -> None:
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]}
        ]
        result = OpenAIClient.convert_messages(messages, None)
        assert len(result) == 1
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "hello"

    def test_user_with_multiple_text_blocks(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "part1"},
                    {"type": "text", "text": "part2"},
                ],
            }
        ]
        result = OpenAIClient.convert_messages(messages, None)
        assert result[0]["role"] == "user"
        assert "part1" in result[0]["content"]
        assert "part2" in result[0]["content"]

    def test_user_with_text_and_tool_result(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "please process"},
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": "result text",
                    },
                ],
            }
        ]
        result = OpenAIClient.convert_messages(messages, None)
        # Should produce: 1 user message + 1 tool message
        assert len(result) == 2
        assert result[0]["role"] == "user"
        assert result[0]["content"] == "please process"
        assert result[1]["role"] == "tool"
        assert result[1]["tool_call_id"] == "tu_1"
        assert result[1]["content"] == "result text"

    def test_tool_result_with_dict_content(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": {"key": "value", "nested": [1, 2]},
                    },
                ],
            }
        ]
        result = OpenAIClient.convert_messages(messages, None)
        assert result[0]["role"] == "tool"
        assert result[0]["tool_call_id"] == "tu_1"
        # Should be JSON-serialized dict
        parsed = json.loads(result[0]["content"])
        assert parsed == {"key": "value", "nested": [1, 2]}

    def test_tool_result_with_list_content(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": ["a", "b", "c"],
                    },
                ],
            }
        ]
        result = OpenAIClient.convert_messages(messages, None)
        assert result[0]["role"] == "tool"
        parsed = json.loads(result[0]["content"])
        assert parsed == ["a", "b", "c"]

    def test_assistant_with_text(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "I'll help."}],
            }
        ]
        result = OpenAIClient.convert_messages(messages, None)
        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] == "I'll help."

    def test_assistant_with_text_and_tool_use(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me check."},
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "Read",
                        "input": {"file_path": "/f.txt"},
                    },
                ],
            }
        ]
        result = OpenAIClient.convert_messages(messages, None)
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] == "Let me check."
        assert len(result[0]["tool_calls"]) == 1
        assert result[0]["tool_calls"][0]["id"] == "call_1"
        assert result[0]["tool_calls"][0]["function"]["name"] == "Read"
        assert (
            result[0]["tool_calls"][0]["function"]["arguments"]
            == '{"file_path": "/f.txt"}'
        )

    def test_assistant_with_multiple_tool_uses(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "Read",
                        "input": {"file_path": "/a.txt"},
                    },
                    {
                        "type": "tool_use",
                        "id": "call_2",
                        "name": "Glob",
                        "input": {"pattern": "*.py"},
                    },
                ],
            }
        ]
        result = OpenAIClient.convert_messages(messages, None)
        assert len(result[0]["tool_calls"]) == 2
        names = [tc["function"]["name"] for tc in result[0]["tool_calls"]]
        assert names == ["Read", "Glob"]

    def test_assistant_with_only_tool_uses_no_text(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    },
                ],
            }
        ]
        result = OpenAIClient.convert_messages(messages, None)
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] is None  # no text, so content is None
        assert len(result[0]["tool_calls"]) == 1

    def test_unknown_role_passed_through(self) -> None:
        messages = [
            {"role": "system", "content": [{"type": "text", "text": "custom"}]},
        ]
        result = OpenAIClient.convert_messages(messages, None)
        assert result[0]["role"] == "system"
        assert result[0]["content"] == [{"type": "text", "text": "custom"}]

    def test_string_content_passed_through(self) -> None:
        messages = [{"role": "user", "content": "plain text content"}]
        result = OpenAIClient.convert_messages(messages, None)
        assert result == [{"role": "user", "content": "plain text content"}]

    def test_system_and_messages_combined(self) -> None:
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        ]
        result = OpenAIClient.convert_messages(messages, "You are the system.")
        assert len(result) == 3
        assert result[0] == {"role": "system", "content": "You are the system."}
        assert result[1]["role"] == "user"
        assert result[2]["role"] == "assistant"


# ---------------------------------------------------------------------------
# convert_tools
# ---------------------------------------------------------------------------


@pytest.mark.model
class TestConvertTools:
    def test_all_tools_with_star(self) -> None:
        tools = OpenAIClient.convert_tools(["*"])
        tool_names = {t["function"]["name"] for t in tools}
        assert "Read" in tool_names
        assert "Write" in tool_names
        assert "Edit" in tool_names
        assert "Glob" in tool_names
        assert "Grep" in tool_names
        assert "Bash" in tool_names

    def test_filtered_tools(self) -> None:
        tools = OpenAIClient.convert_tools(["Read", "Bash"])
        names = {t["function"]["name"] for t in tools}
        assert names == {"Read", "Bash"}

    def test_empty_tools_list(self) -> None:
        tools = OpenAIClient.convert_tools([])
        assert tools == []

    def test_openai_tool_schema_structure(self) -> None:
        tools = OpenAIClient.convert_tools(["Read"])
        assert len(tools) == 1
        t = tools[0]
        assert t["type"] == "function"
        assert "function" in t
        assert t["function"]["name"] == "Read"
        assert "description" in t["function"]
        assert "parameters" in t["function"]
        assert t["function"]["parameters"]["type"] == "object"

    def test_star_and_explicit_does_not_duplicate(self) -> None:
        tools = OpenAIClient.convert_tools(["*", "Read"])
        names = [t["function"]["name"] for t in tools]
        read_count = names.count("Read")
        assert read_count == 1  # no duplicates


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


@pytest.mark.model
class TestInit:
    def test_provider_is_openai(self) -> None:
        assert OpenAIClient.provider == ModelProvider.OPENAI

    def test_custom_api_key_and_base_url(self) -> None:
        client = OpenAIClient(
            model="gpt-4o",
            api_key="sk-test",
            base_url="https://custom-proxy.example.com",
        )
        assert client.model == "gpt-4o"
        assert client.api_key == "sk-test"
        assert client.base_url == "https://custom-proxy.example.com"

    def test_no_base_url_with_api_key(self) -> None:
        client = OpenAIClient(model="gpt-4.1-mini", api_key="sk-test", base_url=None)
        assert client.model == "gpt-4.1-mini"
        assert client.api_key == "sk-test"
        assert client.base_url is None
