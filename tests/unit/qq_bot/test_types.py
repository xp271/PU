"""Tests for qq_bot/types.py — QQBotConfig."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_core.channels.qq_bot.types import QQBotConfig
from agent_core.models import AgentConfig, CompressionConfig


class TestQQBotConfig:
    """QQBotConfig 验证测试。"""

    def test_minimal_config(self):
        """最小配置正确创建。"""
        cfg = QQBotConfig(app_id="123", app_secret="abc")
        assert cfg.app_id == "123"
        assert cfg.app_secret == "abc"
        assert cfg.permission_mode == "bypassPermissions"

    def test_missing_app_id_uses_default(self):
        """app_id 缺失时使用空字符串默认值。"""
        cfg = QQBotConfig(app_secret="abc")
        assert cfg.app_id == ""

    def test_defaults(self):
        """验证默认值。"""
        cfg = QQBotConfig(app_id="x", app_secret="y")
        assert cfg.model is None
        assert cfg.max_turns == 20
        assert cfg.tools == ["*"]
        assert cfg.permission_mode == "bypassPermissions"

    def test_to_agent_config_overlay(self):
        """to_agent_config 覆盖指定字段。"""
        wc = QQBotConfig(
            app_id="x", app_secret="y",
            model="deepseek/deepseek-v4-pro",
            max_turns=50,
            tools=["file_read"],
        )
        base = AgentConfig()
        result = wc.to_agent_config(base)
        assert result.model == "deepseek/deepseek-v4-pro"
        assert result.max_turns == 50
        assert result.tools == ["file_read"]
        assert result.permission_mode == "bypassPermissions"

    def test_to_agent_config_keep_other_fields(self):
        """未指定的字段保持 base 值。"""
        wc = QQBotConfig(app_id="x", app_secret="y")
        base = AgentConfig(max_concurrency=10, context_size=32000)
        result = wc.to_agent_config(base)
        assert result.max_concurrency == 10
        assert result.context_size == 32000

    def test_to_agent_config_api_keys_merge(self):
        """api_keys merge 而非替换。"""
        wc = QQBotConfig(app_id="x", app_secret="y", api_keys={"deepseek": "ds-key"})
        base = AgentConfig(api_keys={"anthropic": "ant-key"})
        result = wc.to_agent_config(base)
        assert result.api_keys == {"anthropic": "ant-key", "deepseek": "ds-key"}

    def test_to_agent_config_none_base(self):
        """base=None 使用默认值。"""
        wc = QQBotConfig(app_id="x", app_secret="y", model="openai/gpt-4o")
        result = wc.to_agent_config(None)
        assert result.model == "openai/gpt-4o"
