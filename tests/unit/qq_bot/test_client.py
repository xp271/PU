"""Tests for qq_bot/client.py — QQBotClient event callbacks."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.channels.qq_bot.client import QQBotClient


def _make_mock_message(content: str, user_openid: str = "user_1",
                       group_openid: str = "group_1",
                       member_openid: str = "member_1") -> MagicMock:
    """Build a mock botpy Message/C2CMessage/GroupMessage."""
    msg = MagicMock()
    msg.content = content
    msg.author = MagicMock()
    msg.author.user_openid = user_openid
    msg.author.member_openid = member_openid
    msg.group_openid = group_openid
    return msg


class TestQQBotClient:
    """QQBotClient 事件回调测试。"""

    @pytest.fixture(autouse=True)
    def _mock_botpy_init(self):
        """Patch botpy.Client.__init__ to avoid real side effects."""
        with patch("botpy.Client.__init__", return_value=None):
            yield

    @pytest.fixture
    def channel(self):
        from agent_core.channels.qq_bot.channel import QQBotChannel
        ch = MagicMock(spec=QQBotChannel)
        ch.handle_message = AsyncMock()
        return ch

    def test_c2c_callback_forward(self, channel):
        """on_c2c_message_create 应转发到 channel.handle_message。"""
        client = QQBotClient(channel)
        msg = _make_mock_message("你好呀")

        import asyncio
        asyncio.run(client.on_c2c_message_create(msg))

        channel.handle_message.assert_called_once_with(
            user_id="user_1",
            channel_id="c2c",
            content="你好呀",
            msg_type="text",
        )

    def test_group_at_callback_forward(self, channel):
        """on_group_at_message_create 应转发到 channel.handle_message。"""
        client = QQBotClient(channel)
        msg = _make_mock_message("@bot help", member_openid="member_2", group_openid="g123")

        import asyncio
        asyncio.run(client.on_group_at_message_create(msg))

        channel.handle_message.assert_called_once_with(
            user_id="member_2",
            channel_id="g123",
            content="@bot help",
            msg_type="text",
        )

    def test_c2c_callback_empty_content(self, channel):
        """空消息也应转发（不做过滤）。"""
        client = QQBotClient(channel)
        msg = _make_mock_message("")

        import asyncio
        asyncio.run(client.on_c2c_message_create(msg))

        channel.handle_message.assert_called_once_with(
            user_id="user_1",
            channel_id="c2c",
            content="",
            msg_type="text",
        )
