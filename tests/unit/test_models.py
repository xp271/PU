from __future__ import annotations

import pydantic
import pytest

from agent_core.models import AgentConfig


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
        assert "test_server" in config.mcp_servers
        assert config.mcp_servers["test_server"].command == "echo"
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

    def test_get_api_key_from_config(self) -> None:
        config = AgentConfig(api_keys={"anthropic": "sk-config-key"})
        from agent_core.models import ModelProvider
        assert config.get_api_key(ModelProvider.ANTHROPIC) == "sk-config-key"

    def test_get_api_key_fallback_to_env(self) -> None:
        import os
        from agent_core.models import ModelProvider

        os.environ["ANTHROPIC_API_KEY"] = "sk-env-key"
        config = AgentConfig()
        assert config.get_api_key(ModelProvider.ANTHROPIC) == "sk-env-key"

    def test_get_api_key_returns_none_when_not_set(self) -> None:
        from agent_core.models import ModelProvider

        config = AgentConfig(api_keys={})
        # Clear env var to ensure clean state
        import os
        os.environ.pop("DEEPSEEK_API_KEY", None)
        assert config.get_api_key(ModelProvider.DEEPSEEK) is None

    def test_get_base_url_from_config(self) -> None:
        from agent_core.models import ModelProvider

        config = AgentConfig(base_urls={"openai": "https://custom.openai.com"})
        assert config.get_base_url(ModelProvider.OPENAI) == "https://custom.openai.com"

    def test_get_base_url_returns_none_when_not_set(self) -> None:
        from agent_core.models import ModelProvider

        config = AgentConfig()
        assert config.get_base_url(ModelProvider.ANTHROPIC) is None

    def test_max_concurrency_bounds(self) -> None:
        config = AgentConfig(max_concurrency=10)
        assert config.max_concurrency == 10

    def test_max_concurrency_default(self) -> None:
        config = AgentConfig()
        assert config.max_concurrency == 5

    def test_max_concurrency_out_of_bounds_low(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            AgentConfig(max_concurrency=0)

    def test_max_concurrency_out_of_bounds_high(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            AgentConfig(max_concurrency=21)


# ---------------------------------------------------------------------------
# ModelId
# ---------------------------------------------------------------------------

@pytest.mark.model
class TestModelId:
    def test_parse_with_provider_prefix(self) -> None:
        from agent_core.models import ModelId, ModelProvider

        mid = ModelId.parse("openai/gpt-4o")
        assert mid.provider == ModelProvider.OPENAI
        assert mid.name == "gpt-4o"

    def test_parse_unknown_provider_prefix_raises(self) -> None:
        from agent_core.models import ModelId

        with pytest.raises(ValueError, match="Unknown provider"):
            ModelId.parse("google/gemini")

    def test_parse_anthropic_no_prefix(self) -> None:
        from agent_core.models import ModelId, ModelProvider

        mid = ModelId.parse("claude-haiku-4-5")
        assert mid.provider == ModelProvider.ANTHROPIC
        assert mid.name == "claude-haiku-4-5"

    def test_parse_non_anthropic_no_prefix_raises(self) -> None:
        from agent_core.models import ModelId

        with pytest.raises(ValueError, match="Cannot parse model"):
            ModelId.parse("gpt-4o")

    def test_known_values_includes_all_expected(self) -> None:
        from agent_core.models import ModelId

        known = ModelId.known_values()
        assert "anthropic/claude-sonnet-4-5" in known
        assert "openai/gpt-4o" in known
        assert "deepseek/deepseek-v4-pro" in known
        assert "claude-haiku-4-5" in known  # unprefixed Anthropic

    def test_str_and_repr(self) -> None:
        from agent_core.models import ModelId, ModelProvider

        mid = ModelId(ModelProvider.DEEPSEEK, "deepseek-v4-flash")
        assert str(mid) == "deepseek/deepseek-v4-flash"
        assert "deepseek/deepseek-v4-flash" in repr(mid)

    def test_ordering_by_provider_then_name(self) -> None:
        from agent_core.models import ModelId, ModelProvider

        a = ModelId(ModelProvider.ANTHROPIC, "claude-haiku-4-5")
        b = ModelId(ModelProvider.ANTHROPIC, "claude-sonnet-4-5")
        c = ModelId(ModelProvider.OPENAI, "gpt-4o")
        assert a < b  # same provider, name order
        assert a < c  # ANTHROPIC < OPENAI alphabetically
