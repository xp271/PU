"""Tests for qq_bot/channel.py — QQBotChannel."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
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
        """/cd 无参显示当前工作目录。"""
        mock_api = _setup_mock_api(channel)
        channel._agent_config.workspace_root = "C:\\PU"

        await channel.handle_message("u1", "c2c", "/cd", "text")

        mock_api.post_c2c_message.assert_called_once()
        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "C:\\PU" in content

    @pytest.mark.asyncio
    async def test_cd_shows_override_when_set(self, channel: QQBotChannel):
        """/cd 优先显示 workspace_override。"""
        mock_api = _setup_mock_api(channel)
        channel._agent_config.workspace_root = "C:\\PU"
        session = channel._bridge.get_session("u1", "c2c")
        session.workspace_override = "D:\\other"

        await channel.handle_message("u1", "c2c", "/cd", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "D:\\other" in content

    @pytest.mark.asyncio
    async def test_cd_switch_success(self, channel: QQBotChannel):
        """/cd <path> 切换并清空历史。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as tmpdir:
            await channel.handle_message("u1", "c2c", f"/cd {tmpdir}", "text")

            session = channel._bridge.get_session("u1", "c2c")
            assert session.workspace_override == os.path.abspath(tmpdir)
            assert session.messages == []

            content = mock_api.post_c2c_message.call_args.kwargs["content"]
            assert "已切换至" in content

    @pytest.mark.asyncio
    async def test_cd_switch_dir_not_exist(self, channel: QQBotChannel):
        """/cd 不存在的目录应报错，session 不变。"""
        mock_api = _setup_mock_api(channel)

        await channel.handle_message("u1", "c2c", "/cd Z:/does_not_exist", "text")

        session = channel._bridge.get_session("u1", "c2c")
        assert session.workspace_override is None

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "不存在" in content

    @pytest.mark.asyncio
    async def test_cd_reset(self, channel: QQBotChannel):
        """/cd --reset 清除 override。"""
        mock_api = _setup_mock_api(channel)
        channel._agent_config.workspace_root = "C:\\PU"
        session = channel._bridge.get_session("u1", "c2c")
        session.workspace_override = "D:\\other"

        await channel.handle_message("u1", "c2c", "/cd --reset", "text")

        assert session.workspace_override is None
        assert session.messages == []
        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "已重置" in content

    @pytest.mark.asyncio
    async def test_cd_switch_disabled(self, channel: QQBotChannel):
        """allow_shell_commands=False 时 /cd 返回未启用。"""
        mock_api = _setup_mock_api(channel)
        channel._config.allow_shell_commands = False

        await channel.handle_message("u1", "c2c", "/cd D:\\test", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "未启用" in content

    @pytest.mark.asyncio
    async def test_cd_group_chat(self, channel: QQBotChannel):
        """/cd 在群聊中也正常工作。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as tmpdir:
            await channel.handle_message("u1", "group123", f"/cd {tmpdir}", "text")

            mock_api.post_group_message.assert_called_once()
            content = mock_api.post_group_message.call_args.kwargs["content"]
            assert "已切换至" in content

    @pytest.mark.asyncio
    async def test_cd_no_agent_config_ws(self, channel: QQBotChannel):
        """/cd 无参且 workspace_root=None 时回退 os.getcwd()。"""
        mock_api = _setup_mock_api(channel)
        channel._agent_config.workspace_root = None

        await channel.handle_message("u1", "c2c", "/cd", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "(默认)" in content or "当前工作目录:" in content

    @pytest.mark.asyncio
    async def test_cd_relative_path(self, channel: QQBotChannel):
        """/cd subdir 相对路径应正确解析。"""
        mock_api = _setup_mock_api(channel)
        channel._agent_config.workspace_root = os.getcwd()

        with tempfile.TemporaryDirectory(dir=os.getcwd()) as tmpdir:
            rel_name = os.path.basename(tmpdir)
            await channel.handle_message("u1", "c2c", f"/cd {rel_name}", "text")

            session = channel._bridge.get_session("u1", "c2c")
            assert session.workspace_override == os.path.abspath(tmpdir)

            content = mock_api.post_c2c_message.call_args.kwargs["content"]
            assert "已切换至" in content

    @pytest.mark.asyncio
    async def test_cd_parent_dir_clamped(self, channel: QQBotChannel):
        """/cd .. 被 clamp 在原始 workspace_root 内。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as tmpdir:
            child = os.path.join(tmpdir, "child")
            os.makedirs(child)
            channel._agent_config.workspace_root = child

            session = channel._bridge.get_session("u1", "c2c")
            session.workspace_override = child

            await channel.handle_message("u1", "c2c", "/cd ..", "text")

            content = mock_api.post_c2c_message.call_args.kwargs["content"]
            # 应该被拒绝，因为 .. 会跳出 workspace_root(child)
            assert "无法切换" in content or "不存在" in content

    @pytest.mark.asyncio
    async def test_cd_absolute_outside_root(self, channel: QQBotChannel):
        """/cd 绝对路径超出 workspace_root 应被拒绝。"""
        mock_api = _setup_mock_api(channel)
        channel._agent_config.workspace_root = "C:\\some-narrow-dir"

        await channel.handle_message("u1", "c2c", "/cd C:\\Windows", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "无法切换" in content

    @pytest.mark.asyncio
    async def test_full_cd_flow_to_agent(self, channel: QQBotChannel):
        """/cd 切换后，下一条正常消息应使用覆盖后的 workspace_root。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Step 1: switch workspace
            await channel.handle_message("u1", "c2c", f"/cd {tmpdir}", "text")
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

    @pytest.mark.asyncio
    async def test_cd_independent_per_user(self, channel: QQBotChannel):
        """同一群聊中不同用户有独立的 workspace_override。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
            await channel.handle_message("u1", "groupX", f"/cd {t1}", "text")
            await channel.handle_message("u2", "groupX", f"/cd {t2}", "text")

            s1 = channel._bridge.get_session("u1", "groupX")
            s2 = channel._bridge.get_session("u2", "groupX")

            assert s1.workspace_override == os.path.abspath(t1)
            assert s2.workspace_override == os.path.abspath(t2)
            assert s1.workspace_override != s2.workspace_override

    @pytest.mark.asyncio
    async def test_cd_multi_parent_clamped(self, channel: QQBotChannel):
        """/cd ../../.. 多次 .. 始终被 clamp 在 workspace_root。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as tmpdir:
            # 三层嵌套：root/a/b/c
            a = os.path.join(tmpdir, "a")
            b = os.path.join(a, "b")
            c = os.path.join(b, "c")
            os.makedirs(c)
            channel._agent_config.workspace_root = c

            await channel.handle_message("u1", "c2c", "/cd ../../..", "text")

            content = mock_api.post_c2c_message.call_args.kwargs["content"]
            # 应被拒绝，因为 .. 试图跳出 workspace_root(c)
            assert "无法切换" in content or "不存在" in content

    @pytest.mark.asyncio
    async def test_cd_to_file_not_dir(self, channel: QQBotChannel):
        """/cd 指向文件（非目录）应报"目录不存在"。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as tmpdir:
            fp = os.path.join(tmpdir, "readme.txt")
            with open(fp, "w") as f:
                f.write("hello")
            channel._agent_config.workspace_root = tmpdir

            await channel.handle_message("u1", "c2c", f"/cd {fp}", "text")

            content = mock_api.post_c2c_message.call_args.kwargs["content"]
            assert "不存在" in content

    @pytest.mark.asyncio
    async def test_cd_only_whitespace_arg(self, channel: QQBotChannel):
        """/cd    （仅空白字符）等同于 /cd 无参，显示当前目录。"""
        mock_api = _setup_mock_api(channel)
        channel._agent_config.workspace_root = "C:\\PU"

        await channel.handle_message("u1", "c2c", "/cd    ", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "C:\\PU" in content

    @pytest.mark.asyncio
    async def test_cd_prefix_match_routes_to_cd(self, channel: QQBotChannel):
        """/cdxyz 前缀匹配被路由到 /cd 处理器，arg="xyz"。"""
        mock_api = _setup_mock_api(channel)

        # "xyz" is relative, workspace_root=None → fallback os.getcwd
        # "xyz" dir likely doesn't exist → 目录不存在
        await channel.handle_message("u1", "c2c", "/cdxyz", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        # Should NOT be "未知指令" — it's routed to cd handler
        assert "未知指令" not in content

    @pytest.mark.asyncio
    async def test_cd_multi_switch_chain(self, channel: QQBotChannel):
        """多次 /cd 串联，最后一次覆盖前一次。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
            await channel.handle_message("u1", "c2c", f"/cd {t1}", "text")
            await channel.handle_message("u1", "c2c", f"/cd {t2}", "text")

            session = channel._bridge.get_session("u1", "c2c")
            assert session.workspace_override == os.path.abspath(t2)

    @pytest.mark.asyncio
    async def test_cd_reset_when_no_override(self, channel: QQBotChannel):
        """/cd --reset 在无 override 时仍可执行（no-op）。"""
        mock_api = _setup_mock_api(channel)
        channel._agent_config.workspace_root = "C:\\PU"

        await channel.handle_message("u1", "c2c", "/cd --reset", "text")

        session = channel._bridge.get_session("u1", "c2c")
        assert session.workspace_override is None
        assert session.messages == []
        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "已重置" in content


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

    @pytest.mark.asyncio
    async def test_ls_after_cd_uses_override(self, channel: QQBotChannel):
        """/ls 在 /cd 切换后应列出新 workspace。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, "after_cd_dir"))
            # workspace_root 必须包含 tmpdir，否则 /cd 会被边界检查拒绝
            channel._agent_config.workspace_root = os.path.dirname(tmpdir)

            # 先切换
            await channel.handle_message("u1", "c2c", f"/cd {tmpdir}", "text")
            mock_api.post_c2c_message.reset_mock()

            await channel.handle_message("u1", "c2c", "/ls", "text")

            content = mock_api.post_c2c_message.call_args.kwargs["content"]
            assert tmpdir in content
            assert "📁 after_cd_dir/" in content

    @pytest.mark.asyncio
    async def test_ls_no_ws_root_fallback_cwd(self, channel: QQBotChannel):
        """/ls 在 workspace_root=None 时回退到 os.getcwd()。"""
        mock_api = _setup_mock_api(channel)
        channel._agent_config.workspace_root = None

        await channel.handle_message("u1", "c2c", "/ls", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        # 应显示当前工作目录路径
        assert os.getcwd() in content

    @pytest.mark.asyncio
    async def test_ls_on_file_returns_not_exist(self, channel: QQBotChannel):
        """/ls 指向文件（非目录）应返回"目录不存在"。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as tmpdir:
            fp = os.path.join(tmpdir, "readme.txt")
            with open(fp, "w") as f:
                f.write("hello")
            channel._agent_config.workspace_root = tmpdir

            await channel.handle_message("u1", "c2c", f"/ls readme.txt", "text")

            content = mock_api.post_c2c_message.call_args.kwargs["content"]
            assert "不存在" in content

    @pytest.mark.asyncio
    async def test_ls_oserror_permission_denied(self, channel: QQBotChannel):
        """/ls 遇到 PermissionError 等 OSError 应友好提示。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as tmpdir:
            channel._agent_config.workspace_root = tmpdir
            with patch("os.listdir", side_effect=PermissionError("denied")):
                await channel.handle_message("u1", "c2c", "/ls", "text")

            content = mock_api.post_c2c_message.call_args.kwargs["content"]
            assert "无法读取目录" in content

    @pytest.mark.asyncio
    async def test_ls_only_whitespace_arg(self, channel: QQBotChannel):
        """/ls    （仅空白字符）等同于 /ls 无参。"""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as tmpdir:
            channel._agent_config.workspace_root = tmpdir
            await channel.handle_message("u1", "c2c", "/ls    ", "text")

            content = mock_api.post_c2c_message.call_args.kwargs["content"]
            assert tmpdir in content


# ═══════════════════════════════════════════════════════════════════════
# TestClearCommand — /clear 指令
# ═══════════════════════════════════════════════════════════════════════


class TestClearCommand:

    @pytest.mark.asyncio
    async def test_clear_history(self, channel: QQBotChannel):
        """/clear 清除对话历史。"""
        mock_api = _setup_mock_api(channel)
        session = channel._bridge.get_session("u1", "c2c")
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
        session = channel._bridge.get_session("u1", "c2c")
        session.messages = [{"role": "user", "content": "hi"}]

        await channel.handle_message("u1", "c2c", "/clear", "text")

        assert session.messages == []
        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "已清除" in content  # 不是 "未启用"

    @pytest.mark.asyncio
    async def test_clear_empty_session_idempotent(self, channel: QQBotChannel):
        """/clear 在空 session 时幂等——不抛异常、消息为 []。"""
        mock_api = _setup_mock_api(channel)
        session = channel._bridge.get_session("u1", "c2c")
        session.clear_history()  # 确保空

        await channel.handle_message("u1", "c2c", "/clear", "text")

        assert session.messages == []
        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "已清除" in content


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
        assert "/cd" in content
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
        assert "/cd" in content  # help 正常显示


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

        await channel.handle_message("u1", "c2c", "/cd", "text")
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
        assert "/cd" in mock_api.post_c2c_message.call_args.kwargs["content"]


# ═══════════════════════════════════════════════════════════════════════
# TestCdCheckpoint — /cd and /cd --reset save checkpoint
# ═══════════════════════════════════════════════════════════════════════


class TestCdCheckpoint:
    @pytest.mark.asyncio
    async def test_cd_saves_checkpoint(self, channel: QQBotChannel):
        """/cd saves a checkpoint with the new workspace_override."""
        mock_api = _setup_mock_api(channel)

        with tempfile.TemporaryDirectory() as tmpdir:
            channel._agent_config.workspace_root = os.path.dirname(tmpdir)
            await channel.handle_message("u1", "c2c", f"/cd {tmpdir}", "text")

            # Verify checkpoint was saved
            ck_key = channel._checkpoint_key("c2c:u1")
            from agent_core.persistence.session import load_checkpoint
            ckpt = await load_checkpoint(
                ck_key, workspace_root=channel._agent_config.workspace_root,
            )
            assert ckpt is not None
            assert ckpt["workspace_override"] == os.path.abspath(tmpdir)

    @pytest.mark.asyncio
    async def test_cd_reset_saves_checkpoint(self, channel: QQBotChannel):
        """/cd --reset saves a checkpoint with workspace_override=null."""
        mock_api = _setup_mock_api(channel)
        channel._agent_config.workspace_root = "C:\\PU"
        session = channel._bridge.get_session("u1", "c2c")
        session.workspace_override = "D:\\other"

        await channel.handle_message("u1", "c2c", "/cd --reset", "text")

        ck_key = channel._checkpoint_key("c2c:u1")
        from agent_core.persistence.session import load_checkpoint
        ckpt = await load_checkpoint(
            ck_key, workspace_root=channel._agent_config.workspace_root,
        )
        assert ckpt is not None
        assert ckpt["workspace_override"] is None


# ═══════════════════════════════════════════════════════════════════════
# TestCheckpointKey — sanitize bridge session ID for filesystem
# ═══════════════════════════════════════════════════════════════════════


class TestCheckpointKey:
    def test_checkpoint_key_sanitize(self):
        """_checkpoint_key replaces :, \\, / with _."""
        assert QQBotChannel._checkpoint_key("c2c:user1") == "c2c_user1"
        assert QQBotChannel._checkpoint_key("group123:user_2") == "group123_user_2"
        assert QQBotChannel._checkpoint_key("a\\b/c:d") == "a_b_c_d"


# ═══════════════════════════════════════════════════════════════════════
# TestSessionCreateCheckpoint — session create triggers checkpoint restore
# ═══════════════════════════════════════════════════════════════════════


class TestSessionCreateCheckpoint:
    @pytest.mark.asyncio
    async def test_session_create_loads_checkpoint(self, channel: QQBotChannel):
        """When a new session is created, workspace_override from checkpoint
        is restored via the on_session_created callback."""
        mock_api = _setup_mock_api(channel)

        # Save a checkpoint first
        from agent_core.persistence.session import save_checkpoint
        s = channel._bridge.get_session("u1", "c2c")
        s.workspace_override = "D:\\saved_ws"
        await save_checkpoint(
            s,
            checkpoint_key=channel._checkpoint_key("c2c:u1"),
            workspace_root=channel._agent_config.workspace_root,
        )

        # Clear and re-create — the on_session_created callback should
        # restore workspace_override from checkpoint
        channel._bridge.clear_session("u1", "c2c")
        # Process a new message which triggers _get_or_create_session + callback
        with patch(
            "agent_core.bridge.agent_bridge.Agent",
            return_value=_make_mock_agent(["ok"]),
        ):
            await channel.handle_message("u1", "c2c", "hello again", "text")

        restored = channel._bridge.get_session("u1", "c2c")
        assert restored.workspace_override == "D:\\saved_ws"


# ═══════════════════════════════════════════════════════════════════════
# TestRestartCommand — /restart 指令
# ═══════════════════════════════════════════════════════════════════════


class TestRestartCommand:

    @pytest.mark.asyncio
    async def test_restart_c2c_sends_message_then_execv(self, channel: QQBotChannel):
        """/restart 在 C2C 中发送提示消息后调用 os.execv。"""
        mock_api = _setup_mock_api(channel)

        with patch("os.execv") as mock_execv, \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch.dict(os.environ, {}, clear=True):
            await channel.handle_message("u1", "c2c", "/restart", "text")

        mock_api.post_c2c_message.assert_called_once()
        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "重启" in content
        mock_execv.assert_called_once()

    @pytest.mark.asyncio
    async def test_restart_group_sends_message(self, channel: QQBotChannel):
        """/restart 在群聊中也发送提示消息。"""
        mock_api = _setup_mock_api(channel)

        with patch("os.execv"), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch.dict(os.environ, {}, clear=True):
            await channel.handle_message("u1", "group123", "/restart", "text")

        mock_api.post_group_message.assert_called_once()
        content = mock_api.post_group_message.call_args.kwargs["content"]
        assert "重启" in content

    @pytest.mark.asyncio
    async def test_restart_unaffected_by_allow_shell_commands(self, channel: QQBotChannel):
        """allow_shell_commands=False 时 /restart 仍可用。"""
        mock_api = _setup_mock_api(channel)
        channel._config.allow_shell_commands = False

        with patch("os.execv"), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch.dict(os.environ, {}, clear=True):
            await channel.handle_message("u1", "c2c", "/restart", "text")

        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "重启" in content  # 不是 "未启用"

    @pytest.mark.asyncio
    async def test_restart_sets_env_var_with_user_channel(self, channel: QQBotChannel):
        """/restart 在 execv 前设置 QQ_BOT_RESTART_NOTIFY 环境变量。"""
        _setup_mock_api(channel)

        with patch("os.execv"), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch.dict(os.environ, {}, clear=True):
            await channel.handle_message("u1", "groupX", "/restart", "text")

            import json
            raw = os.environ.get("QQ_BOT_RESTART_NOTIFY", "")
            assert raw
            info = json.loads(raw)
            assert info["user_id"] == "u1"
            assert info["channel_id"] == "groupX"

    @pytest.mark.asyncio
    async def test_restart_env_var_overwrites_previous(self, channel: QQBotChannel):
        """重复 /restart 时环境变量被覆盖为最新值。"""
        _setup_mock_api(channel)

        with patch("os.execv"), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch.dict(os.environ, {"QQ_BOT_RESTART_NOTIFY": '{"old":"data"}'},
                        clear=True):
            await channel.handle_message("u2", "c2c", "/restart", "text")

            import json
            info = json.loads(os.environ["QQ_BOT_RESTART_NOTIFY"])
            assert info["user_id"] == "u2"

    @pytest.mark.asyncio
    async def test_restart_execv_uses_sys_executable(self, channel: QQBotChannel):
        """os.execv 使用 sys.argv[0]（原始入口脚本）而非 sys.executable，
        过滤原始 --config 并替换为绝对路径。"""
        _setup_mock_api(channel)

        with patch("os.execv") as mock_execv, \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch.dict(os.environ, {}, clear=True), \
             patch("agent_core.channels.qq_bot.channel.sys") as mock_sys:
            mock_sys.argv = ["agent-core-qqbot", "start", "--config", "cfg.json"]
            # 模拟启动时已解析的配置路径
            channel._config_path = Path("/abs/path/to/config.json")
            await channel.handle_message("u1", "c2c", "/restart", "text")

        mock_execv.assert_called_once_with(
            "agent-core-qqbot",
            ["agent-core-qqbot", "start",
             "--config", str(Path("/abs/path/to/config.json"))],
        )

    @pytest.mark.asyncio
    async def test_restart_without_client_still_execv(self, channel: QQBotChannel):
        """无 client 时 /restart 不抛异常，仍调用 execv（消息发送静默失败）。"""
        with patch("os.execv") as mock_execv, \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch.dict(os.environ, {}, clear=True):
            await channel.handle_message("u1", "c2c", "/restart", "text")

        mock_execv.assert_called_once()

    @pytest.mark.asyncio
    async def test_restart_with_args_is_unknown(self, channel: QQBotChannel):
        """/restart 后带参数（如 /restart now）不精确匹配，落到未知指令分支。"""
        mock_api = _setup_mock_api(channel)

        with patch("os.execv") as mock_execv:
            await channel.handle_message("u1", "c2c", "/restart now", "text")

        mock_execv.assert_not_called()
        content = mock_api.post_c2c_message.call_args.kwargs["content"]
        assert "未知指令" in content
