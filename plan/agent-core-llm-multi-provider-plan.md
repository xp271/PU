# LLM 多 Provider 集成层 —— 实现方案

> 对应 TodoList 条目 #3，预估 3 天。
> 目标：provider-agnostic 的 LLM 客户端抽象，Anthropic + OpenAI 首批实现，
> 内部统一事件流，主循环完全不知 provider 细节。

---

> **实现完成度** (2026-06-10)：
> - ModelProvider / ModelId / AgentConfig 多 Provider 字段 — 已完成
> - InternalEvent 事件流 — 已完成
> - BaseClient 抽象 + Provider 注册表（registry 替代 match/case）— 已完成
> - AnthropicClient（自包含：消息转换 + tool schema + stop 映射 + 错误分类 + 流式解析）— 已完成
> - OpenAIClient（同上，自包含）— 已完成
> - 错误分类 + 重试配置 — 已完成
> - **结构化日志** (`logging/tracer.py`) — **待实现**
>
> **与原始计划的差异**：
> 1. `normalize.py` 和 `tool_schema.py` 已删除，内容按 Provider 内聚到 `anthropic.py` / `openai.py` 中
> 2. `classify_error()` 从独立函数改为各 Provider 的 classmethod
> 3. `get_client()` 使用注册表模式 (`register_client()`) 替代硬编码 match/case
> 4. 错误重试从 tenacity decorator 改为内联手动指数退避
> 5. 新增 Provider 只需 2 步：新文件 + `models.py` 加枚举

---

## 1. 架构总览

```
                        ┌─────────────────────────────┐
                        │      query() 主循环          │
                        │  只消费 AsyncGenerator       │
                        │      [InternalEvent]         │
                        └───────────┬─────────────────┘
                                    │
                  ┌─────────────────▼──────────────────┐
                  │          BaseClient (ABC)          │
                  │  stream_messages(messages, tools,  │
                  │    system, model) -> AsyncGen      │
                  └──────┬──────────────────┬─────────┘
                         │                  │
              ┌──────────▼───┐    ┌─────────▼────────┐
              │ AnthropicClient│   │  OpenAIClient    │
              │ (anthropic SDK)│   │  (openai SDK)    │
              └──────┬────────┘    └────────┬─────────┘
                     │                      │
                     ▼                      ▼
              Annotated stream       Chat Completions
              Messages API           streaming API
```

**核心设计原则**：
- **主循环只消费 `InternalEvent`**，不知道 provider 存在
- **每个 provider 是一个自包含 `.py` 文件**，包含该 provider 全部逻辑（消息转换、工具 schema、stop 映射、错误分类、流式解析）
- **注册表模式路由**：Provider 模块 import 时调用 `register_client()` 自动注册，`get_client()` 通过字典查找路由，无硬编码分支
- **消息存储格式采用 Anthropic content-block 结构**（更丰富、支持多 content block），出站时通过 `convert_messages()` classmethod 按需转换
- **错误分类 per-provider**，统一的 `ErrorCategory` 类型 + `retry_config_for()` 工厂函数

---

## 2. 数据模型 (`models.py` 新增)

### 2.1 ModelProvider 枚举

```python
class ModelProvider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"

    @classmethod
    def known_providers(cls) -> set[str]:
        return {p.value for p in cls}

    @classmethod
    def known_models(cls) -> dict["ModelProvider", list[str]]:
        """返回每个 provider 的已知模型名列表。"""
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
```

### 2.2 ModelId

```python
from dataclasses import dataclass

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
        支持格式:
          "anthropic/claude-sonnet-4-5"  → ModelId(ANTHROPIC, "claude-sonnet-4-5")
          "openai/gpt-4o"                → ModelId(OPENAI, "gpt-4o")
          "claude-sonnet-4-5"            → ModelId(ANTHROPIC, "claude-sonnet-4-5")  # 向后兼容
          "gpt-4o"                        → ValueError (非Anthropic模型无前缀歧义)
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

        # 无前缀：检查是否已知 Anthropic 模型（向后兼容）
        for m in ModelProvider.known_models().get(ModelProvider.ANTHROPIC, []):
            if m == raw:
                return cls(provider=ModelProvider.ANTHROPIC, name=raw)

        raise ValueError(
            f"Cannot parse model '{raw}': no provider prefix and not a known "
            f"Anthropic model. Use format 'provider/model-name', e.g. 'openai/gpt-4o'."
        )

    @classmethod
    def known_values(cls) -> set[str]:
        """返回所有已知的 'provider/model' 字符串。"""
        result = set()
        for provider, models in ModelProvider.known_models().items():
            for m in models:
                result.add(f"{provider.value}/{m}")
        return result
```

### 2.3 AgentConfig 修改

在现有 `AgentConfig` 上新增两个字段，调整 `validate_model` 逻辑：

```python
class AgentConfig(BaseModel):
    model: str = Field(default_factory=lambda: str(ModelId(
        ModelProvider.ANTHROPIC, "claude-sonnet-4-5"
    )))
    # ... 现有字段不变 ...

    # 新增字段
    api_keys: dict[str, str] = Field(
        default_factory=dict,
        description="Provider → API key mapping. Falls back to env vars "
                    "(ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.)",
    )
    base_urls: dict[str, str] = Field(
        default_factory=dict,
        description="Provider → custom base URL. Only overrides the same "
                    "provider's default API endpoint.",
    )

    # --- Property helpers ---

    @property
    def model_id(self) -> ModelId:
        """解析为结构化 ModelId。"""
        return ModelId.parse(self.model)

    def get_api_key(self, provider: ModelProvider) -> str | None:
        """按优先级：AgentConfig.api_keys → 环境变量。"""
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

    # --- validate_model 调整 ---

    @model_validator(mode="after")
    def validate_model(self) -> "AgentConfig":
        model_str = self.model.strip()
        # 尝试解析为 ModelId
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
```

---

## 3. 内部事件流 (`llm/events.py`)

在现有 `AgentMessage` 5 种类型之外，新增纯内部用的事件类型：

```python
# ---- 新增内部事件 ----

class TokenCounts(BaseModel):
    """LLM 返回的 token 统计。内部事件，不暴露给 AgentMessage 流。"""
    type: Literal["token_counts"] = "token_counts"
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int | None = None   # Anthropic prompt caching 专用
    cache_write_tokens: int | None = None  # Anthropic prompt caching 专用


class StopEvent(BaseModel):
    """LLM 返回的终止信号。内部事件，由 query() 消费后转为 DoneMessage。"""
    type: Literal["stop"] = "stop"
    stop_reason: str                       # Provider 原始 stop reason
    finish_reason_detail: str | None = None


class StreamStart(BaseModel):
    """流开始信号。携带本轮元信息，供日志系统使用。"""
    type: Literal["stream_start"] = "stream_start"
    turn_index: int
    provider: str
    model: str


# ---- 内部事件联合类型 ----
# query() 循环消费这个类型，不作 provider 区分

InternalEvent = Annotated[
    StreamStart
    | AssistantText    # 复用已有
    | ToolUse           # 复用已有
    | TokenCounts
    | StopEvent
    | ErrorMessage,     # 复用已有 —— 流式传输中的异常
    Field(discriminator="type"),
]
```

**事件生命周期**：

```
StreamStart → stream_start  (turn 元信息)
  ├─ AssistantText   (文本增量，可多次)
  ├─ ToolUse         (工具调用请求)
  ├─ TokenCounts     (本轮的 token 统计，通常在 stop 前)
  └─ StopEvent       (终止信号)
       └─ stop_reason 由 query() 映射为 StopReason
```

---

## 4. BaseClient 抽象 + Provider 注册表 (`llm/base.py`)

```python
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator

from .events import InternalEvent
from .errors import ErrorCategory


# -- 全局注册表 --

_PROVIDER_CLIENTS: dict[ModelProvider, type[BaseClient]] = {}

def register_client(provider: ModelProvider, client_cls: type[BaseClient]) -> None:
    """Provider 模块 import 时调用，自动注册。"""

def get_client(model_id: ModelId, config: AgentConfig) -> BaseClient:
    """从注册表查找并实例化。无需修改此函数即可支持新 Provider。"""


# -- 抽象基类 --

class BaseClient(ABC):
    """自包含的 Provider 客户端抽象。

    每个子类覆盖 4 个 classmethod + 1 个 instance method。
    """

    def __init__(self, model: str, api_key: str | None, base_url: str | None): ...

    @classmethod
    def convert_messages(cls, messages, system) -> Any: ...

    @classmethod
    def convert_tools(cls, allowed_tools) -> list[dict]: ...

    @classmethod
    def normalize_stop_reason(cls, raw_reason) -> tuple[str, str | None]: ...

    @classmethod
    def classify_error(cls, exception) -> ErrorCategory: ...

    @abstractmethod
    async def stream_messages(
        self, messages: list[dict], tools: list[dict],
        system: str | None = None, max_tokens: int = 4096,
        turn_index: int = 0,
    ) -> AsyncGenerator[InternalEvent, None]:
        """产出 StreamStart → AssistantText* → ToolUse* → TokenCounts → StopEvent"""
```

**设计要点**：
- `register_client()` 替代了硬编码 match/case，新增 Provider 无需修改 `base.py`
- `convert_messages` / `convert_tools` / `normalize_stop_reason` / `classify_error` 作为 classmethod——无需实例化即可调用（例如 query() 在创建 client 前获取 tool schema）
- 原计划的独立 `normalize.py` / `tool_schema.py` 已删除——内容内聚到各 Provider 文件

---

## 5. 消息转换（已内聚到各 Provider 的 `convert_messages()` classmethod 中）

原计划的独立 `normalize.py` 已删除。消息转换逻辑现在是各 Provider Client 的 classmethod。

### 5.1 出站（Outbound）—— 内部格式 → Provider API 格式

内部存储格式统一使用 Anthropic content-block 结构。各 Provider 的 `convert_messages()` 负责转换为自身格式：

```
内部存储格式 (Anthropic content-block)
─────────────────────────────────────
{ "role": "user", "content": [
    {"type": "text", "text": "Read README.md"},
    {"type": "tool_result", "tool_use_id": "tu_1", "content": "..."}
]}
{ "role": "assistant", "content": [
    {"type": "text", "text": "Let me read that file."},
    {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {...}}
]}
```

- **Anthropic**：近乎恒等变换，仅剥离 `_` 前缀的内部元数据，返回 `(system, messages)` 元组
- **OpenAI**：system→首条 system message；user 中的 tool_result→独立 `role:"tool"` message；assistant 中的 tool_use→`tool_calls` 数组；空 text→`content=None`

### 5.2 入站（Inbound）—— Provider 流式 chunk → InternalEvent

入站转换在各 Client 的 `stream_messages()` / `_do_stream()` 中完成。每个 Client 解析自身 SDK 的事件格式并产出统一的 `InternalEvent`。

---

## 6. AnthropicClient (`llm/anthropic.py`) — 已实现

**自包含**：所有 Anthropic 专属逻辑集中在此文件。`register_client(ModelProvider.ANTHROPIC, AnthropicClient)` 自动注册。

| 方法 | 说明 |
|------|------|
| `convert_messages()` | 内部格式 → Anthropic API 参数，剥离 `_` 前缀字段，返回 `(system, messages)` 元组 |
| `convert_tools()` | Tool 注册表 → `[{name, description, input_schema}]` |
| `normalize_stop_reason()` | `end_turn`→end_turn, `max_tokens`→max_turns, `stop_sequence`/`tool_use`→end_turn |
| `classify_error()` | 429→rate_limit, 529→overloaded, 413/context→context_length, 5xx→transient, 401/403→fatal |
| `stream_messages()` | 封装 `AsyncAnthropic.messages.stream()` → 解析 ContentBlockStart/Delta/Stop, MessageStop → InternalEvent |
| 重试 | 内联手动循环（rate_limit/overloaded/transient 退避，3 次上限） |
| Prompt caching | `cache_read_input_tokens` / `cache_creation_input_tokens` 通过 `TokenCounts` 传递 |

---

## 7. OpenAIClient (`llm/openai.py`) — 已实现

**自包含**：所有 OpenAI 专属逻辑集中在此文件。`register_client(ModelProvider.OPENAI, OpenAIClient)` 自动注册。

| 方法 | 说明 |
|------|------|
| `convert_messages()` | 内部 content-block → OpenAI Chat Completions 格式（tool_result→role:tool, tool_use→tool_calls, 空 text→content=null） |
| `convert_tools()` | Tool 注册表 → `[{type:"function", function:{name, description, parameters}}]` |
| `normalize_stop_reason()` | `stop`→end_turn, `length`→max_turns, `tool_calls`→end_turn, `content_filter`→permission_denied |
| `classify_error()` | 429→rate_limit, 503→overloaded, 500/502→transient, 413/context→context_length, content_filter→content_filter, 401/403→fatal |
| `stream_messages()` | 封装 `AsyncOpenAI.chat.completions.create(stream=True)` → 解析 delta → InternalEvent |
| Tool call 聚合 | OpenAI 的 tool_calls 按 index 跨 chunk 累积，流结束后一次性产出 `ToolUse` |
| 重试 | 同 AnthropicClient，内联手动指数退避 |

---

## 8. Tool Schema 转换（已内聚到各 Provider 的 `convert_tools()` classmethod 中）

原计划的独立 `tool_schema.py` 已删除。逻辑现在是各 Provider 的 classmethod，从 Tool 注册表提取 `input_schema()` JSON Schema 并转为 Provider 原生格式。

- Anthropic: `{name, description, input_schema}`
- OpenAI: `{type: "function", function: {name, description, parameters}}`

---

## 9. 错误分类 + 重试 (`llm/errors.py`) — 已实现

```python
ErrorCategory = Literal[
    "rate_limit",       # 429 → 指数退避重试
    "overloaded",       # 529 (Anthropic) / 503 (OpenAI)
    "context_length",   # 不可恢复
    "content_filter",   # 内容过滤 (OpenAI)
    "transient",        # 500/502/网络错误
    "fatal",            # 认证/参数错误
]

def retry_config_for(classify_error_fn, provider: str) -> dict:
    """返回 tenacity.retry(**kwargs) 参数。"""
```

- `classify_error()` 逻辑在实现中移入了各 Provider 的 classmethod（每个 SDK 异常类型和 status code 语义不同）
- 当前重试采用内联手动循环（4s→8s→16s 指数退避），不依赖 tenacity decorator；`retry_config_for` 保留为未来迁移到 tenacity 的接口

---

## 10. StopReason 跨 Provider 映射（已内聚到各 Provider 的 `normalize_stop_reason()` classmethod 中）

| Anthropic raw | Internal |
|---------------|----------|
| `end_turn` / `stop_sequence` / `tool_use` | `end_turn` |
| `max_tokens` | `max_turns` |

| OpenAI raw | Internal |
|------------|----------|
| `stop` / `tool_calls` | `end_turn` |
| `length` | `max_turns` |
| `content_filter` | `permission_denied` |

---

## 11. 与 query() 主循环的集成（待实现——Phase 2 条目 #4）

```python
# 伪代码
model_id = config.model_id
client = get_client(model_id, config)
tools = client.__class__.convert_tools(config.tools)

async for event in client.stream_messages(messages, tools):
    match event:
        case StreamStart(): ...
        case AssistantText(): yield  # 透传给调用方
        case ToolUse(): await execute_and_append(...)
        case TokenCounts(): ...
        case StopEvent(): ...
        case ErrorMessage(): yield; return
```

`query()` 通过 `get_client()` 获取 client，通过 `client.__class__.convert_tools()` 获取 tool schema，通过 `client.stream_messages()` 消费事件流——主循环对 provider 零感知。

---

## 12. 结构化日志（待实现）

`logging/tracer.py`：每个 turn 记录结构化日志，统一 `trace_id`。字段：`turn_index / provider / model / input_tokens / output_tokens / cache_read_tokens(Anthropic) / tool_uses / latency_ms / error_category`。

---

## 13. 实现状态

| 步骤 | 文件 | 状态 |
|------|------|------|
| 1 | `models.py` — ModelProvider / ModelId / AgentConfig 扩展 | ✅ 已完成 |
| 2 | `llm/events.py` — InternalEvent 类型 | ✅ 已完成 |
| 3 | `llm/base.py` — BaseClient + 注册表 + factory | ✅ 已完成 |
| 4 | `llm/anthropic.py` — AnthropicClient（自包含） | ✅ 已完成 |
| 5 | `llm/openai.py` — OpenAIClient（自包含） | ✅ 已完成 |
| 6 | `llm/errors.py` — ErrorCategory + retry_config_for | ✅ 已完成 |
| 7 | `logging/tracer.py` — 结构化日志 | ⬜ 待实现 |
| 8 | `tests/` — 单元 + mock 集成测试 | ⬜ 待实现 |

> `normalize.py` 和 `tool_schema.py` 已删除——内容内聚到各 Provider 文件。

---

## 14. 测试策略

### 单元测试

- `test_model_id.py`：`ModelId.parse()` 各种格式（有前缀/无前缀/无效 provider/无效模型名）
- Provider-specific 测试：mock Anthropic SDK stream → 验证 `AnthropicClient` 产出正确 `InternalEvent`；mock OpenAI SDK stream → 验证 `OpenAIClient`
- `test_errors.py`：各 Provider `classify_error()` 分类正确性

### Mock 集成测试（不依赖真实 API）

- `MockAnthropicStream`：模拟 Anthropic SDK 流式事件序列
- `MockOpenAIStream`：模拟 OpenAI SDK 流式 chunk 序列
- `test_multiprovider_integration`：同一 prompt 经不同 Provider mock → 产出相同的 AgentMessage 序列

### vcrpy 录制回放（需要一次真实 API key）

- Anthropic 端到端 turn flow (text + tool_use + text)
- OpenAI 端到端 turn flow (text + tool_call + text)
