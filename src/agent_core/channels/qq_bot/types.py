"""QQ 机器人配置模型 — QQBotConfig。

所有配置从 ``agent-core.config.json`` 的 ``qq_bot`` 嵌套字段加载，
支持环境变量覆盖（``QQ_BOT_*`` 前缀）。
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field

from agent_core.models import AgentConfig, PermissionMode


class QQBotConfig(BaseModel):
    """QQ 机器人接入配置。

    比微信客服简洁得多——无需 crypto、token、sync、dedup。
    botpy 内置管理 AccessToken 和 WebSocket 连接。
    """

    app_id: str = Field(
        default_factory=lambda: os.environ.get("QQ_BOT_APP_ID", ""),
        description="机器人 AppID",
    )
    app_secret: str = Field(
        default_factory=lambda: os.environ.get("QQ_BOT_APP_SECRET", ""),
        description="机器人 AppSecret（botpy 称为 bot_token）",
    )

    # Agent 配置 overlay
    model: str = Field(default="anthropic/claude-sonnet-4-5")
    max_turns: int = Field(default=20, ge=1, le=500)
    permission_mode: PermissionMode = Field(
        default="bypassPermissions",
        description="QQ 机器人无交互终端，必须 bypass",
    )
    system_prompt: str | None = Field(default=None)
    tools: list[str] = Field(default_factory=lambda: ["*"])
    workspace_root: str | None = Field(default=None)
    allow_workspace_switch: bool = Field(
        default=True,
        description="允许 QQBot 用户在对话中通过 /workspace 切换工作目录",
    )
    api_keys: dict[str, str] = Field(default_factory=dict)
    base_urls: dict[str, str] = Field(default_factory=dict)

    def to_agent_config(self, base: AgentConfig | None = None) -> AgentConfig:
        """将 QQ 机器人相关 Agent 字段与基础配置合并。"""
        cfg = base.model_copy(deep=True) if base else AgentConfig()
        cfg.model = self.model
        cfg.max_turns = self.max_turns
        cfg.permission_mode = self.permission_mode
        cfg.tools = self.tools
        if self.workspace_root is not None:
            cfg.workspace_root = self.workspace_root
        for provider, key in self.api_keys.items():
            cfg.api_keys[provider] = key
        for provider, url in self.base_urls.items():
            cfg.base_urls[provider] = url
        return cfg
