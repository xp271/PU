from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .logging.config import LoggingConfig


# ---------------------------------------------------------------------------
# Shared type aliases (single source of truth for repeated literal unions)
# ---------------------------------------------------------------------------

PermissionMode = Literal["bypassPermissions", "acceptEdits", "ask"]
StopReason = Literal["end_turn", "max_turns", "permission_denied", "error"]


# ---------------------------------------------------------------------------
# ModelProvider & ModelId — provider-agnostic model identification
# ---------------------------------------------------------------------------

class ModelProvider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    DEEPSEEK = "deepseek"
    LLAMACPP = "llamacpp"

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
            cls.DEEPSEEK: [
                "deepseek-v4-pro",
                "deepseek-v4-flash",
            ],
            cls.LLAMACPP: ["*"],
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
# CompressionConfig
# ---------------------------------------------------------------------------


class CompressionConfig(BaseModel):
    """Auto-compression configuration. All fields are optional with sensible defaults.

    Compression shares the same trigger threshold as hard truncation
    (context window × 0.8, defined by TokenBudget.truncation_threshold).
    When the threshold is crossed, compression is *always* preferred
    over truncation.  Truncation is only used when compression is
    disabled, has no old rounds to compress, or fails after all retries.
    """

    enabled: bool = Field(
        default=True,
        description="Enable auto-compression (when False, fall through to hard truncate directly)",
    )
    keep_recent_rounds: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Number of recent conversation rounds to keep uncompressed",
    )
    summary_max_tokens: int = Field(
        default=2048,
        ge=512,
        le=8192,
        description="Maximum tokens for the LLM summary response",
    )
    tool_result_preview_chars: int = Field(
        default=200,
        ge=50,
        le=500,
        description="Truncation preview character count for tool results in the compression prompt",
    )
    max_retries: int = Field(
        default=2,
        ge=0,
        le=5,
        description=(
            "Maximum retries when compression verification fails. "
            "0 means no retry — fail straight to hard truncation."
        ),
    )
    prompt_template: str | None = Field(
        default=None,
        description=(
            "Custom compression request prompt template. Must contain "
            "``{preview_chars}`` and ``{conversation_text}`` placeholders. "
            "When None (default), the built-in template is used."
        ),
    )


# ---------------------------------------------------------------------------
# SamplingConfig
# ---------------------------------------------------------------------------


class SamplingConfig(BaseModel):
    """Sampling parameter overrides for LLM inference.

    All fields are optional — when None the provider/API default is used.
    Currently effective for OpenAI-compatible APIs (OpenAI, DeepSeek, llamacpp).
    """

    temperature: float | None = Field(
        default=None, ge=0.0, le=2.0,
        description="Sampling temperature (0.0 = deterministic)",
    )
    top_p: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Nucleus sampling top-p",
    )
    top_k: int | None = Field(
        default=None, ge=1,
        description="Top-k sampling (llamacpp specific, ignored by OpenAI/DeepSeek)",
    )
    repeat_penalty: float | None = Field(
        default=None, ge=1.0, le=2.0,
        description="Repeat penalty (llamacpp specific, ignored by OpenAI/DeepSeek)",
    )


# ---------------------------------------------------------------------------
# LlamaCppConfig
# ---------------------------------------------------------------------------


class LlamaCppConfig(BaseModel):
    """llama.cpp server configuration.  Only effective for llamacpp provider."""

    gpu_layers: int = Field(
        default=99, ge=0, le=999,
        description="Number of layers to offload to GPU (-ngl). 99 = all layers.",
    )
    cache_type_k: str = Field(
        default="q8_0",
        description="KV cache K quantization type (f16, q8_0, q4_0)",
    )
    cache_type_v: str = Field(
        default="q8_0",
        description="KV cache V quantization type (f16, q8_0, q4_0)",
    )


# ---------------------------------------------------------------------------
# MCP configuration models
# ---------------------------------------------------------------------------


class McpServerTransport(str, Enum):
    STDIO = "stdio"
    SSE = "sse"
    WEBSOCKET = "websocket"


class McpOAuthConfig(BaseModel):
    """OAuth 2.0 / 2.1 configuration for MCP server authentication."""

    client_id: str
    client_secret: str | None = None
    authorization_url: str | None = None
    token_url: str
    scopes: list[str] = Field(default_factory=list)
    redirect_uri: str | None = None
    device_code_url: str | None = None

    # -- runtime token storage --
    token: str | None = None
    refresh_token: str | None = None
    expires_at: float | None = None

    # -- JWT metadata validation --
    audience: str | None = None
    issuer: str | None = None


class McpServerConfig(BaseModel):
    """Configuration for a single MCP server connection."""

    transport: McpServerTransport = McpServerTransport.STDIO
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None

    # -- OAuth --
    auth: McpOAuthConfig | None = None

    # -- Tool filtering (optional) --
    include_tools: list[str] | None = None
    exclude_tools: list[str] | None = None

    # -- Connection management --
    connect_timeout: float = 30.0
    request_timeout: float = 60.0
    max_reconnect_attempts: int = 3
    reconnect_delay: float = 2.0

    @model_validator(mode="after")
    def _validate_transport_fields(self) -> "McpServerConfig":
        if self.transport == McpServerTransport.STDIO:
            if not self.command:
                raise ValueError("'command' is required for stdio transport")
        elif self.transport in (McpServerTransport.SSE, McpServerTransport.WEBSOCKET):
            if not self.url:
                raise ValueError("'url' is required for sse/websocket transport")
        return self


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
    mcp_servers: dict[str, McpServerConfig] = Field(
        default_factory=dict,
        description="MCP server configurations keyed by server name",
    )
    workspace_root: str | None = Field(
        default=None,
        description=(
            "Workspace root directory used as the security boundary for "
            "file/path tools (Read/Edit/Write/Glob and Bash working_dir). "
            "When None, the workspace boundary check is disabled and tools "
            "may operate anywhere the process can reach."
        ),
    )
    skills_dir: str | None = Field(
        default=None,
        description="Directory path for skill definitions (not yet wired — Item 9)",
    )
    hooks: list[Any] = Field(
        default_factory=list,
        description=(
            "Hook instances registered at agent bootstrap "
            "(not yet wired — Item 9; values are currently ignored)"
        ),
    )
    auto_enable_plugins: list[str] = Field(
        default_factory=list,
        description="Plugin names to auto-enable at agent startup (Item 9)",
    )
    allow_unknown_model: bool = Field(
        default=False,
        description=(
            "If True, accept model identifiers outside the built-in known lists "
            "(e.g. when routing through a proxy or self-hosted base_url). "
            "Unknown names are still warned about, not rejected."
        ),
    )
    context_size: int | None = Field(
        default=None,
        ge=512,
        le=1_000_000,
        description=(
            "Context window size in tokens. For local providers (e.g. llamacpp) "
            "this is passed as --ctx-size to the server at startup. When None, "
            "the provider default is used (8192 for llamacpp)."
        ),
    )
    max_concurrency: int = Field(
        default=5,
        ge=1,
        le=20,
        description=(
            "Maximum number of concurrency-safe tools to execute in parallel "
            "during a single turn."
        ),
    )
    compression: CompressionConfig = Field(
        default_factory=CompressionConfig,
        description="Auto-compression configuration",
    )
    logging: LoggingConfig = Field(
        default_factory=LoggingConfig,
        description="Conversation logging / data-collection configuration (Item 12)",
    )
    sampling: SamplingConfig | None = Field(
        default=None,
        description=(
            "Sampling parameter overrides. When None, provider/API defaults "
            "are used. Effective for OpenAI-compatible APIs (OpenAI, DeepSeek, llamacpp)."
        ),
    )
    llamacpp: LlamaCppConfig = Field(
        default_factory=LlamaCppConfig,
        description="llama.cpp server options (GPU layers, KV cache quantization, etc.)",
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
            ModelProvider.DEEPSEEK: "DEEPSEEK_API_KEY",
            ModelProvider.LLAMACPP: "LLAMACPP_API_KEY",
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
    final_messages: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Full conversation message list at the end of this turn loop "
            "(prior history + everything produced this prompt). Lets a "
            "session runtime thread history into the next prompt."
        ),
        exclude=True,
    )


class CompactMessage(BaseModel):
    """Compression event notification emitted during the agent stream.

    Allows callers to observe and log compression activity.  Non-breaking —
    callers that do not match this variant simply ignore it.
    """

    type: Literal["compact"] = "compact"
    status: Literal["started", "completed", "skipped"]
    rounds_compressed: int = 0
    rounds_preserved: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    compression_ratio: float = 0.0
    summary: str | None = None
    retries_used: int = 0
    error_detail: str | None = None


AgentMessage = Annotated[
    AssistantText | ToolUse | ToolResult | ErrorMessage | DoneMessage | CompactMessage,
    Field(discriminator="type"),
]
