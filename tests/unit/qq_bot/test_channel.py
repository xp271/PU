"""Tests for qq_bot/channel.py — QQBotChannel."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.channels.qq_bot.types import QQBotConfig
from agent_core.channels.qq_bot.channel import QQBotChannel


@pytest.fixture
def config() -> QQBotConfig:
    return QQBotConfig(app_id="test_app_12345", app_secret="test_secret_abc")


@pytest.fixture
def channel(config: QQBotConfig) -> QQBotChannel:
    with patch("agent_core.channels.qq_bot.channel._load_global_agent_config", return_value=None):
        return QQBotChannel(config)


def _make_mock_agent(texts: list[str]):
    async def _stream(prompt, *, system=None):
        for t in texts:
            yield MagicMock(type="assistant_text", content=t)
        yield MagicMock(
            type="done", stop_reason="end_turn", total_tokens=100,
            final_messages=[], error_detail=None,
        )
    mock = MagicMock()
    mock.stream = _stream
    mock.aclose = AsyncMock()
    return mock


class TestQQBotChannel:

    @pytest.mark.asyncio
    async def test_handle_message_c2c(self, channel: QQBotChannel):
        """C2C 单聊消息应调用 post_c2c_message。"""
        mock_agent = _make_mock_agent(["你好呀"])
        with patch("agent_core.bridge.agent_bridge.Agent", return_value=mock_agent):
            channel._client = MagicMock()
            channel._client.api = AsyncMock()

            await channel.handle_message("user1", "c2c", "你好", "text")

            channel._client.api.post_c2c_message.assert_called_once()
            call = channel._client.api.post_c2c_message.call_args
            assert call.kwargs["openid"] == "user1"
            assert call.kwargs["content"] == "你好呀"
            assert call.kwargs["msg_type"] == 0

    @pytest.mark.asyncio
    async def test_handle_message_group(self, channel: QQBotChannel):
        """群聊消息应调用 post_group_message。"""
        mock_agent = _make_mock_agent(["你好"])
        with patch("agent_core.bridge.agent_bridge.Agent", return_value=mock_agent):
            channel._client = MagicMock()
            channel._client.api = AsyncMock()

            await channel.handle_message("user1", "group123", "@bot help", "text")

            channel._client.api.post_group_message.assert_called_once()
            call = channel._client.api.post_group_message.call_args
            assert call.kwargs["group_openid"] == "group123"
            assert call.kwargs["content"] == "你好"

    @pytest.mark.asyncio
    async def test_handle_message_error(self, channel: QQBotChannel):
        """Agent 异常时应发送错误回复。"""
        mock_agent = MagicMock()
        mock_agent.aclose = AsyncMock()
        mock_agent.stream = MagicMock(side_effect=RuntimeError("boom"))

        with patch("agent_core.bridge.agent_bridge.Agent", return_value=mock_agent):
            channel._client = MagicMock()
            channel._client.api = AsyncMock()

            await channel.handle_message("user1", "c2c", "你好", "text")

            channel._client.api.post_c2c_message.assert_called_once()
            assert "处理出错" in channel._client.api.post_c2c_message.call_args.kwargs["content"]

    @pytest.mark.asyncio
    async def test_send_without_client(self, channel: QQBotChannel):
        """无 client 时 handle_message 不抛异常。"""
        # No client set — replies silently discarded
        await channel.handle_message("u1", "c2c", "test", "text")

    @pytest.mark.asyncio
    async def test_long_reply_split(self, channel: QQBotChannel):
        """长回复拆分为多条消息。"""
        mock_agent = _make_mock_agent(["x" * 2500])
        with patch("agent_core.bridge.agent_bridge.Agent", return_value=mock_agent):
            channel._client = MagicMock()
            channel._client.api = AsyncMock()

            await channel.handle_message("user1", "c2c", "test", "text")

            assert channel._client.api.post_c2c_message.call_count == 2

    @pytest.mark.asyncio
    async def test_start_missing_credentials(self, channel: QQBotChannel):
        """缺少 app_id 应抛 ValueError。"""
        channel._config.app_id = ""
        with pytest.raises(ValueError):
            await channel.start()
