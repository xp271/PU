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


class TestGetSession:
    """AgentBridge.get_session() 公开接口测试。"""

    @pytest.mark.asyncio
    async def test_get_session_creates(self, bridge: AgentBridge):
        """get_session() 首次调用创建新 session。"""
        s = bridge.get_session("u1", "ch1")
        assert s is not None
        assert s.messages == []

    @pytest.mark.asyncio
    async def test_get_session_reuses(self, bridge: AgentBridge):
        """get_session() 重复调用返回同一个 session。"""
        s1 = bridge.get_session("u1", "ch1")
        s2 = bridge.get_session("u1", "ch1")
        assert s1 is s2

    @pytest.mark.asyncio
    async def test_get_session_independent_users(self, bridge: AgentBridge):
        """不同用户有独立的 session。"""
        s1 = bridge.get_session("u1", "ch1")
        s2 = bridge.get_session("u2", "ch1")
        assert s1 is not s2


class TestWorkspaceOverride:
    """AgentBridge.process() 中 workspace_override 的测试。"""

    @pytest.mark.asyncio
    async def test_process_uses_workspace_override(self, bridge: AgentBridge, mock_agent_cls):
        """session.workspace_override 非 None 时，Agent 收到覆盖后的 config。"""
        mock_agent_cls.return_value = _make_mock_agent(["ok"])
        session = bridge.get_session("u1", "ch1")
        session.workspace_override = "D:\\test_ws"

        await bridge.process("u1", "ch1", "test", "text")

        # 验证传入 Agent 的 config.workspace_root 是覆盖后的值
        call_args = mock_agent_cls.call_args
        passed_config = call_args[0][0]  # first positional arg = AgentConfig
        assert passed_config.workspace_root == "D:\\test_ws"

    @pytest.mark.asyncio
    async def test_process_no_override_no_change(self, bridge: AgentBridge, mock_agent_cls):
        """session.workspace_override 为 None 时，Agent 使用原始 config。"""
        mock_agent_cls.return_value = _make_mock_agent(["ok"])

        await bridge.process("u1", "ch1", "test", "text")

        call_args = mock_agent_cls.call_args
        passed_config = call_args[0][0]
        # Should be the same as self._base (original, not overwritten)
        assert passed_config is bridge._base

    @pytest.mark.asyncio
    async def test_process_override_reset(self, bridge: AgentBridge, mock_agent_cls):
        """workspace_override 设为 None 后恢复使用原始 config。"""
        mock_agent_cls.return_value = _make_mock_agent(["ok"])
        session = bridge.get_session("u1", "ch1")
        session.workspace_override = "D:\\test_ws"

        await bridge.process("u1", "ch1", "test", "text")
        assert mock_agent_cls.call_args[0][0].workspace_root == "D:\\test_ws"

        # Reset
        session.workspace_override = None
        mock_agent_cls.reset_mock()
        mock_agent_cls.return_value = _make_mock_agent(["ok2"])

        await bridge.process("u1", "ch1", "test2", "text")
        assert mock_agent_cls.call_args[0][0] is bridge._base
