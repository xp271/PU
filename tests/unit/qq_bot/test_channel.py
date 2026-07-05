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


class TestWorkspaceCommand:
    """QQBotChannel._handle_workspace_command 测试。"""

    @pytest.mark.asyncio
    async def test_workspace_no_arg_shows_current(self, channel: "QQBotChannel"):
        """无参数 /workspace 显示当前工作目录。"""
        mock_api = AsyncMock()
        channel._client = MagicMock()
        channel._client.api = mock_api
        channel._agent_config.workspace_root = "C:\\PU"

        await channel.handle_message("u1", "c2c", "/workspace", "text")

        mock_api.post_c2c_message.assert_called_once()
        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "C:\\PU" in content

    @pytest.mark.asyncio
    async def test_workspace_shows_override_when_set(self, channel: "QQBotChannel"):
        """/workspace 应优先显示 workspace_override。"""
        mock_api = AsyncMock()
        channel._client = MagicMock()
        channel._client.api = mock_api
        channel._agent_config.workspace_root = "C:\\PU"
        session = channel._bridge.get_session("u1", "c2c")
        session.workspace_override = "D:\\other"

        await channel.handle_message("u1", "c2c", "/workspace", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "D:\\other" in content

    @pytest.mark.asyncio
    async def test_workspace_switch_success(self, channel: "QQBotChannel"):
        """/workspace <path> 切换并清空历史。"""
        import os, tempfile
        mock_api = AsyncMock()
        channel._client = MagicMock()
        channel._client.api = mock_api

        with tempfile.TemporaryDirectory() as tmpdir:
            await channel.handle_message("u1", "c2c", f"/workspace {tmpdir}", "text")

            session = channel._bridge.get_session("u1", "c2c")
            assert session.workspace_override == os.path.abspath(tmpdir)
            assert session.messages == []

            content = mock_api.post_c2c_message.call_args.kwargs["content"]
            assert "已切换至" in content

    @pytest.mark.asyncio
    async def test_workspace_switch_dir_not_exist(self, channel: "QQBotChannel"):
        """/workspace 不存在的目录应报错，session 不变。"""
        mock_api = AsyncMock()
        channel._client = MagicMock()
        channel._client.api = mock_api

        await channel.handle_message("u1", "c2c", "/workspace Z:/does_not_exist", "text")

        session = channel._bridge.get_session("u1", "c2c")
        assert session.workspace_override is None

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "不存在" in content

    @pytest.mark.asyncio
    async def test_workspace_reset(self, channel: "QQBotChannel"):
        """/workspace --reset 清除 override。"""
        mock_api = AsyncMock()
        channel._client = MagicMock()
        channel._client.api = mock_api
        channel._agent_config.workspace_root = "C:\\PU"
        session = channel._bridge.get_session("u1", "c2c")
        session.workspace_override = "D:\\other"

        await channel.handle_message("u1", "c2c", "/workspace --reset", "text")

        assert session.workspace_override is None
        assert session.messages == []
        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "已重置" in content

    @pytest.mark.asyncio
    async def test_workspace_switch_disabled(self, channel: "QQBotChannel"):
        """allow_workspace_switch=False 时返回错误。"""
        mock_api = AsyncMock()
        channel._client = MagicMock()
        channel._client.api = mock_api
        channel._config.allow_workspace_switch = False

        await channel.handle_message("u1", "c2c", "/workspace D:\\test", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "未启用" in content

    @pytest.mark.asyncio
    async def test_workspace_group_chat(self, channel: "QQBotChannel"):
        """/workspace 在群聊中也正常工作。"""
        import os, tempfile
        mock_api = AsyncMock()
        channel._client = MagicMock()
        channel._client.api = mock_api

        with tempfile.TemporaryDirectory() as tmpdir:
            await channel.handle_message("u1", "group123", f"/workspace {tmpdir}", "text")

            mock_api.post_group_message.assert_called_once()
            content = mock_api.post_group_message.call_args.kwargs["content"]
            assert "已切换至" in content

    @pytest.mark.asyncio
    async def test_workspace_no_agent_config_ws(self, channel: "QQBotChannel"):
        """/workspace 无参数且 agent_config.workspace_root 为 None 时显示提示。"""
        mock_api = AsyncMock()
        channel._client = MagicMock()
        channel._client.api = mock_api
        channel._agent_config.workspace_root = None

        await channel.handle_message("u1", "c2c", "/workspace", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "未设置" in content

    # ------------------------------------------------------------------
    # Plan §6 item 11: full /workspace → Agent integration
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_full_workspace_flow_to_agent(self, channel: "QQBotChannel"):
        """/workspace 切换后，下一条正常消息应使用覆盖后的 workspace_root。"""
        import os, tempfile
        mock_api = AsyncMock()
        channel._client = MagicMock()
        channel._client.api = mock_api

        with tempfile.TemporaryDirectory() as tmpdir:
            # Step 1: switch workspace
            await channel.handle_message("u1", "c2c", f"/workspace {tmpdir}", "text")
            session = channel._bridge.get_session("u1", "c2c")
            expected = os.path.abspath(tmpdir)
            assert session.workspace_override == expected
            assert session.messages == []

            # Step 2: send normal message — verify Agent receives override
            mock_agent = _make_mock_agent(["ok"])
            with patch("agent_core.bridge.agent_bridge.Agent", return_value=mock_agent) as mock_cls:
                await channel.handle_message("u1", "c2c", "hello", "text")

                call_args = mock_cls.call_args
                passed_config = call_args[0][0]
                assert passed_config.workspace_root == expected

    # ------------------------------------------------------------------
    # Boundary: relative path → abs
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_workspace_relative_path_to_absolute(self, channel: "QQBotChannel"):
        """/workspace 相对路径应被 os.path.abspath() 转为绝对路径。"""
        import os
        mock_api = AsyncMock()
        channel._client = MagicMock()
        channel._client.api = mock_api

        await channel.handle_message("u1", "c2c", "/workspace ..", "text")

        session = channel._bridge.get_session("u1", "c2c")
        assert session.workspace_override == os.path.abspath("..")
        assert os.path.isabs(session.workspace_override)

    # ------------------------------------------------------------------
    # Boundary: independent workspace per user in same channel
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_workspace_independent_per_user(self, channel: "QQBotChannel"):
        """同一群聊中不同用户有独立的 workspace_override。"""
        import os, tempfile
        mock_api = AsyncMock()
        channel._client = MagicMock()
        channel._client.api = mock_api

        with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
            await channel.handle_message("u1", "groupX", f"/workspace {t1}", "text")
            await channel.handle_message("u2", "groupX", f"/workspace {t2}", "text")

            s1 = channel._bridge.get_session("u1", "groupX")
            s2 = channel._bridge.get_session("u2", "groupX")

            assert s1.workspace_override == os.path.abspath(t1)
            assert s2.workspace_override == os.path.abspath(t2)
            assert s1.workspace_override != s2.workspace_override
