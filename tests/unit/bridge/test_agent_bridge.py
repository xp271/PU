"""Tests for bridge/agent_bridge.py — AgentBridge session management + Agent lifecycle."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.bridge.agent_bridge import AgentBridge
from agent_core.models import AgentConfig


@pytest.fixture
def bridge() -> AgentBridge:
    return AgentBridge(AgentConfig(), session_timeout=0.1)


def _make_mock_agent(texts: list[str], error_msg: str | None = None):
    async def _mock_stream(prompt, *, system=None):
        for t in texts:
            yield MagicMock(type="assistant_text", content=t)
        if error_msg:
            yield MagicMock(type="error", message=error_msg)
        yield MagicMock(
            type="done", stop_reason="end_turn", total_tokens=100,
            final_messages=[], error_detail=None,
        )
    mock_agent = MagicMock()
    mock_agent.stream = _mock_stream
    mock_agent.aclose = AsyncMock()
    return mock_agent


@pytest.fixture
def mock_agent_cls():
    with patch("agent_core.bridge.agent_bridge.Agent") as mock:
        yield mock


class TestSessionManagement:
    def test_session_id_format(self, bridge: AgentBridge):
        sid = bridge.session_id("user1", "channel1")
        assert sid == "channel1:user1"

    def test_session_creation(self, bridge: AgentBridge):
        s = bridge._get_or_create_session("ch1:u1")
        assert s is not None
        assert bridge._get_or_create_session("ch1:u1") is s

    def test_session_reuse(self, bridge: AgentBridge):
        s1 = bridge._get_or_create_session("ch1:u1")
        s2 = bridge._get_or_create_session("ch1:u1")
        assert s1 is s2

    def test_session_timeout_lazy_cleanup(self, bridge: AgentBridge):
        import time
        sid = "ch1:u1"
        s1 = bridge._get_or_create_session(sid)
        time.sleep(0.15)
        s2 = bridge._get_or_create_session(sid)
        assert s1 is not s2

    def test_clear_session(self, bridge: AgentBridge):
        bridge._get_or_create_session("ch1:u1")
        bridge.clear_session("u1", "ch1")
        s = bridge._get_or_create_session("ch1:u1")
        assert len(s.messages) == 0

    def test_different_users_independent(self, bridge: AgentBridge):
        s1 = bridge._get_or_create_session("ch1:u1")
        s2 = bridge._get_or_create_session("ch1:u2")
        assert s1 is not s2
        assert bridge._session_locks["ch1:u1"] is not bridge._session_locks["ch1:u2"]


class TestBuildPrompt:
    def test_text_passthrough(self, bridge: AgentBridge):
        assert bridge._build_prompt("hello", "text") == "hello"

    def test_voice_with_recognition(self, bridge: AgentBridge):
        assert bridge._build_prompt("recognized", "voice") == "[语音消息] recognized"

    def test_voice_without_recognition(self, bridge: AgentBridge):
        assert bridge._build_prompt("", "voice") == "[语音消息]"

    def test_image(self, bridge: AgentBridge):
        assert bridge._build_prompt("url", "image") == "[图片消息] url"

    def test_unknown_type(self, bridge: AgentBridge):
        assert bridge._build_prompt("c", "file") == "[file消息] c"


class TestProcess:
    @pytest.mark.asyncio
    async def test_process_creates_and_closes_agent(self, bridge: AgentBridge, mock_agent_cls):
        mock_agent_cls.return_value = _make_mock_agent(["hello"])
        result = await bridge.process("u1", "ch1", "hi", "text")
        assert result == ["hello"]
        mock_agent_cls.return_value.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_collects_text(self, bridge: AgentBridge, mock_agent_cls):
        mock_agent_cls.return_value = _make_mock_agent(["hello, ", "world"])
        result = await bridge.process("u1", "ch1", "test", "text")
        assert result == ["hello, world"]

    @pytest.mark.asyncio
    async def test_process_empty_reply(self, bridge: AgentBridge, mock_agent_cls):
        mock_agent_cls.return_value = _make_mock_agent([])
        result = await bridge.process("u1", "ch1", "test", "text")
        assert result == []

    @pytest.mark.asyncio
    async def test_process_error_with_partial_text(self, bridge: AgentBridge, mock_agent_cls):
        mock_agent_cls.return_value = _make_mock_agent(["partial"], error_msg="err")
        result = await bridge.process("u1", "ch1", "test", "text")
        assert result == ["partial"]

    @pytest.mark.asyncio
    async def test_process_error_no_text(self, bridge: AgentBridge, mock_agent_cls):
        mock_agent_cls.return_value = _make_mock_agent([], error_msg="err")
        result = await bridge.process("u1", "ch1", "test", "text")
        assert result == ["抱歉，处理您的请求时遇到了问题，请稍后再试。"]

    @pytest.mark.asyncio
    async def test_agent_aclose_on_exception(self, bridge: AgentBridge, mock_agent_cls):
        mock_agent = MagicMock()
        mock_agent.aclose = AsyncMock()
        async def _crash(prompt, **kw):
            raise RuntimeError("crash")
            yield
        mock_agent.stream = _crash
        mock_agent_cls.return_value = mock_agent
        with pytest.raises(RuntimeError):
            await bridge.process("u1", "ch1", "test", "text")
        mock_agent.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_per_session_lock(self, bridge: AgentBridge, mock_agent_cls):
        call_order: list[int] = []

        def _make_delayed(idx: int):
            async def _stream(prompt, **kw):
                call_order.append(idx)
                yield MagicMock(type="assistant_text", content=f"r{idx}")
                yield MagicMock(type="done", stop_reason="end_turn", total_tokens=10,
                               final_messages=[], error_detail=None)
            return _stream

        mock_agent_cls.side_effect = [
            MagicMock(stream=_make_delayed(0), aclose=AsyncMock()),
            MagicMock(stream=_make_delayed(1), aclose=AsyncMock()),
        ]
        await asyncio.gather(
            bridge.process("u1", "ch1", "m1", "text"),
            bridge.process("u1", "ch1", "m2", "text"),
        )
        assert call_order == [0, 1]

    @pytest.mark.asyncio
    async def test_long_text_split(self, bridge: AgentBridge, mock_agent_cls):
        mock_agent_cls.return_value = _make_mock_agent(["x" * 3500])
        result = await bridge.process("u1", "ch1", "test", "text")
        assert len(result) == 2
        assert all(len(c) <= 1900 for c in result)
