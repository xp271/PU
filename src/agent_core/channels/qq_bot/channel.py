"""QQ 机器人信道调度器。

比 WechatKFChannel 简洁得多——不需要 crypto、token、sync、dedup。
botpy 封装了 WebSocket 连接和 AccessToken 管理。
"""

from __future__ import annotations

import logging
from typing import Any

from agent_core.models import AgentConfig

from .client import QQBotClient
from .sender import MessageSender
from .types import QQBotConfig

logger = logging.getLogger("agent_core.qq_bot.channel")


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

    def __init__(self, config: QQBotConfig) -> None:
        self._config = config
        self._client: QQBotClient | None = None
        self._sender: MessageSender | None = None

        base_agent_config = _load_global_agent_config()
        from agent_core.bridge.agent_bridge import AgentBridge
        self._bridge = AgentBridge(
            config.to_agent_config(base_agent_config),
            system_prompt=config.system_prompt,
        )

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
            self._sender = MessageSender(client.api)
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
        """处理一条用户消息 → Agent 推理 → 回复。"""
        try:
            replies = await self._bridge.process(
                user_id, channel_id, content, msg_type
            )
        except Exception:
            logger.exception(f"AgentBridge.process error for user {user_id}")
            replies = ["处理出错，请稍后再试。"]

        sender = self._sender
        if sender is None and self._client is not None:
            sender = MessageSender(self._client.api)
        for text in replies:
            if sender is None:
                continue
            if channel_id == "c2c":
                await sender.send_c2c_text(user_id, text)
            else:
                await sender.send_group_text(channel_id, text)
