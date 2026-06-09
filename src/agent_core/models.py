from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Shared type aliases (single source of truth for repeated literal unions)
# ---------------------------------------------------------------------------

PermissionMode = Literal["bypassPermissions", "acceptEdits", "ask"]
StopReason = Literal["end_turn", "max_turns", "permission_denied", "error"]


# ---------------------------------------------------------------------------
# ClaudeModel — legacy Anthropic-only enum (retained for backward compatibility)
# ---------------------------------------------------------------------------

class ClaudeModel(str, Enum):
    SONNET_4_5 = "claude-sonnet-4-5"
    OPUS_4_7 = "claude-opus-4-7"
    HAIKU_4_5 = "claude-haiku-4-5"

    @classmethod
    def known_values(cls) -> set[str]:
        return {m.value for m in cls}


# ---------------------------------------------------------------------------
# ModelProvider & ModelId — provider-agnostic model identification
# ---------------------------------------------------------------------------

class ModelProvider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"

    @classmethod
    def known_providers(cls) -> set[str]:
        return {p.value for p in cls}

    @classmethod
    def known_models(cls) -> dict["ModelProvider", list[str]]:
        return {
            cls.ANTHROPIC: [
                "claude-sonnet-4-5",
                "claude-opus-4-7",
                "claude-haiku-4-5",
            ],
            cls.OPENAI: [
                "gpt-4o",
                "gpt-4.1",
                "gpt-4.1-mini",
                "o4-mini",
            ],
        }


@dataclass(frozen=True, order=True)
class ModelId:
    provider: ModelProvider
    name: str

    def __str__(self) -> str:
        return f"{self.provider.value}/{self.name}"

    def __repr__(self) -> str:
        return f"ModelId({self!s})"

    @classmethod
    def parse(cls, raw: str) -> "ModelId":
        """
        Parse a model identifier string.

        Supported formats:
          "anthropic/claude-sonnet-4-5"  -> ModelId(ANTHROPIC, "claude-sonnet-4-5")
          "openai/gpt-4o"               -> ModelId(OPENAI, "gpt-4o")
          "claude-sonnet-4-5"           -> ModelId(ANTHROPIC, "claude-sonnet-4-5")  # backward compat
          "gpt-4o"                       -> ValueError (non-Anthropic model ambiguous without prefix)
        """
        raw = raw.strip()
        if "/" in raw:
            provider_str, _, name = raw.partition("/")
            try:
                provider = ModelProvider(provider_str)
            except ValueError:
                raise ValueError(
                    f"Unknown provider '{provider_str}' in model '{raw}'. "
                    f"Known providers: {sorted(ModelProvider.known_providers())}"
                ) from None
            return cls(provider=provider, name=name)

        # No prefix: check if known Anthropic model (backward compatibility)
        for m in ModelProvider.known_models().get(ModelProvider.ANTHROPIC, []):
            if m == raw:
                return cls(provider=ModelProvider.ANTHROPIC, name=raw)

        raise ValueError(
            f"Cannot parse model '{raw}': no provider prefix and not a known "
            f"Anthropic model. Use format 'provider/model-name', e.g. 'openai/gpt-4o'."
        )

    @classmethod
    def known_values(cls) -> set[str]:
        """All known ``'provider/model'`` strings plus unprefixed Anthropic names."""
        result: set[str] = set()
        for provider, models in ModelProvider.known_models().items():
            for m in models:
                result.add(f"{provider.value}/{m}")
                # Also include unprefixed names for Anthropic (backward compat)
                if provider == ModelProvider.ANTHROPIC:
                    result.add(m)
        return result


# ---------------------------------------------------------------------------
# AgentConfig
# ---------------------------------------------------------------------------

class AgentConfig(BaseModel):
    model: str = Field(
        default_factory=lambda: str(ModelId(ModelProvider.ANTHROPIC, "claude-sonnet-4-5")),
        description="Model identifier, e.g. 'anthropic/claude-sonnet-4-5' or 'openai/gpt-4o'",
    )
    max_turns: int = Field(default=20, description="Maximum number of turn iterations")
    permission_mode: PermissionMode = Field(
        default="ask",
        description="Default permission handling mode",
    )
    tools: list[str] = Field(
        default_factory=lambda: ["*"],
        description="List of tool names available to the agent",
    )
    mcp_servers: dict[str, Any] = Field(
        default_factory=dict,
        description="MCP server configurations keyed by server name",
    )
    skills_dir: str | None = Field(
        default=None,
        description="Directory path for skill definitions",
    )
    hooks: list[Any] = Field(
        default_factory=list,
        description="Hook instances registered at agent bootstrap",
    )
    allow_unknown_model: bool = Field(
        default=False,
        description=(
            "If True, accept model identifiers outside the built-in known lists "
            "(e.g. when routing through a proxy or self-hosted base_url). "
            "Unknown names are still warned about, not rejected."
        ),
    )

    # -- New fields for multi-provider support --

    api_keys: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Provider -> API key mapping. Falls back to env vars "
            "(ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.)"
        ),
    )
    base_urls: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Provider -> custom base URL. Only overrides the same "
            "provider's default API endpoint."
        ),
    )

    # -- Property helpers --

    @property
    def model_id(self) -> ModelId:
        """Parse the model string into a structured ModelId."""
        return ModelId.parse(self.model)

    def get_api_key(self, provider: ModelProvider) -> str | None:
        """Resolve API key: AgentConfig.api_keys -> environment variable."""
        env_var_map = {
            ModelProvider.ANTHROPIC: "ANTHROPIC_API_KEY",
            ModelProvider.OPENAI: "OPENAI_API_KEY",
        }
        key = self.api_keys.get(provider.value)
        if key:
            return key
        import os

        var = env_var_map.get(provider)
        if var:
            return os.environ.get(var)
        return None

    def get_base_url(self, provider: ModelProvider) -> str | None:
        return self.base_urls.get(provider.value)

    # -- Validators --

    @model_validator(mode="after")
    def validate_model(self) -> "AgentConfig":
        model_str = self.model.strip()
        try:
            ModelId.parse(model_str)
        except ValueError as e:
            if self.allow_unknown_model:
                import warnings

                warnings.warn(
                    f"Model '{model_str}' could not be parsed as a known provider/model: {e}",
                    stacklevel=2,
                )
                return self
            raise

        if model_str not in ModelId.known_values():
            known = ", ".join(sorted(ModelId.known_values()))
            if self.allow_unknown_model:
                import warnings

                warnings.warn(
                    f"Model '{model_str}' is not a known built-in model ({known}); "
                    "proceeding because allow_unknown_model=True.",
                    stacklevel=2,
                )
                return self
            raise ValueError(
                f"Unknown model '{model_str}'. Known models: {known}. "
                "Set allow_unknown_model=True to use an unlisted identifier."
            )
        return self

    @field_validator("tools")
    @classmethod
    def validate_tools(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("tools list must not be empty")
        return v

    @field_validator("max_turns")
    @classmethod
    def validate_max_turns(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_turns must be >= 1")
        if v > 500:
            raise ValueError("max_turns must be <= 500")
        return v


# ---------------------------------------------------------------------------
# AgentResult
# ---------------------------------------------------------------------------

class AgentResult(BaseModel):
    content: str
    total_tokens: int
    tool_uses: int
    stop_reason: StopReason
    error_detail: str | None = None


# ---------------------------------------------------------------------------
# Streaming message variants (discriminated union on `type`)
# ---------------------------------------------------------------------------

class AssistantText(BaseModel):
    type: Literal["assistant_text"] = "assistant_text"
    content: str


class ToolUse(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    tool_use_id: str
    tool_name: str
    tool_input: dict[str, Any]


class ToolResult(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    tool_name: str
    content: Any
    is_error: bool = False


class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    message: str
    error_type: str | None = None


class DoneMessage(BaseModel):
    """Terminal message emitted once at the end of a stream.

    Carries the authoritative stop reason and run-level token accounting so
    that run() can aggregate without inferring them from stream termination.
    """

    type: Literal["done"] = "done"
    stop_reason: StopReason
    total_tokens: int = 0
    error_detail: str | None = None


AgentMessage = Annotated[
    AssistantText | ToolUse | ToolResult | ErrorMessage | DoneMessage,
    Field(discriminator="type"),
]
