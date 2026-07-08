"""Tests for qq_bot/channel.py — QQBotChannel."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.channels.qq_bot.types import QQBotConfig
from agent_core.channels.qq_bot.channel import QQBotChannel


@pytest.fixture
def config() -> QQBotConfig:
    return QQBotConfig(app_id="test_app_12345", app_secret="test_secret_abc")


@pytest.fixture
def channel(config: QQBotConfig) -> QQBotChannel:
    with patch(
        "agent_core.channels.qq_bot.channel._load_global_agent_config",
        return_value=None,
    ):
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


def _setup_mock_api(channel: QQBotChannel) -> AsyncMock:
    """Inject a mock API into the channel and return it."""
    mock_api = AsyncMock()
    channel._client = MagicMock()
    channel._client.api = mock_api
    return mock_api


# ═══════════════════════════════════════════════════════════════════════
# TestQQBotChannel — 基础消息处理（非 shell 指令）
# ═══════════════════════════════════════════════════════════════════════


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
        """无 client 时非 / 消息走 Agent，但无 API 可发消息—不抛异常。"""
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


# ═══════════════════════════════════════════════════════════════════════
# TestCdCommand — /cd 指令
# ═══════════════════════════════════════════════════════════════════════


class TestCdCommand:

    @pytest.mark.asyncio
    async def test_cd_no_arg_shows_current(self, channel: QQBotChannel):
        """/cdp 无参显示当前工作目录。"""
        mock_api = _setup_mock_api(channel)
        channel._agent_config.workspace_root = "C:\\PU"

        await channel.handle_message("u1", "c2c", "/cdp", "text")

        mock_api.post_c2c_message.assert_called_once()
        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "C:\\PU" in content

    @pytest.mark.asyncio
    async def test_cd_shows_override_when_set(self, channel: QQBotChannel):
        """/cdp优先显示 workspace_override。"""
        mock_api = _setup_mock_api(channel)
        channel._agent_config.workspace_root = "C:\\PU"
        session = await channel._bridge.get_session("u1", "c2c")
        session.workspace_override = "D:\\other"

        await channel.handle_message("u1", "c2c", "/cdp", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "D:\\other" in content

    @pytest.mark.asyncio
    async def test_cd_switch_success(self, channel: QQBotChannel):
        """/cdp<path> 切换并清空历史。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as tmpdir:
            await channel.handle_message("u1", "c2c", f"/cdp{tmpdir}", "text")

            session = await channel._bridge.get_session("u1", "c2c")
            assert session.workspace_override == os.path.abspath(tmpdir)
            assert session.messages == []

            content = mock_api.post_c2c_message.call_args.kwargs["content"]
            assert "已切换至" in content

    @pytest.mark.asyncio
    async def test_cd_switch_dir_not_exist(self, channel: QQBotChannel):
        """/cdp不存在的目录应报错，session 不变。"""
        mock_api = _setup_mock_api(channel)

        await channel.handle_message("u1", "c2c", "/cdpZ:/does_not_exist", "text")

        session = await channel._bridge.get_session("u1", "c2c")
        assert session.workspace_override is None

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "不存在" in content

    @pytest.mark.asyncio
    async def test_cd_reset(self, channel: QQBotChannel):
        """/cdp--reset 清除 override。"""
        mock_api = _setup_mock_api(channel)
        channel._agent_config.workspace_root = "C:\\PU"
        session = await channel._bridge.get_session("u1", "c2c")
        session.workspace_override = "D:\\other"

        await channel.handle_message("u1", "c2c", "/cdp--reset", "text")

        assert session.workspace_override is None
        assert session.messages == []
        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "已重置" in content

    @pytest.mark.asyncio
    async def test_cd_switch_disabled(self, channel: QQBotChannel):
        """allow_shell_commands=False 时 /cd 返回未启用。"""
        mock_api = _setup_mock_api(channel)
        channel._config.allow_shell_commands = False

        await channel.handle_message("u1", "c2c", "/cdpD:\\test", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "未启用" in content

    @pytest.mark.asyncio
    async def test_cd_group_chat(self, channel: QQBotChannel):
        """/cdp在群聊中也正常工作。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as tmpdir:
            await channel.handle_message("u1", "group123", f"/cdp{tmpdir}", "text")

            mock_api.post_group_message.assert_called_once()
            content = mock_api.post_group_message.call_args.kwargs["content"]
            assert "已切换至" in content

    @pytest.mark.asyncio
    async def test_cd_no_agent_config_ws(self, channel: QQBotChannel):
        """/cdp 无参且 workspace_root=None 时回退 os.getcwd()。"""
        mock_api = _setup_mock_api(channel)
        channel._agent_config.workspace_root = None

        await channel.handle_message("u1", "c2c", "/cdp", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "(默认)" in content or "当前工作目录:" in content

    @pytest.mark.asyncio
    async def test_cd_relative_path(self, channel: QQBotChannel):
        """/cdpsubdir 相对路径应正确解析。"""
        mock_api = _setup_mock_api(channel)
        channel._agent_config.workspace_root = os.getcwd()

        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmpdir:
            rel_name = os.path.basename(tmpdir)
            await channel.handle_message("u1", "c2c", f"/cdp{rel_name}", "text")

            session = await channel._bridge.get_session("u1", "c2c")
            assert session.workspace_override == os.path.abspath(tmpdir)

            content = mock_api.post_c2c_message.call_args.kwargs["content"]
            assert "已切换至" in content

    @pytest.mark.asyncio
    async def test_cd_parent_dir_clamped(self, channel: QQBotChannel):
        """/cdp.. 被 clamp 在原始 workspace_root 内。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as tmpdir:
            child = os.path.join(tmpdir, "child")
            os.makedirs(child)
            channel._agent_config.workspace_root = child

            session = await channel._bridge.get_session("u1", "c2c")
            session.workspace_override = child

            await channel.handle_message("u1", "c2c", "/cdp..", "text")

            content = mock_api.post_c2c_message.call_args.kwargs["content"]
            # 应该被拒绝，因为 .. 会跳出 workspace_root(child)
            assert "无法切换" in content or "不存在" in content

    @pytest.mark.asyncio
    async def test_cd_absolute_outside_root(self, channel: QQBotChannel):
        """/cdp绝对路径超出 workspace_root 应被拒绝。"""
        mock_api = _setup_mock_api(channel)
        channel._agent_config.workspace_root = "C:\\some-narrow-dir"

        await channel.handle_message("u1", "c2c", "/cdpC:\\Windows", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "无法切换" in content

    @pytest.mark.asyncio
    async def test_full_cd_flow_to_agent(self, channel: QQBotChannel):
        """/cdp切换后，下一条正常消息应使用覆盖后的 workspace_root。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Step 1: switch workspace
            await channel.handle_message("u1", "c2c", f"/cdp{tmpdir}", "text")
            session = await channel._bridge.get_session("u1", "c2c")
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

    @pytest.mark.asyncio
    async def test_cd_independent_per_user(self, channel: QQBotChannel):
        """同一群聊中不同用户有独立的 workspace_override。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
            await channel.handle_message("u1", "groupX", f"/cdp{t1}", "text")
            await channel.handle_message("u2", "groupX", f"/cdp{t2}", "text")

            s1 = await channel._bridge.get_session("u1", "groupX")
            s2 = await channel._bridge.get_session("u2", "groupX")

            assert s1.workspace_override == os.path.abspath(t1)
            assert s2.workspace_override == os.path.abspath(t2)
            assert s1.workspace_override != s2.workspace_override


# ═══════════════════════════════════════════════════════════════════════
# TestLsCommand — /ls 指令
# ═══════════════════════════════════════════════════════════════════════


class TestLsCommand:

    @pytest.mark.asyncio
    async def test_ls_current_dir(self, channel: QQBotChannel):
        """/ls 列出当前 effective workspace。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as tmpdir:
            # create some files/dirs
            os.makedirs(os.path.join(tmpdir, "subdir"))
            with open(os.path.join(tmpdir, "file.txt"), "w") as f:
                f.write("x")

            channel._agent_config.workspace_root = tmpdir
            await channel.handle_message("u1", "c2c", "/ls", "text")

            mock_api.post_c2c_message.assert_called_once()
            content = mock_api.post_c2c_message.call_args.kwargs["content"]
            assert tmpdir in content
            assert "📁 subdir/" in content
            assert "📄 file.txt" in content

    @pytest.mark.asyncio
    async def test_ls_subpath(self, channel: QQBotChannel):
        """/ls subdir 列出子目录。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as tmpdir:
            sub = os.path.join(tmpdir, "sub")
            os.makedirs(sub)
            with open(os.path.join(sub, "a.txt"), "w") as f:
                f.write("x")

            channel._agent_config.workspace_root = tmpdir
            await channel.handle_message("u1", "c2c", "/ls sub", "text")

            content = mock_api.post_c2c_message.call_args.kwargs["content"]
            assert sub in content
            assert "📄 a.txt" in content

    @pytest.mark.asyncio
    async def test_ls_not_exist(self, channel: QQBotChannel):
        """/ls 不存在的目录应报错。"""
        mock_api = _setup_mock_api(channel)

        await channel.handle_message("u1", "c2c", "/ls Z:/nope", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "不存在" in content

    @pytest.mark.asyncio
    async def test_ls_empty_dir(self, channel: QQBotChannel):
        """/ls 空目录显示 (空目录)。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as tmpdir:
            channel._agent_config.workspace_root = tmpdir
            await channel.handle_message("u1", "c2c", "/ls", "text")

            content = mock_api.post_c2c_message.call_args.kwargs["content"]
            assert "(空目录)" in content

    @pytest.mark.asyncio
    async def test_ls_disabled(self, channel: QQBotChannel):
        """allow_shell_commands=False 时 /ls 返回未启用。"""
        mock_api = _setup_mock_api(channel)
        channel._config.allow_shell_commands = False

        await channel.handle_message("u1", "c2c", "/ls", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "未启用" in content

    @pytest.mark.asyncio
    async def test_ls_outside_root_blocked(self, channel: QQBotChannel):
        """/ls ../../.. 越权应被阻断。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as tmpdir:
            channel._agent_config.workspace_root = tmpdir
            await channel.handle_message("u1", "c2c", "/ls ../../..", "text")

            content = mock_api.post_c2c_message.call_args.kwargs["content"]
            assert "无法切换" in content

    @pytest.mark.asyncio
    async def test_ls_truncation_over_50(self, channel: QQBotChannel):
        """/ls 超过 50 个条目时截断提示。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(55):
                with open(os.path.join(tmpdir, f"file_{i:03d}.txt"), "w") as f:
                    f.write("x")

            channel._agent_config.workspace_root = tmpdir
            await channel.handle_message("u1", "c2c", "/ls", "text")

            content = mock_api.post_c2c_message.call_args.kwargs["content"]
            assert "及另外" in content
            assert "5 个条目" in content  # 55 - 50


# ═══════════════════════════════════════════════════════════════════════
# TestClearCommand — /clear 指令
# ═══════════════════════════════════════════════════════════════════════


class TestClearCommand:

    @pytest.mark.asyncio
    async def test_clear_history(self, channel: QQBotChannel):
        """/clear 清除对话历史。"""
        mock_api = _setup_mock_api(channel)
        session = await channel._bridge.get_session("u1", "c2c")
        session.messages = [{"role": "user", "content": "hello"}]
        session._written_count = 1

        await channel.handle_message("u1", "c2c", "/clear", "text")

        assert session.messages == []
        assert session._written_count == 0
        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "已清除" in content

    @pytest.mark.asyncio
    async def test_clear_unaffected_by_allow_shell_commands(self, channel: QQBotChannel):
        """/clear 不受 allow_shell_commands=False 影响。"""
        mock_api = _setup_mock_api(channel)
        channel._config.allow_shell_commands = False
        session = await channel._bridge.get_session("u1", "c2c")
        session.messages = [{"role": "user", "content": "hi"}]

        await channel.handle_message("u1", "c2c", "/clear", "text")

        assert session.messages == []
        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "已清除" in content  # 不是 "未启用"


# ═══════════════════════════════════════════════════════════════════════
# TestHelpCommand — /help 指令
# ═══════════════════════════════════════════════════════════════════════


class TestHelpCommand:

    @pytest.mark.asyncio
    async def test_help_shows_commands(self, channel: QQBotChannel):
        """/help 显示可用指令列表。"""
        mock_api = _setup_mock_api(channel)

        await channel.handle_message("u1", "c2c", "/help", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "/cdp" in content
        assert "/ls" in content
        assert "/clear" in content
        assert "/help" in content

    @pytest.mark.asyncio
    async def test_help_unaffected_by_allow_shell_commands(self, channel: QQBotChannel):
        """/help 不受 allow_shell_commands=False 影响。"""
        mock_api = _setup_mock_api(channel)
        channel._config.allow_shell_commands = False

        await channel.handle_message("u1", "c2c", "/help", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "/cdp" in content  # help 正常显示


# ═══════════════════════════════════════════════════════════════════════
# TestUnknownCommand — 未知 / 指令
# ═══════════════════════════════════════════════════════════════════════


class TestUnknownCommand:

    @pytest.mark.asyncio
    async def test_unknown_slash_command(self, channel: QQBotChannel):
        """未知 / 指令返回提示。"""
        mock_api = _setup_mock_api(channel)

        await channel.handle_message("u1", "c2c", "/foobar", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "未知指令" in content
        assert "/help" in content

    @pytest.mark.asyncio
    async def test_legacy_workspace_is_unknown(self, channel: QQBotChannel):
        """旧 /workspace 被视为未知指令。"""
        mock_api = _setup_mock_api(channel)

        await channel.handle_message("u1", "c2c", "/workspace", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "未知指令" in content
        assert "/workspace" in content

    @pytest.mark.asyncio
    async def test_slash_without_client_silent(self, channel: QQBotChannel):
        """无 client 时 / 指令不抛异常。"""
        await channel.handle_message("u1", "c2c", "/help", "text")
        # no exception


# ═══════════════════════════════════════════════════════════════════════
# TestAllowShellCommandsFlag — allow_shell_commands 开关范围
# ═══════════════════════════════════════════════════════════════════════


class TestAllowShellCommandsFlag:

    @pytest.mark.asyncio
    async def test_cd_blocked_when_disabled(self, channel: QQBotChannel):
        """allow_shell_commands=False → /cd 被阻止。"""
        mock_api = _setup_mock_api(channel)
        channel._config.allow_shell_commands = False

        await channel.handle_message("u1", "c2c", "/cdp", "text")
        assert "未启用" in mock_api.post_c2c_message.call_args.kwargs["content"]

    @pytest.mark.asyncio
    async def test_ls_blocked_when_disabled(self, channel: QQBotChannel):
        """allow_shell_commands=False → /ls 被阻止。"""
        mock_api = _setup_mock_api(channel)
        channel._config.allow_shell_commands = False

        await channel.handle_message("u1", "c2c", "/ls", "text")
        assert "未启用" in mock_api.post_c2c_message.call_args.kwargs["content"]

    @pytest.mark.asyncio
    async def test_clear_not_blocked(self, channel: QQBotChannel):
        """allow_shell_commands=False → /clear 仍可用。"""
        mock_api = _setup_mock_api(channel)
        channel._config.allow_shell_commands = False

        await channel.handle_message("u1", "c2c", "/clear", "text")
        assert "已清除" in mock_api.post_c2c_message.call_args.kwargs["content"]

    @pytest.mark.asyncio
    async def test_help_not_blocked(self, channel: QQBotChannel):
        """allow_shell_commands=False → /help 仍可用。"""
        mock_api = _setup_mock_api(channel)
        channel._config.allow_shell_commands = False

        await channel.handle_message("u1", "c2c", "/help", "text")
        assert "/cdp" in mock_api.post_c2c_message.call_args.kwargs["content"]


# ═══════════════════════════════════════════════════════════════════════
# TestEdgeCases — 边缘条件 & fallback
# ═══════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Channel 层边界条件和降级路径测试。"""

    # -- _resolve_path 边缘 --------------------------------------------------

    @pytest.mark.asyncio
    async def test_resolve_path_no_workspace_root_or_override(self, channel: QQBotChannel):
        """workspace_root=None 且 workspace_override=None → 回退到 os.getcwd()。

        _load_global_agent_config mock 返回 None → workspace_root 为 None，
        session 无 override → effective = os.getcwd()。
        """
        channel._agent_config.workspace_root = None

        session = await channel._bridge.get_session("u1", "c2c")
        session.workspace_override = None

        path, err = await channel._resolve_path("u1", "c2c", "")
        assert err is None
        assert path == os.getcwd()

    @pytest.mark.asyncio
    async def test_resolve_path_empty_arg_returns_effective(self, channel: QQBotChannel):
        """空字符串 arg → 返回 effective workspace 而非 None。"""
        channel._agent_config.workspace_root = str(tempfile.gettempdir())

        path, err = await channel._resolve_path("u1", "c2c", "")
        assert err is None
        assert path == str(tempfile.gettempdir())

    @pytest.mark.asyncio
    async def test_resolve_path_override_none_root_none_absolute(self, channel: QQBotChannel):
        """workspace_root=None 时绝对路径不受限（无沙箱）"""
        channel._agent_config.workspace_root = None

        path, err = await channel._resolve_path(
            "u1", "c2c", os.path.abspath(tempfile.gettempdir())
        )
        assert err is None
        assert path == os.path.abspath(tempfile.gettempdir())

    @pytest.mark.asyncio
    async def test_resolve_path_relative_no_effective(self, channel: QQBotChannel):
        """workspace_root=None 且无 override → os.getcwd() 兜底。

        effective 链: workspace_override → workspace_root → os.getcwd()
        所以 effective 永远不是 None（os.getcwd() 兜底）。
        本测试验证用 cwd 下的真实子目录解析相对路径。
        """
        channel._agent_config.workspace_root = None
        session = await channel._bridge.get_session("u1", "c2c")
        session.workspace_override = None

        # 使用当前目录下的真实子目录
        real_subdir = "tests"
        path, err = await channel._resolve_path("u1", "c2c", real_subdir)
        # os.getcwd() 兜底 → relative 基于 cwd → 解析到 tests/
        assert err is None
        assert path is not None
        assert os.path.isabs(path)
        assert path.endswith("tests")

    # -- /clear 不重置 workspace_override -----------------------------------

    @pytest.mark.asyncio
    async def test_clear_preserves_workspace_override(self, channel: QQBotChannel):
        """/clear 清除对话历史但不影响 workspace_override。"""
        _setup_mock_api(channel)

        session = await channel._bridge.get_session("u1", "c2c")
        session.workspace_override = str(tempfile.gettempdir())

        await channel.handle_message("u1", "c2c", "/clear", "text")

        # workspace_override 不变
        session2 = await channel._bridge.get_session("u1", "c2c")
        assert session2.workspace_override == str(tempfile.gettempdir())

    # -- _send_message 无 client 时不崩溃 -----------------------------------

    @pytest.mark.asyncio
    async def test_send_message_without_client_silent(self, channel: QQBotChannel):
        """无 client 时 _send_message 静默返回，不抛异常。"""
        channel._client = None
        await channel._send_message("u1", "c2c", "test")
        # 不抛异常

    # -- handle_message 非 slash 路径无 client -------------------------------

    @pytest.mark.asyncio
    async def test_handle_message_no_client_graceful(self, channel: QQBotChannel):
        """无 client 时正常消息处理不崩溃（replies 无法发送但不出错）。"""
        channel._client = None

        channel._bridge.process = AsyncMock(return_value=["reply text"])
        await channel.handle_message("u1", "c2c", "hello", "text")
        # 不抛异常 —— replies 循环中 api=None → skip

    # -- /cd 路径中带空格 ----------------------------------------------------

    @pytest.mark.asyncio
    async def test_cd_path_with_spaces(self, channel: QQBotChannel):
        """/cdp到带空格的路径（如 Windows 常见）"""
        mock_api = _setup_mock_api(channel)
        channel._agent_config.workspace_root = tempfile.gettempdir()

        spaced_dir = os.path.join(tempfile.gettempdir(), "test dir with spaces")
        os.makedirs(spaced_dir, exist_ok=True)
        try:
            await channel.handle_message("u1", "c2c", "/cdp" + spaced_dir, "text")

            content = mock_api.post_c2c_message.call_args.kwargs["content"]
            assert "已切换" in content
            assert spaced_dir in content
        finally:
            os.rmdir(spaced_dir)

    # -- 空消息处理 ----------------------------------------------------------

    @pytest.mark.asyncio
    async def test_handle_empty_message(self, channel: QQBotChannel):
        """空消息（仅空白）仍走 Agent 处理，不 crash。"""
        _setup_mock_api(channel)

        mock_process = AsyncMock(return_value=["ok"])
        channel._bridge.process = mock_process
        await channel.handle_message("u1", "c2c", "   ", "text")
        mock_process.assert_called_once_with("u1", "c2c", "   ", "text")

    # -- 超长消息 ------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_handle_very_long_message(self, channel: QQBotChannel):
        """超长消息（>2000 字符）正常传入 Agent 处理。"""
        _setup_mock_api(channel)

        long_msg = "x" * 5000
        mock_process = AsyncMock(return_value=["ok"])
        channel._bridge.process = mock_process
        await channel.handle_message("u1", "c2c", long_msg, "text")
        mock_process.assert_called_once_with("u1", "c2c", long_msg, "text")
