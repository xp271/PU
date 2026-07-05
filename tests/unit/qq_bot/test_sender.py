"""Tests for qq_bot/sender.py — MessageSender."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.channels.qq_bot.sender import MessageSender


@pytest.fixture
def api():
    """Mock botpy.API."""
    mock = AsyncMock()
    mock.post_c2c_message = AsyncMock()
    mock.post_group_message = AsyncMock()
    return mock


@pytest.fixture
def sender(api):
    return MessageSender(api)


class TestMessageSender:
    """MessageSender 消息发送测试。"""

    @pytest.mark.asyncio
    async def test_send_c2c_text(self, sender, api):
        """send_c2c_text 应调用 post_c2c_message。"""
        await sender.send_c2c_text("user1", "你好")

        api.post_c2c_message.assert_called_once_with(
            openid="user1",
            msg_type=0,
            content="你好",
        )

    @pytest.mark.asyncio
    async def test_send_group_text(self, sender, api):
        """send_group_text 应调用 post_group_message。"""
        await sender.send_group_text("g123", "大家好")

        api.post_group_message.assert_called_once_with(
            group_openid="g123",
            msg_type=0,
            content="大家好",
        )

    @pytest.mark.asyncio
    async def test_send_c2c_text_error_handled(self, sender, api):
        """发送失败不抛异常（错误被记录但不传播）。"""
        api.post_c2c_message.side_effect = RuntimeError("network error")

        # 不应抛异常
        await sender.send_c2c_text("user1", "test")

        api.post_c2c_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_group_text_error_handled(self, sender, api):
        """群聊发送失败不抛异常。"""
        api.post_group_message.side_effect = RuntimeError("network error")

        await sender.send_group_text("g123", "test")

        api.post_group_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_c2c_markdown(self, sender, api):
        """send_c2c_markdown 发送模板消息。"""
        await sender.send_c2c_markdown(
            "user1", "tpl_001", {"title": "Hello"}
        )

        api.post_c2c_message.assert_called_once_with(
            openid="user1",
            msg_type=2,
            markdown={"custom_template_id": "tpl_001", "params": {"title": "Hello"}},
        )

    @pytest.mark.asyncio
    async def test_send_group_markdown(self, sender, api):
        """send_group_markdown 发送群聊模板消息。"""
        await sender.send_group_markdown(
            "g123", "tpl_002", {"content": "Hi"}
        )

        api.post_group_message.assert_called_once_with(
            group_openid="g123",
            msg_type=2,
            markdown={"custom_template_id": "tpl_002", "params": {"content": "Hi"}},
        )
