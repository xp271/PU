"""Tests for qq_bot/client.py — QQBotClient event callbacks."""

from __future__ import annotations

import os
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


# ═══════════════════════════════════════════════════════════════════════
# TestOnReadyRestartNotify — on_ready 重启通知
# ═══════════════════════════════════════════════════════════════════════


class TestOnReadyRestartNotify:
    """QQBotClient.on_ready 重启通知测试。"""

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

    @pytest.fixture
    def client(self, channel):
        c = QQBotClient(channel)
        c.api = MagicMock()
        c.api.post_c2c_message = AsyncMock()
        c.api.post_group_message = AsyncMock()
        return c

    def test_on_ready_sends_c2c_notification(self, client):
        """env var 有效时 on_ready 发送 C2C 重启完成通知。"""
        import json
        import os as _os
        with patch.object(_os, "environ", {
            "QQ_BOT_RESTART_NOTIFY": json.dumps({"user_id": "u1", "channel_id": "c2c"}),
        }):
            import asyncio
            asyncio.run(client.on_ready())

        client.api.post_c2c_message.assert_called_once_with(
            openid="u1", msg_type=0, content="服务重启完成，可以继续使用。"
        )
        client.api.post_group_message.assert_not_called()

    def test_on_ready_sends_group_notification(self, client):
        """env var 有效时 on_ready 发送群聊重启完成通知。"""
        import json
        import os as _os
        with patch.object(_os, "environ", {
            "QQ_BOT_RESTART_NOTIFY": json.dumps({"user_id": "u2", "channel_id": "groupX"}),
        }):
            import asyncio
            asyncio.run(client.on_ready())

        client.api.post_group_message.assert_called_once_with(
            group_openid="groupX", msg_type=0, content="服务重启完成，可以继续使用。"
        )
        client.api.post_c2c_message.assert_not_called()

    def test_on_ready_no_env_var_is_noop(self, client):
        """env var 不存在时 on_ready 不做任何操作。"""
        with patch.dict("os.environ", {}, clear=True):
            import asyncio
            asyncio.run(client.on_ready())

        client.api.post_c2c_message.assert_not_called()
        client.api.post_group_message.assert_not_called()

    def test_on_ready_malformed_json_is_noop(self, client):
        """env var 值不是合法 JSON 时 on_ready 静默跳过。"""
        with patch.dict("os.environ", {"QQ_BOT_RESTART_NOTIFY": "not-json"}, clear=True):
            import asyncio
            asyncio.run(client.on_ready())

        client.api.post_c2c_message.assert_not_called()
        client.api.post_group_message.assert_not_called()

    def test_on_ready_missing_key_is_noop(self, client):
        """env var JSON 缺少必要字段时 on_ready 静默跳过。"""
        import json
        with patch.dict("os.environ", {
            "QQ_BOT_RESTART_NOTIFY": json.dumps({"user_id": "u1"}),  # no channel_id
        }, clear=True):
            import asyncio
            asyncio.run(client.on_ready())

        client.api.post_c2c_message.assert_not_called()
        client.api.post_group_message.assert_not_called()

    def test_on_ready_api_error_is_swallowed(self, client):
        """API 调用失败时 on_ready 不抛异常，静默处理。"""
        import json
        client.api.post_c2c_message = AsyncMock(side_effect=RuntimeError("network"))
        with patch.dict("os.environ", {
            "QQ_BOT_RESTART_NOTIFY": json.dumps({"user_id": "u1", "channel_id": "c2c"}),
        }, clear=True):
            import asyncio
            asyncio.run(client.on_ready())

        # 不抛异常即通过

    def test_on_ready_pops_env_var(self, client):
        """on_ready 处理完后从环境变量中移除 QQ_BOT_RESTART_NOTIFY。"""
        import json
        env = {"QQ_BOT_RESTART_NOTIFY": json.dumps({"user_id": "u1", "channel_id": "c2c"})}
        with patch.dict("os.environ", env, clear=True):
            import asyncio
            asyncio.run(client.on_ready())
            assert "QQ_BOT_RESTART_NOTIFY" not in os.environ
