"""QQ 机器人信道调度器。

比 WechatKFChannel 简洁得多——不需要 crypto、token、sync、dedup。
botpy 封装了 WebSocket 连接和 AccessToken 管理。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, TYPE_CHECKING

from agent_core.models import AgentConfig

from .client import QQBotClient
from .types import QQBotConfig

if TYPE_CHECKING:
    from agent_core.session import SessionState

logger = logging.getLogger("agent_core.qq_bot.channel")

# ---------------------------------------------------------------------------
# 帮助文本
# ---------------------------------------------------------------------------

_QQBOT_HELP = """\
QQBot 可用指令:
  /cd [路径]      切换工作目录（支持相对/绝对路径，.. 返回上级）
  /cd --reset     重置为默认工作目录
  /ls [路径]      列出目录内容（一级，可选子路径）
  /clear          清除对话历史
  /restart        重启服务（使配置更改生效）
  /help           显示此帮助"""


def _load_global_agent_config() -> AgentConfig | None:
    """从 agent-core.config.json 加载基础 AgentConfig，复用 CLI 的加载逻辑。"""
    from agent_core.cli._config import _find_config_file, _load_config  # noqa: PLC0415
    try:
        return _load_config()
    except SystemExit:
        return None


# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------


class QQBotChannel:
    """QQ 机器人信道调度器。"""

    def __init__(self, config: QQBotConfig, config_path: Path | None = None) -> None:
        self._config = config
        self._config_path = config_path
        self._client: QQBotClient | None = None

        base_agent_config = _load_global_agent_config()
        self._agent_config = config.to_agent_config(base_agent_config)
        from agent_core.bridge.agent_bridge import AgentBridge
        self._bridge = AgentBridge(
            self._agent_config,
            system_prompt=config.system_prompt,
        )
        # Register checkpoint callbacks
        self._bridge.on_session_created(self._on_qqbot_session_created)
        self._bridge.on_session_evicted(self._on_qqbot_session_evicted)

    # ------------------------------------------------------------------
    # Checkpoint (v1)
    # ------------------------------------------------------------------

    @staticmethod
    def _checkpoint_key(bridge_sid: str) -> str:
        """将 bridge session_id 转换为安全的文件名 key。"""
        return bridge_sid.replace(":", "_").replace("\\", "_").replace("/", "_")

    async def _on_qqbot_session_created(
        self, sid: str, session: "SessionState",
    ) -> None:
        """从 JSONL 和 checkpoint 恢复 QQBot session 的完整状态。

        1. JSONL → 恢复 messages、_written_count、_compaction_count
        2. checkpoint → 恢复 workspace_override、permission_rules、时间戳
        3. 挂载 JSONL writer，使后续 flush_new_messages 能增量写入磁盘
        """
        ck_key = self._checkpoint_key(sid)
        ws_root = self._agent_config.workspace_root

        # 1) 先恢复 checkpoint 元数据（workspace_override、permission_rules 等）
        from agent_core.persistence.session import load_session, load_checkpoint
        ckpt = await load_checkpoint(
            ck_key, workspace_root=ws_root,
        )
        if ckpt is not None:
            session.restore_checkpoint(ckpt)

        # 2) 从 JSONL 恢复对话历史 — 但当 checkpoint 显示 session 已被 /clear
        #    清空（message_count == 0）时跳过，避免旧消息"复活"
        if ckpt is None or int(ckpt.get("message_count", -1)) > 0:
            loaded = await load_session(
                ck_key, workspace_root=ws_root,
            )
            if loaded is not None:
                session.messages = loaded.messages
                session._written_count = loaded._written_count
                session._compaction_count = loaded._compaction_count

        # 3) 挂载 JSONL writer — 关键：没有这一步，flush_new_messages 全程是 no-op，
        #    新消息永远不会落盘，导致 checkpoint.message_count 远大于 JSONL 实际条目数
        from agent_core.persistence.path import compute_project_hash, session_jsonl_path
        ph = compute_project_hash(ws_root)
        jsonl_path = session_jsonl_path(ph, ck_key)
        await session.begin_persistence(jsonl_path)

    async def _on_qqbot_session_evicted(
        self, sid: str, session: "SessionState",
    ) -> None:
        """QQBot session 超时/清理前保存 checkpoint + JSONL。

        顺序：先关 JSONL（flush 剩余消息到磁盘），再写 checkpoint。
        这样 checkpoint 的 written_count 与 JSONL 实际条目数一致。
        """
        if not session.messages:
            return  # 空 session 不存档

        from agent_core.persistence.path import (
            compute_project_hash,
            session_jsonl_path,
        )
        from agent_core.persistence.session import save_checkpoint

        ck_key = self._checkpoint_key(sid)
        ph = compute_project_hash(self._agent_config.workspace_root)
        jsonl_path = session_jsonl_path(ph, ck_key)

        # /clear 后 _written_count 回到 0，旧 JSONL 数据已过时；
        # 删除它避免旧消息在下次 resume 时复活
        if session._written_count == 0 and os.path.exists(jsonl_path):
            os.unlink(jsonl_path)

        # 1) 先关闭 JSONL writer，flush 剩余消息到磁盘
        await session.end_persistence()

        # 2) 再保存 checkpoint（此时 JSONL 已完整，written_count 准确）
        await save_checkpoint(
            session,
            checkpoint_key=ck_key,
            workspace_root=self._agent_config.workspace_root,
        )

    async def _save_workspace_checkpoint(
        self, user_id: str, channel_id: str, session: "SessionState",
    ) -> None:
        """/cd 或 /cd --reset 后即时保存 workspace_override 到 checkpoint。"""
        from agent_core.persistence.session import save_checkpoint
        bridge_sid = self._bridge.session_id(user_id, channel_id)
        try:
            await save_checkpoint(
                session,
                checkpoint_key=self._checkpoint_key(bridge_sid),
                workspace_root=self._agent_config.workspace_root,
            )
        except Exception:
            logger.exception("Failed to save checkpoint after /cd")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动 botpy WebSocket 连接。"""
        if not self._config.app_id or not self._config.app_secret:
            raise ValueError("QQBotConfig missing app_id or app_secret")

        logger.info(f"starting QQ bot (app_id={self._config.app_id[:8]}...)")

        # botpy 内部用 asyncio.run() 管理自己的 event loop。
        # Client 必须在子线程内创建，这样 asyncio.get_event_loop()
        # 才能拿到正确的 loop（botpy 构造时内部会调 get_event_loop）。
        def _run_bot():
            import asyncio as _asyncio
            _loop = _asyncio.new_event_loop()
            _asyncio.set_event_loop(_loop)
            client = QQBotClient(self)
            self._client = client
            _loop.run_until_complete(client.start(
                self._config.app_id, self._config.app_secret
            ))

        import asyncio
        await asyncio.to_thread(_run_bot)

    async def stop(self) -> None:
        pass  # botpy 内部管理连接生命周期

    # ------------------------------------------------------------------
    # 消息处理
    # ------------------------------------------------------------------

    async def handle_message(
        self, user_id: str, channel_id: str, content: str, msg_type: str,
    ) -> None:
        """处理一条用户消息 → Agent 推理 → 回复。

        以 ``/`` 开头的消息在进入 Agent 之前被拦截为 shell 指令。
        """
        stripped = content.strip()

        if stripped.startswith("/"):
            await self._dispatch_command(user_id, channel_id, stripped)
            return

        try:
            replies = await self._bridge.process(
                user_id, channel_id, content, msg_type
            )
        except Exception:
            logger.exception(f"AgentBridge.process error for user {user_id}")
            replies = ["处理出错，请稍后再试。"]

        api = self._client.api if self._client is not None else None
        for text in replies:
            if api is None:
                continue
            if channel_id == "c2c":
                await api.post_c2c_message(openid=user_id, msg_type=0, content=text)
            else:
                await api.post_group_message(group_openid=channel_id, msg_type=0, content=text)

    # ------------------------------------------------------------------
    # 指令路由
    # ------------------------------------------------------------------

    async def _dispatch_command(
        self, user_id: str, channel_id: str, content: str,
    ) -> None:
        """将 shell 指令分发到对应处理器。"""
        if content.startswith("/cd"):
            await self._handle_cd_command(user_id, channel_id, content)
        elif content.startswith("/ls"):
            await self._handle_ls_command(user_id, channel_id, content)
        elif content == "/clear":
            await self._handle_clear_command(user_id, channel_id)
        elif content == "/restart":
            await self._handle_restart_command(user_id, channel_id)
        elif content == "/help":
            await self._handle_help_command(user_id, channel_id)
        else:
            cmd = content.split()[0]
            await self._send_message(
                user_id, channel_id,
                f"未知指令: {cmd}。输入 /help 查看可用指令。",
            )

    # ------------------------------------------------------------------
    # 路径解析
    # ------------------------------------------------------------------

    def _resolve_path(
        self, user_id: str, channel_id: str, arg: str,
    ) -> tuple[str | None, str | None]:
        """解析相对于当前 effective workspace 的路径。

        Returns:
            (resolved_path, error_msg)

            - error_msg 非 None → 路径不合法，resolved_path 为 None
            - arg 为空字符串 → 返回 (effective, None)，调用方决定行为
        """
        session = self._bridge.get_session(user_id, channel_id)
        effective = (
            session.workspace_override
            or self._agent_config.workspace_root
            or os.getcwd()
        )

        if not arg:
            return effective, None

        if os.path.isabs(arg):
            target = os.path.abspath(arg)
        else:
            if effective is None:
                return None, "无法解析相对路径：当前工作目录未设置"
            target = os.path.abspath(os.path.join(effective, arg))

        # 安全检查：不能超出原始 workspace_root
        original_root = self._agent_config.workspace_root
        if original_root is not None:
            root_norm = os.path.normpath(original_root)
            target_norm = os.path.normpath(target)
            if not (target_norm == root_norm
                    or target_norm.startswith(root_norm + os.sep)):
                return None, f"无法切换到工作空间外的目录: {target}"

        if not os.path.isdir(target):
            return None, f"目录不存在: {target}"

        return target, None

    # ------------------------------------------------------------------
    # /cd
    # ------------------------------------------------------------------

    async def _handle_cd_command(
        self, user_id: str, channel_id: str, content: str,
    ) -> None:
        """处理 /cd 指令：查看、切换或重置工作目录。"""
        if not self._config.allow_shell_commands:
            await self._send_message(
                user_id, channel_id, "Shell 指令功能未启用。",
            )
            return

        session = self._bridge.get_session(user_id, channel_id)
        arg = content[len("/cd"):].strip()

        # 无参：显示当前
        if not arg:
            effective = (
                session.workspace_override
                or self._agent_config.workspace_root
                or os.getcwd()
            )
            suffix = ""
            if (session.workspace_override is None
                    and self._agent_config.workspace_root is None):
                suffix = " (默认)"
            await self._send_message(
                user_id, channel_id,
                f"当前工作目录: {effective}{suffix}",
            )
            return

        # reset
        if arg == "--reset":
            session.workspace_override = None
            session.clear_history()
            await self._save_workspace_checkpoint(user_id, channel_id, session)
            default = self._agent_config.workspace_root or os.getcwd()
            await self._send_message(
                user_id, channel_id,
                f"工作目录已重置为默认: {default}\n对话历史已重置。",
            )
            return

        # 切换
        new_path, err = self._resolve_path(user_id, channel_id, arg)
        if err is not None:
            await self._send_message(user_id, channel_id, err)
            return

        session.workspace_override = new_path
        session.clear_history()
        await self._save_workspace_checkpoint(user_id, channel_id, session)
        await self._send_message(
            user_id, channel_id,
            f"工作目录已切换至: {new_path}\n对话历史已重置。",
        )

    # ------------------------------------------------------------------
    # /ls
    # ------------------------------------------------------------------

    async def _handle_ls_command(
        self, user_id: str, channel_id: str, content: str,
    ) -> None:
        """处理 /ls 指令：列出当前 effective workspace 的一级目录。"""
        if not self._config.allow_shell_commands:
            await self._send_message(
                user_id, channel_id, "Shell 指令功能未启用。",
            )
            return

        arg = content[len("/ls"):].strip()
        target, err = self._resolve_path(user_id, channel_id, arg)
        if err is not None:
            await self._send_message(user_id, channel_id, err)
            return

        try:
            entries = sorted(
                os.listdir(target),
                key=lambda n: (not os.path.isdir(os.path.join(target, n)),
                               n.lower()),
            )
        except OSError as exc:
            await self._send_message(
                user_id, channel_id, f"无法读取目录: {exc}",
            )
            return

        if not entries:
            await self._send_message(
                user_id, channel_id, f"{target}\n(空目录)",
            )
            return

        # format into columns
        formatted: list[str] = []
        for e in entries:
            full = os.path.join(target, e)
            if os.path.isdir(full):
                formatted.append(f"📁 {e}/")
            else:
                formatted.append(f"📄 {e}")

        col_width = max(len(f) for f in formatted) + 2
        per_row = max(1, 60 // col_width)  # ~30 中文字宽 ≈ 60 半角
        lines = [target]
        limit = min(len(formatted), 50)
        for i in range(0, limit, per_row):
            row = "".join(
                f.ljust(col_width) for f in formatted[i:i + per_row]
            )
            lines.append(row.rstrip())
        if len(formatted) > 50:
            lines.append(f"... 及另外 {len(formatted) - 50} 个条目")

        await self._send_message(user_id, channel_id, "\n".join(lines))

    # ------------------------------------------------------------------
    # /clear
    # ------------------------------------------------------------------

    async def _handle_clear_command(
        self, user_id: str, channel_id: str,
    ) -> None:
        """处理 /clear 指令：清除对话历史并即时保存 checkpoint，
        防止旧消息在 session 重建时从 JSONL 复活。"""
        session = self._bridge.get_session(user_id, channel_id)
        session.clear_history()
        await self._save_workspace_checkpoint(user_id, channel_id, session)
        await self._send_message(user_id, channel_id, "对话历史已清除。")

    # ------------------------------------------------------------------
    # /restart
    # ------------------------------------------------------------------

    async def _handle_restart_command(
        self, user_id: str, channel_id: str,
    ) -> None:
        """处理 /restart 指令：以 os.execv 替换当前进程来重启。

        os.execv 会用新进程替换当前进程，保留 pid 和命令行参数。
        所有 WebSocket 连接由 OS 在新进程中重建，配置文件的更改会生效。
        如有外部 supervisor（systemd/pm2/shell loop）也能协同工作。
        """
        logger.info("QQBot /restart from user=%s channel=%s", user_id, channel_id)
        await self._send_message(user_id, channel_id, "正在重启服务，请稍候...")

        # 给消息发送和 IO 刷新留一点时间
        import asyncio
        await asyncio.sleep(1.5)

        # 通过环境变量告知新进程：启动后向此用户发送"重启完成"通知
        import json
        os.environ["QQ_BOT_RESTART_NOTIFY"] = json.dumps({
            "user_id": user_id,
            "channel_id": channel_id,
        })

        # 重建启动参数：过滤原始 --config，替换为解析后的绝对路径。
        # 用 sys.argv[0]（原始入口，如 venv/Scripts/agent-core-qqbot.exe）启动，
        # 而非 sys.executable，确保 venv 环境正确加载。
        filtered_args: list[str] = []
        skip_next = False
        for arg in sys.argv[1:]:
            if skip_next:
                skip_next = False
                continue
            if arg == "--config":
                skip_next = True
                continue
            filtered_args.append(arg)

        restart_argv = [sys.argv[0], *filtered_args]
        if self._config_path is not None:
            restart_argv.extend(["--config", str(self._config_path)])

        os.execv(sys.argv[0], restart_argv)

    # ------------------------------------------------------------------
    # /help
    # ------------------------------------------------------------------

    async def _handle_help_command(
        self, user_id: str, channel_id: str,
    ) -> None:
        """处理 /help 指令：显示可用指令列表。"""
        await self._send_message(user_id, channel_id, _QQBOT_HELP)

    # ------------------------------------------------------------------
    # 发送消息
    # ------------------------------------------------------------------

    async def _send_message(
        self, user_id: str, channel_id: str, content: str,
    ) -> None:
        """发送一条文本消息到 C2C 或群聊。"""
        api = self._client.api if self._client is not None else None
        if api is None:
            return
        if channel_id == "c2c":
            await api.post_c2c_message(openid=user_id, msg_type=0, content=content)
        else:
            await api.post_group_message(group_openid=channel_id, msg_type=0, content=content)
