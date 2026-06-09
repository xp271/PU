from __future__ import annotations

import pydantic
import pytest

from agent_core.models import AgentConfig, ClaudeModel


@pytest.mark.model
class TestClaudeModel:
    def test_known_values_contains_all_members(self) -> None:
        values = ClaudeModel.known_values()
        assert "claude-sonnet-4-5" in values
        assert "claude-opus-4-7" in values
        assert "claude-haiku-4-5" in values

    def test_member_value_matches_string(self) -> None:
        assert ClaudeModel.SONNET_4_5.value == "claude-sonnet-4-5"
        assert ClaudeModel.OPUS_4_7.value == "claude-opus-4-7"
        assert ClaudeModel.HAIKU_4_5.value == "claude-haiku-4-5"


@pytest.mark.model
class TestAgentConfig:
    def test_default_config_has_expected_values(self) -> None:
        config = AgentConfig()
        assert config.model == "anthropic/claude-sonnet-4-5"
        assert config.max_turns == 20
        assert config.permission_mode == "ask"
        assert config.tools == ["*"]
        assert config.mcp_servers == {}
        assert config.skills_dir is None
        assert config.hooks == []
        assert config.allow_unknown_model is False

    def test_custom_config_accepts_valid_values(self) -> None:
        mcp = {"test_server": {"command": "echo", "args": []}}
        config = AgentConfig(
            model="claude-haiku-4-5",
            max_turns=30,
            permission_mode="bypassPermissions",
            tools=["Read", "Glob"],
            mcp_servers=mcp,
            skills_dir="/tmp/skills",
        )
        assert config.model == "claude-haiku-4-5"
        assert config.max_turns == 30
        assert config.permission_mode == "bypassPermissions"
        assert config.tools == ["Read", "Glob"]
        assert config.mcp_servers == mcp
        assert config.skills_dir == "/tmp/skills"

    def test_invalid_model_raises_validation_error(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse model"):
            AgentConfig(model="unknown-model-99")

    def test_allow_unknown_model_suppresses_validation_error(self) -> None:
        with pytest.warns(UserWarning, match="could not be parsed"):
            config = AgentConfig(
                model="custom-proxy-model-v2",
                allow_unknown_model=True,
            )
        assert config.model == "custom-proxy-model-v2"

    def test_allow_unknown_model_still_requires_explicit_opt_in(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse model"):
            AgentConfig(model="unlisted-model")

    def test_invalid_permission_mode_raises_validation_error(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            AgentConfig(permission_mode="invalid_mode")  # type: ignore[arg-type]

    def test_empty_tools_raises_validation_error(self) -> None:
        with pytest.raises(ValueError, match="tools list must not be empty"):
            AgentConfig(tools=[])

    def test_max_turns_zero_raises_validation_error(self) -> None:
        with pytest.raises(ValueError, match="max_turns must be >= 1"):
            AgentConfig(max_turns=0)

    def test_max_turns_exceeds_limit_raises_validation_error(self) -> None:
        with pytest.raises(ValueError, match="max_turns must be <= 500"):
            AgentConfig(max_turns=501)

    def test_all_permission_modes_accepted(self) -> None:
        for mode in ("bypassPermissions", "acceptEdits", "ask"):
            config = AgentConfig(permission_mode=mode)  # type: ignore[arg-type]
            assert config.permission_mode == mode
