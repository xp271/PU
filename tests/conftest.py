import pytest

from agent_core.models import AgentConfig


@pytest.fixture
def default_config() -> AgentConfig:
    return AgentConfig()


@pytest.fixture
def custom_config() -> AgentConfig:
    return AgentConfig(
        model="claude-haiku-4-5",
        max_turns=10,
        permission_mode="acceptEdits",
        tools=["*"],
    )
