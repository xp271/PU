# Agent API

The `Agent` class is the only entry point. Everything else is internal.

## `Agent` class

```python
from agent_core import Agent, AgentConfig

agent = Agent(config)
```

### Constructor

```
Agent(config: AgentConfig) -> None
```

Takes one `AgentConfig` instance. Initialises internal state — no network calls.

### Methods

| Method | Signature | Description |
|--------|----------|-------------|
| `run` | `async run(prompt: str) -> AgentResult` | Execute a prompt, aggregate the stream, return the final result. |
| `stream` | `async stream(prompt: str) -> AsyncGenerator[AgentMessage, None]` | Execute a prompt and yield typed messages as they arrive. |
| `on_permission_check` | `on_permission_check(callback: PermissionCallback) -> None` | Register an async callback `(tool_name, tool_input) -> bool` for permission decisions. |

---

## `AgentConfig`

All fields are optional with sensible defaults.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `model` | `str` | `"anthropic/claude-sonnet-4-5"` | Model identifier in `"provider/model-name"` format. Omit provider to default to `anthropic`. |
| `max_turns` | `int` | `20` | Maximum turn iterations (1–500). |
| `permission_mode` | `PermissionMode` | `"ask"` | `"bypassPermissions"` / `"acceptEdits"` / `"ask"`. |
| `tools` | `list[str]` | `["*"]` | Available tool names. `["*"]` = all registered tools. |
| `mcp_servers` | `dict[str, Any]` | `{}` | MCP server configurations keyed by name. |
| `skills_dir` | `str \| None` | `None` | Directory path for skill definition files. |
| `hooks` | `list[Any]` | `[]` | Hook instances registered at agent startup. |
| `api_keys` | `dict[str, str]` | `{}` | Provider → API key mapping. Falls back to env vars (`ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`). |
| `base_urls` | `dict[str, str]` | `{}` | Provider → custom base URL (for proxies / self-hosted). |
| `allow_unknown_model` | `bool` | `False` | Allow model identifiers outside the known list. |
| `compression` | `CompressionConfig` | `CompressionConfig()` | Auto-compression settings. |
| `max_concurrency` | `int` | `5` | Maximum concurrent safe-tool executions (1–20). |
| `auto_enable_plugins` | `list[str]` | `[]` | Plugin names to auto-activate. |

### `PermissionCallback`

```python
PermissionCallback = Callable[[str, dict[str, Any]], Awaitable[bool]]
```

Return `True` to allow, `False` to deny.

---

## `AgentResult`

```python
class AgentResult(BaseModel):
    content: str                        # LLM final text response
    total_tokens: int                   # total tokens consumed
    tool_uses: int                      # total tool calls made
    stop_reason: StopReason             # why the agent stopped
    error_detail: str | None = None     # details if stop_reason="error"
```

### `StopReason`

```python
StopReason = Literal["end_turn", "max_turns", "permission_denied", "error"]
```

---

## `AgentMessage` (streamed)

A Pydantic discriminated union keyed on `type`. Six variants:

### `AssistantText`

```python
class AssistantText(BaseModel):
    type: Literal["assistant_text"] = "assistant_text"
    content: str
```

### `ToolUse`

```python
class ToolUse(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    tool_use_id: str
    tool_name: str
    tool_input: dict[str, Any]
```

### `ToolResult`

```python
class ToolResult(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    tool_name: str
    content: Any
    is_error: bool = False
```

### `ErrorMessage`

```python
class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    message: str
    error_type: str | None = None
```

### `DoneMessage`

```python
class DoneMessage(BaseModel):
    type: Literal["done"] = "done"
    stop_reason: StopReason
    total_tokens: int = 0
    error_detail: str | None = None
```

### `CompactMessage`

```python
class CompactMessage(BaseModel):
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
```

---

## `ModelProvider` & `ModelId`

```python
class ModelProvider(str, Enum):
    ANTHROPIC = "anthropic"
    DEEPSEEK = "deepseek"
    OPENAI = "openai"

@dataclass(frozen=True)
class ModelId:
    provider: ModelProvider
    name: str  # e.g. "claude-sonnet-4-5"

    def __str__(self) -> str:           # "anthropic/claude-sonnet-4-5"
        ...

    @classmethod
    def parse(cls, raw: str) -> "ModelId":
        """'provider/model' format; omitting provider defaults to anthropic."""
        ...
```

## `CompressionConfig`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `True` | Enable auto-compression. |
| `keep_recent_rounds` | `int` (1–10) | `3` | Recent rounds kept uncompressed. |
| `summary_max_tokens` | `int` (512–8192) | `2048` | Max tokens for the summary. |
| `tool_result_preview_chars` | `int` (50–500) | `200` | Preview chars for tool results in the prompt. |
| `max_retries` | `int` (0–5) | `2` | Max retries on verification failure. |
| `prompt_template` | `str \| None` | `None` | Custom compression prompt. Must contain `{preview_chars}` and `{conversation_text}`. |
