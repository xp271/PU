"""QQ 消息发送封装 — botpy API 的薄封装。

支持文本、Markdown、图片等多种消息类型。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import botpy

logger = logging.getLogger("agent_core.qq_bot.sender")


class MessageSender:
    """QQ 消息发送封装。

    封装 botpy 的 HTTP API 调用，统一处理单聊/群聊的消息发送。
    """

    def __init__(self, api: "botpy.API") -> None:
        self._api = api

    async def send_c2c_text(self, openid: str, content: str) -> None:
        """发送单聊文本消息（msg_type=0）。"""
        try:
            await self._api.post_c2c_message(
                openid=openid,
                msg_type=0,
                content=content,
            )
        except Exception:
            logger.exception(f"send_c2c_text failed for user {openid}")

    async def send_group_text(self, group_openid: str, content: str) -> None:
        """发送群聊文本消息（msg_type=0）。"""
        try:
            await self._api.post_group_message(
                group_openid=group_openid,
                msg_type=0,
                content=content,
            )
        except Exception:
            logger.exception(f"send_group_text failed for group {group_openid}")

    async def send_c2c_markdown(
        self, openid: str, template_id: str, params: dict[str, str]
    ) -> None:
        """发送单聊 Markdown 模板消息（msg_type=2）。

        QQ 支持 Markdown 模板消息，需要先在开放平台配置模板。
        """
        try:
            await self._api.post_c2c_message(
                openid=openid,
                msg_type=2,
                markdown={"custom_template_id": template_id, "params": params},
            )
        except Exception:
            logger.exception(f"send_c2c_markdown failed for user {openid}")

    async def send_group_markdown(
        self, group_openid: str, template_id: str, params: dict[str, str]
    ) -> None:
        """发送群聊 Markdown 模板消息（msg_type=2）。"""
        try:
            await self._api.post_group_message(
                group_openid=group_openid,
                msg_type=2,
                markdown={"custom_template_id": template_id, "params": params},
            )
        except Exception:
            logger.exception(f"send_group_markdown failed for group {group_openid}")
