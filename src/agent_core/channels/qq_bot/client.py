"""QQ 机器人 WebSocket 客户端 — botpy 封装。

注册事件回调，将消息转发给 QQBotChannel。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import botpy

if TYPE_CHECKING:
    from .channel import QQBotChannel

logger = logging.getLogger("agent_core.qq_bot.client")


class QQBotClient(botpy.Client):
    """QQ 机器人 WebSocket 客户端。

    botpy 自动管理 WebSocket 连接、心跳、重连和 AccessToken 刷新。
    事件回调在 botpy 的 asyncio event loop 中执行。
    """

    def __init__(self, channel: "QQBotChannel"):
        from pathlib import Path as _Path
        intents = botpy.Intents(public_messages=True)
        # botpy 默认日志写到 {cwd}/botpy.log，重定向到项目 logs/ 目录
        _log_dir = _Path(__file__).resolve().parent.parent.parent.parent / "logs"
        _log_dir.mkdir(exist_ok=True)
        log_config: dict = {
            "version": 1,
            "handlers": {
                "file": {
                    "class": "logging.handlers.RotatingFileHandler",
                    "formatter": "default",
                    "filename": str(_log_dir / "botpy.log"),
                    "maxBytes": 10 * 1024 * 1024,
                    "backupCount": 3,
                    "encoding": "utf-8",
                },
            },
            "formatters": {
                "default": {
                    "format": "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                },
            },
            "loggers": {
                "botpy": {"level": "INFO", "handlers": ["file"]},
            },
        }
        super().__init__(intents=intents, timeout=20, log_config=log_config)
        self._channel = channel

    # ------------------------------------------------------------------
    # 事件回调
    # ------------------------------------------------------------------

    async def on_c2c_message_create(self, message: botpy.message.C2CMessage) -> None:
        """单聊消息。"""
        await self._channel.handle_message(
            user_id=message.author.user_openid,
            channel_id="c2c",
            content=message.content or "",
            msg_type="text",
        )

    async def on_group_at_message_create(self, message: botpy.message.GroupMessage) -> None:
        """群聊 @ 消息。"""
        await self._channel.handle_message(
            user_id=message.author.member_openid,
            channel_id=message.group_openid,
            content=message.content or "",
            msg_type="text",
        )
