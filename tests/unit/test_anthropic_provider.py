"""Unit tests for the Anthropic LLM provider."""

from __future__ import annotations

import anthropic
import httpx
import pytest

from agent_core.llm.anthropic import AnthropicClient, _ANTHROPIC_STOP_MAP
from agent_core.models import ModelProvider


def _make_status_error(status_code: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code=status_code, request=request)
    return anthropic.APIStatusError(
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
        assert AnthropicClient.classify_error(_make_status_error(429)) == "rate_limit"

    def test_529_is_overloaded(self) -> None:
        assert AnthropicClient.classify_error(_make_status_error(529)) == "overloaded"

    def test_413_is_context_length(self) -> None:
        assert (
            AnthropicClient.classify_error(_make_status_error(413))
            == "context_length"
        )

    def test_400_with_context_is_context_length(self) -> None:
        err = anthropic.APIStatusError(
            message="context window exceeded",
            body={"error": {"message": "context window exceeded"}},
            response=httpx.Response(
                status_code=400,
                request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
            ),
        )
        assert AnthropicClient.classify_error(err) == "context_length"

    def test_500_is_transient(self) -> None:
        assert AnthropicClient.classify_error(_make_status_error(500)) == "transient"

    def test_502_is_transient(self) -> None:
        assert AnthropicClient.classify_error(_make_status_error(502)) == "transient"

    def test_503_is_transient(self) -> None:
        assert AnthropicClient.classify_error(_make_status_error(503)) == "transient"

    def test_401_is_fatal(self) -> None:
        assert AnthropicClient.classify_error(_make_status_error(401)) == "fatal"

    def test_403_is_fatal(self) -> None:
        assert AnthropicClient.classify_error(_make_status_error(403)) == "fatal"

    def test_other_4xx_is_fatal(self) -> None:
        assert AnthropicClient.classify_error(_make_status_error(404)) == "fatal"
        assert AnthropicClient.classify_error(_make_status_error(422)) == "fatal"

    def test_non_api_error_is_transient(self) -> None:
        assert (
            AnthropicClient.classify_error(ValueError("something")) == "transient"
        )
        assert AnthropicClient.classify_error(ConnectionError("refused")) == "transient"


# ---------------------------------------------------------------------------
# normalize_stop_reason
# ---------------------------------------------------------------------------


@pytest.mark.model
class TestNormalizeStopReason:
    def test_end_turn_maps_to_end_turn(self) -> None:
        mapped, detail = AnthropicClient.normalize_stop_reason("end_turn")
        assert mapped == "end_turn"
        assert detail is None

    def test_max_tokens_maps_to_max_turns(self) -> None:
        mapped, detail = AnthropicClient.normalize_stop_reason("max_tokens")
        assert mapped == "max_turns"
        assert detail is None

    def test_stop_sequence_maps_to_end_turn(self) -> None:
        mapped, detail = AnthropicClient.normalize_stop_reason("stop_sequence")
        assert mapped == "end_turn"
        assert detail is None

    def test_tool_use_maps_to_end_turn(self) -> None:
        mapped, detail = AnthropicClient.normalize_stop_reason("tool_use")
        assert mapped == "end_turn"
        assert detail is None

    def test_unknown_stop_reason_falls_back_to_end_turn(self) -> None:
        mapped, detail = AnthropicClient.normalize_stop_reason("unknown_reason")
        assert mapped == "end_turn"
        assert detail is None

    def test_all_keys_in_stop_map(self) -> None:
        """Verify the stop map covers all known Anthropic stop reasons."""
        expected = {"end_turn", "max_tokens", "stop_sequence", "tool_use"}
        assert set(_ANTHROPIC_STOP_MAP.keys()) == expected


# ---------------------------------------------------------------------------
# convert_messages
# ---------------------------------------------------------------------------


@pytest.mark.message
class TestConvertMessages:
    def test_passes_system_through(self) -> None:
        system, messages = AnthropicClient.convert_messages(
            [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            "You are helpful.",
        )
        assert system == "You are helpful."
        assert len(messages) == 1

    def test_no_system(self) -> None:
        system, messages = AnthropicClient.convert_messages(
            [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            None,
        )
        assert system is None

    def test_identity_transform_preserves_messages(self) -> None:
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        ]
        system, converted = AnthropicClient.convert_messages(msgs, None)
        assert system is None
        assert converted == msgs

    def test_strips_internal_keys(self) -> None:
        """Internal _keys in message dicts are removed."""
        msgs = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "hi"}],
                "_internal_meta": "should-be-removed",
            }
        ]
        system, converted = AnthropicClient.convert_messages(msgs, "sys")
        assert system == "sys"
        assert "_internal_meta" not in converted[0]
        assert converted[0]["role"] == "user"
        assert converted[0]["content"] == [{"type": "text", "text": "hi"}]

    def test_empty_messages(self) -> None:
        system, converted = AnthropicClient.convert_messages([], "sys prompt")
        assert system == "sys prompt"
        assert converted == []

    def test_tool_use_message_preserved(self) -> None:
        msgs = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "Read",
                        "input": {"file_path": "/f.txt"},
                    }
                ],
            }
        ]
        system, converted = AnthropicClient.convert_messages(msgs, None)
        assert converted[0]["content"][0]["type"] == "tool_use"
        assert converted[0]["content"][0]["id"] == "call_1"

    def test_tool_result_message_preserved(self) -> None:
        msgs = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": "file contents",
                        "is_error": False,
                    }
                ],
            }
        ]
        system, converted = AnthropicClient.convert_messages(msgs, None)
        assert converted[0]["content"][0]["type"] == "tool_result"
        assert converted[0]["content"][0]["tool_use_id"] == "tu_1"


# ---------------------------------------------------------------------------
# convert_tools
# ---------------------------------------------------------------------------


@pytest.mark.model
class TestConvertTools:
    def test_all_tools_with_star(self) -> None:
        tools = AnthropicClient.convert_tools(["*"])
        names = {t["name"] for t in tools}
        assert "Read" in names
        assert "Write" in names
        assert "Edit" in names
        assert "Glob" in names
        assert "Grep" in names
        assert "Bash" in names

    def test_filtered_tools(self) -> None:
        tools = AnthropicClient.convert_tools(["Read", "Glob"])
        names = {t["name"] for t in tools}
        assert names == {"Read", "Glob"}

    def test_empty_tools_list_returns_empty(self) -> None:
        tools = AnthropicClient.convert_tools([])
        assert tools == []

    def test_tool_schema_structure(self) -> None:
        tools = AnthropicClient.convert_tools(["Read"])
        assert len(tools) == 1
        t = tools[0]
        assert t["name"] == "Read"
        assert "description" in t
        assert "input_schema" in t
        assert t["input_schema"]["type"] == "object"


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


@pytest.mark.model
class TestInit:
    def test_provider_is_anthropic(self) -> None:
        assert AnthropicClient.provider == ModelProvider.ANTHROPIC

    def test_custom_api_key_and_base_url(self) -> None:
        client = AnthropicClient(
            model="claude-sonnet-4-5",
            api_key="sk-test",
            base_url="https://custom.example.com",
        )
        assert client.model == "claude-sonnet-4-5"
        assert client.api_key == "sk-test"
        assert client.base_url == "https://custom.example.com"

    def test_no_api_key_or_base_url(self) -> None:
        client = AnthropicClient(
            model="claude-haiku-4-5",
            api_key=None,
            base_url=None,
        )
        assert client.model == "claude-haiku-4-5"
        assert client.api_key is None
        assert client.base_url is None
