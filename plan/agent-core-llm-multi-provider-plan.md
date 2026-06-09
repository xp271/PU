# LLM 多 Provider 集成层 —— 实现方案

> 对应 TodoList 条目 #3，预估 3 天。
> 目标：provider-agnostic 的 LLM 客户端抽象，Anthropic + OpenAI 首批实现，
> 内部统一事件流，主循环完全不知 provider 细节。

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
- **每个 provider 是一个独立模块**，换 provider 只需换 client 实现
- **消息存储格式采用 Anthropic content-block 结构**（更丰富、支持多 content block），出站时按需转换
- **错误恢复 per-provider**，统一分类后由 `errors.py` 的工厂函数返回 tenacity 配置

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

## 4. BaseClient 抽象 (`llm/base.py`)

```python
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator

from .events import InternalEvent


class BaseClient(ABC):
    """Provider-agnostic LLM 客户端抽象。

    每个 provider 实现此接口，将自身 SDK 的流式响应转为统一的
    InternalEvent 流。调用方（query()）只通过此接口消费 LLM 输出。
    """

    def __init__(self, model: str, api_key: str | None, base_url: str | None) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url

    @abstractmethod
    async def stream_messages(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str | None = None,
        max_tokens: int = 4096,
    ) -> AsyncGenerator[InternalEvent, None]:
        """
        发送消息并流式产出 InternalEvent。

        参数：
          messages:  内部格式的消息历史（Anthropic content-block 结构）
          tools:     工具 schema 列表（Provider 原生格式，由 tool_schema.py 生成）
          system:    系统提示词（None 表示无系统提示）
          max_tokens:最大输出 token 数

        产出：
          StreamStart → AssistantText* → (ToolUse)* → TokenCounts → StopEvent

        错误：
          可恢复错误在内部重试（不向外暴露）
          不可恢复错误 yield ErrorMessage + StopEvent
        """
        ...


def get_client(model_id: ModelId, config: "AgentConfig") -> BaseClient:
    """工厂函数：按 provider 路由到对应 client 实现。"""
    from .anthropic import AnthropicClient
    from .openai import OpenAIClient

    provider = model_id.provider
    api_key = config.get_api_key(provider)
    base_url = config.get_base_url(provider)

    match provider:
        case ModelProvider.ANTHROPIC:
            return AnthropicClient(
                model=model_id.name,
                api_key=api_key,
                base_url=base_url,
            )
        case ModelProvider.OPENAI:
            return OpenAIClient(
                model=model_id.name,
                api_key=api_key,
                base_url=base_url,
            )
        case _:
            raise ValueError(f"No client implementation for provider: {provider}")
```

**工厂函数导入写在函数内部**（延迟导入），避免未安装的 SDK 在模块加载时触发 `ImportError`。

---

## 5. 消息规范化 (`llm/normalize.py`)

负责 **双向转换**：

### 5.1 出站（Outbound）—— 内部格式 → Provider API 格式

虽然主循环内部存储采用 Anthropic content-block 结构，但这只影响 `messages` 参数中每条消息的 `content` 字段格式。不同 provider 的需求：

```
┌──────────────────────────────────────────────────────────────┐
│              内部存储格式 (Anthropic content-block)           │
│                                                              │
│  { "role": "user", "content": [                              │
│      {"type": "text", "text": "Read README.md"},             │
│      {"type": "tool_result", "tool_use_id": "tu_1",          │
│       "content": "file content..."}                          │
│  ]}                                                          │
│                                                              │
│  { "role": "assistant", "content": [                         │
│      {"type": "text", "text": "Let me read that file."},     │
│      {"type": "tool_use", "id": "tu_1", "name": "Read",     │
│       "input": {"file_path": "/x"}}                          │
│  ]}                                                          │
└──────────────────────────────────────────────────────────────┘
```

```python
def messages_for_anthropic(
    messages: list[dict[str, Any]],
    system: str | None,
) -> tuple[str | None, list[dict[str, Any]]]:
    """
    Anthropic API 与内部格式一致，几乎无需转换。
    仅需：
      1. 提取 system 为顶层参数（而非 messages[0]）
      2. 过滤内部元数据字段（如 _timestamp, _turn）
    """
    clean = [_clean_message(m) for m in messages]
    return system, clean


def messages_for_openai(
    messages: list[dict[str, Any]],
    system: str | None,
) -> list[dict[str, Any]]:
    """
    将内部 Anthropic content-block 结构转为 OpenAI 格式。

    转换规则：
      1. System prompt: 插入为首条消息 {"role": "system", "content": system}
      2. User message with tool_result blocks:
         → 拆分为多条 OpenAI message:
           - 先发 user message（纯文本部分）
           - 每个 tool_result → {"role": "tool", "tool_call_id": ..., "content": ...}
      3. Assistant message with tool_use blocks:
         → {"role": "assistant",
            "content": text,
            "tool_calls": [{"id": id, "type": "function",
                            "function": {"name": name, "arguments": json}}]}
      4. 空 content 的 assistant message → content=None（OpenAI 要求）
    """
    openai_messages: list[dict[str, Any]] = []

    # 1. 系统提示词
    if system:
        openai_messages.append({"role": "system", "content": system})

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", [])

        if isinstance(content, str):
            # 纯文本消息，直通
            openai_messages.append({"role": role, "content": content})
            continue

        if role == "user":
            # 分离 tool_result 和纯文本
            text_parts = []
            tool_results = []
            for block in content:
                if block.get("type") == "tool_result":
                    tool_results.append(block)
                else:
                    text_parts.append(block)

            # 纯文本部分 → 单条 user message
            if text_parts:
                openai_messages.append({
                    "role": "user",
                    "content": _blocks_to_openai_text(text_parts),
                })

            # tool_result → 独立 tool message
            for tr in tool_results:
                result_content = tr.get("content", "")
                if isinstance(result_content, (list, dict)):
                    result_content = json.dumps(result_content, ensure_ascii=False)
                openai_messages.append({
                    "role": "tool",
                    "tool_call_id": tr["tool_use_id"],
                    "content": str(result_content),
                })

        elif role == "assistant":
            text_content = ""
            tool_calls = []
            for block in content:
                if block.get("type") == "text":
                    text_content += block.get("text", "")
                elif block.get("type") == "tool_use":
                    tool_calls.append({
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block["input"], ensure_ascii=False),
                        },
                    })

            msg_out: dict[str, Any] = {"role": "assistant"}
            if text_content:
                msg_out["content"] = text_content
            else:
                msg_out["content"] = None  # OpenAI: tool-only message → content=null
            if tool_calls:
                msg_out["tool_calls"] = tool_calls
            openai_messages.append(msg_out)

    return openai_messages
```

### 5.2 入站（Inbound）—— Provider 流式 chunk → InternalEvent

入站转换逻辑在各 Client 实现内完成。每个 Client 负责：
- 解析自身 SDK 的流式事件
- 累积文本/tool_use 增量
- 产出统一的 `InternalEvent`

辅助函数（共享的工具映射逻辑）：

```python
def _provider_stop_to_stop_reason(
    provider: ModelProvider, raw_reason: str
) -> tuple[StopReason, str | None]:
    """
    将 provider 原始 stop_reason 映射到内部 StopReason。

    Anthropic: "end_turn" → "end_turn"
               "max_tokens" → "max_turns"
               "stop_sequence" → "end_turn"
               "tool_use" → "end_turn" (query 继续循环)

    OpenAI:     "stop" → "end_turn"
               "length" → "max_turns"
               "tool_calls" → "end_turn" (query 继续循环)
               "content_filter" → "permission_denied"
    """
    ...
```

---

## 6. AnthropicClient (`llm/anthropic.py`)

```python
import anthropic
from anthropic.types import (
    ContentBlockDeltaEvent,
    ContentBlockStartEvent,
    ContentBlockStopEvent,
    MessageDeltaEvent,
    MessageStartEvent,
    MessageStopEvent,
)
from tenacity import retry, stop_after_attempt, wait_exponential

from .base import BaseClient
from .errors import classify_error, retry_config_for
from .events import (
    AssistantText,
    ErrorMessage,
    InternalEvent,
    StopEvent,
    StreamStart,
    TokenCounts,
    ToolUse,
)
from .normalize import messages_for_anthropic


class AnthropicClient(BaseClient):
    def __init__(self, model: str, api_key: str | None, base_url: str | None):
        super().__init__(model, api_key, base_url)
        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self._client = anthropic.AsyncAnthropic(**kwargs)

    async def stream_messages(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str | None = None,
        max_tokens: int = 4096,
        turn_index: int = 0,
    ) -> AsyncGenerator[InternalEvent, None]:
        system, clean_messages = messages_for_anthropic(messages, system)

        yield StreamStart(
            turn_index=turn_index,
            provider="anthropic",
            model=self.model,
        )

        retry_config = retry_config_for("anthropic")

        @retry(**retry_config)
        async def _stream():
            async with self._client.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                messages=clean_messages,
                system=system or anthropic.NOT_GIVEN,
                tools=tools or anthropic.NOT_GIVEN,
            ) as stream:
                current_text_acc = ""
                current_tool_use: dict | None = None

                async for event in stream:
                    match event:
                        case ContentBlockStartEvent():
                            if event.content_block.type == "text":
                                current_text_acc = ""
                            elif event.content_block.type == "tool_use":
                                current_tool_use = {
                                    "id": event.content_block.id,
                                    "name": event.content_block.name,
                                    "input_json": "",
                                }

                        case ContentBlockDeltaEvent() as delta:
                            if delta.delta.type == "text_delta":
                                current_text_acc += delta.delta.text
                                yield AssistantText(content=delta.delta.text)
                            elif delta.delta.type == "input_json_delta":
                                current_tool_use["input_json"] += delta.delta.partial_json

                        case ContentBlockStopEvent():
                            if current_tool_use:
                                input_dict = json.loads(current_tool_use["input_json"])
                                yield ToolUse(
                                    tool_use_id=current_tool_use["id"],
                                    tool_name=current_tool_use["name"],
                                    tool_input=input_dict,
                                )
                                current_tool_use = None

                        case MessageDeltaEvent():
                            pass  # usage 在 message_stop 中

                        case MessageStopEvent():
                            usage = stream.current_message_snapshot.usage
                            yield TokenCounts(
                                input_tokens=usage.input_tokens,
                                output_tokens=usage.output_tokens,
                                cache_read_tokens=getattr(usage, "cache_read_input_tokens", None),
                                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None),
                            )

                            stop_reason = stream.current_message_snapshot.stop_reason
                            yield StopEvent(
                                stop_reason=stop_reason or "end_turn",
                                finish_reason_detail=None,
                            )

        try:
            async for event in _stream():
                yield event
        except anthropic.APIStatusError as e:
            category = classify_error(e, ModelProvider.ANTHROPIC)
            if category == "fatal":
                yield ErrorMessage(
                    message=f"Fatal LLM error: {e.message}",
                    error_type=category,
                )
                yield StopEvent(stop_reason="error", finish_reason_detail=str(e))
            # 可恢复错误由 tenacity retry 处理
        except Exception as e:
            yield ErrorMessage(message=str(e), error_type="fatal")
            yield StopEvent(stop_reason="error", finish_reason_detail=str(e))
```

### 6.1 max_output_tokens 恢复

当 `stop_reason == "max_tokens"` 时，`query()` 循环中的逻辑负责：

```python
# 在 query() 中（非 AnthropicClient 内部）
MAX_CONTINUE_RETRIES = 3
continue_count = 0

if stop_reason == "max_tokens" and continue_count < MAX_CONTINUE_RETRIES:
    # 发送 continue：保持 system + messages，追加最后一条 assistant 的不完整输出
    continue_count += 1
    # 再次调用 client.stream_messages()，从断点续接
```

---

## 7. OpenAIClient (`llm/openai.py`)

```python
import json
from collections.abc import AsyncGenerator

import openai
from openai.types.chat import (
    ChatCompletionChunk,
    ChatCompletionMessageToolCall,
)

from .base import BaseClient
from .errors import classify_error, retry_config_for
from .events import (
    AssistantText,
    ErrorMessage,
    InternalEvent,
    StopEvent,
    StreamStart,
    TokenCounts,
    ToolUse,
)
from .normalize import messages_for_openai


class OpenAIClient(BaseClient):
    def __init__(self, model: str, api_key: str | None, base_url: str | None):
        super().__init__(model, api_key, base_url)
        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self._client = openai.AsyncOpenAI(**kwargs)

    async def stream_messages(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str | None = None,
        max_tokens: int = 4096,
        turn_index: int = 0,
    ) -> AsyncGenerator[InternalEvent, None]:
        openai_messages = messages_for_openai(messages, system)

        yield StreamStart(
            turn_index=turn_index,
            provider="openai",
            model=self.model,
        )

        retry_config = retry_config_for("openai")

        @retry(**retry_config)
        async def _stream():
            # OpenAI 流式使用 Chat Completions API
            stream = await self._client.chat.completions.create(
                model=self.model,
                messages=openai_messages,
                tools=tools or openai.NOT_GIVEN,
                max_tokens=max_tokens,
                stream=True,
                stream_options={"include_usage": True},
            )

            current_text = ""
            current_tool_calls: dict[int, dict] = {}  # index → accumulated tool_call
            final_usage = None
            final_finish_reason = None

            async for chunk in stream:
                # 收集 usage（OpenAI 在最后 chunk 中返回）
                if chunk.usage:
                    final_usage = chunk.usage

                # 收集 finish_reason
                if chunk.choices and chunk.choices[0].finish_reason:
                    final_finish_reason = chunk.choices[0].finish_reason

                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta

                # 文本增量
                if delta.content:
                    current_text += delta.content
                    yield AssistantText(content=delta.content)

                # 工具调用增量
                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in current_tool_calls:
                            current_tool_calls[idx] = {
                                "id": tc_delta.id or "",
                                "name": "",
                                "arguments": "",
                            }
                        if tc_delta.id:
                            current_tool_calls[idx]["id"] = tc_delta.id
                        if tc_delta.function and tc_delta.function.name:
                            current_tool_calls[idx]["name"] = tc_delta.function.name
                        if tc_delta.function and tc_delta.function.arguments:
                            current_tool_calls[idx]["arguments"] += tc_delta.function.arguments

            # 流结束后产出完整的 ToolUse 事件
            for idx in sorted(current_tool_calls.keys()):
                tc = current_tool_calls[idx]
                try:
                    input_dict = json.loads(tc["arguments"])
                except json.JSONDecodeError:
                    input_dict = {"_raw": tc["arguments"]}
                yield ToolUse(
                    tool_use_id=tc["id"],
                    tool_name=tc["name"],
                    tool_input=input_dict,
                )

            # Token 统计
            if final_usage:
                yield TokenCounts(
                    input_tokens=final_usage.prompt_tokens,
                    output_tokens=final_usage.completion_tokens,
                )

            # 停止事件
            yield StopEvent(
                stop_reason=final_finish_reason or "stop",
                finish_reason_detail=None,
            )

        try:
            async for event in _stream():
                yield event
        except openai.APIStatusError as e:
            category = classify_error(e, ModelProvider.OPENAI)
            if category == "fatal":
                yield ErrorMessage(
                    message=f"Fatal LLM error ({e.status_code}): {e.message}",
                    error_type=category,
                )
                yield StopEvent(stop_reason="error", finish_reason_detail=str(e))
        except Exception as e:
            yield ErrorMessage(message=str(e), error_type="fatal")
            yield StopEvent(stop_reason="error", finish_reason_detail=str(e))
```

### 7.1 OpenAI 特有处理

1. **Content filter**: `finish_reason == "content_filter"` → yield `ErrorMessage` + `StopEvent(stop_reason="content_filtered")`
2. **Tool call index 聚合**: OpenAI 的 tool_calls 是通过 index 增量传递的，需要在流结束前累积所有 fragments 再产出完整 `ToolUse`
3. **stream_options={"include_usage"}**: 需要此参数才能获得 token 统计

---

## 8. Tool Schema 转换 (`llm/tool_schema.py`)

从 Tool 注册表提取 schema 并转为 provider 原生格式：

```python
from agent_core.tools import registered_tools


def tools_for_anthropic(
    allowed_tools: list[str],  # ["*"] 或 ["Read", "Glob", ...]
) -> list[dict[str, Any]]:
    """生成 Anthropic Messages API 的 tools 参数格式。"""
    tools: list[dict] = []
    for name, cls in registered_tools().items():
        if "*" not in allowed_tools and name not in allowed_tools:
            continue
        tools.append({
            "name": name,
            "description": cls.__doc__ or "",
            "input_schema": cls.input_schema(),
        })
    return tools


def tools_for_openai(
    allowed_tools: list[str],
) -> list[dict[str, Any]]:
    """生成 OpenAI Chat Completions API 的 tools 参数格式。"""
    tools: list[dict] = []
    for name, cls in registered_tools().items():
        if "*" not in allowed_tools and name not in allowed_tools:
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": cls.__doc__ or "",
                "parameters": cls.input_schema(),
            },
        })
    return tools
```

---

## 9. 错误恢复统一 (`llm/errors.py`)

```python
from enum import Enum
from typing import Literal

ErrorCategory = Literal[
    "rate_limit",         # 429 / rate_limit → 退避重试
    "overloaded",         # 529 (Anthropic) / 503 (OpenAI) → 退避重试
    "context_length",     # context_window_exceeded → 不可恢复
    "content_filter",     # 内容过滤 (OpenAI content_filter / Anthropic 无等效)
    "transient",          # 500/502/网络错误 → 退避重试
    "fatal",              # 认证错误 / 参数错误 / 其他不可恢复
]


def classify_error(exception, provider: "ModelProvider") -> ErrorCategory:
    """根据 provider 和异常类型分类为 ErrorCategory。"""
    if provider == ModelProvider.ANTHROPIC:
        from anthropic import APIStatusError
        if not isinstance(exception, APIStatusError):
            return "transient"

        status = exception.status_code
        if status == 429:
            return "rate_limit"
        if status == 529:
            return "overloaded"
        if status == 413 or (status == 400 and "context" in str(exception).lower()):
            return "context_length"
        if status in (500, 502, 503):
            return "transient"
        if status in (401, 403):
            return "fatal"
        return "fatal"

    elif provider == ModelProvider.OPENAI:
        from openai import APIStatusError
        if not isinstance(exception, APIStatusError):
            return "transient"

        status = exception.status_code
        if status == 429:
            return "rate_limit"
        if status in (500, 502, 503):
            # 503 → overloaded（OpenAI 没有 529）
            return "overloaded" if status == 503 else "transient"
        if status == 413 or (status == 400 and "context" in str(exception).lower()):
            return "context_length"
        if status == 400 and "content_filter" in str(exception).lower():
            return "content_filter"
        if status in (401, 403):
            return "fatal"
        return "fatal"

    return "fatal"


def retry_config_for(provider: str) -> dict:
    """返回 tenacity retry kwargs，按 provider 差异化配置。"""
    return {
        "retry": _retry_predicate(provider),
        "wait": "wait_exponential(multiplier=1, min=4, max=60)",
        "stop": "stop_after_attempt(3)",
        "reraise": True,
    }
```

**重试策略表**：

| ErrorCategory | 行为 |
|------|------|
| `rate_limit` | tenacity 指数退避 1s→2s→4s...，最多 3 次，Retry-After header 优先 |
| `overloaded` | tenacity 指数退避 2s→4s→8s...，最多 5 次 |
| `context_length` | 不重试，抛 `FatalLLMError`，通知主循环触发硬截断/压缩 |
| `content_filter` | 不重试，yield ErrorMessage + StopEvent(stop_reason="permission_denied") |
| `transient` | tenacity 指数退避 1s→2s→4s...，最多 3 次 |

---

## 10. StopReason 跨 Provider 映射

```python
# llm/normalize.py

ANTHROPIC_STOP_MAP = {
    "end_turn": "end_turn",
    "max_tokens": "max_turns",
    "stop_sequence": "end_turn",
    "tool_use": "end_turn",       # 由 query() 继续循环，不出终端
}

OPENAI_STOP_MAP = {
    "stop": "end_turn",
    "length": "max_turns",
    "tool_calls": "end_turn",     # 由 query() 继续循环，不出终端
    "content_filter": "permission_denied",
}


def normalize_stop_reason(
    provider: ModelProvider, raw_reason: str
) -> tuple[str, str | None]:
    """
    返回 (stop_reason, error_detail)。
    - "end_turn" / "tool_use" → query() 检查是否有待执行的 tool_use
    - "max_turns" → query() 检查是否触发 max_output_tokens 恢复
    - "permission_denied" / "error" → 直接终止
    """
    match provider:
        case ModelProvider.ANTHROPIC:
            mapped = ANTHROPIC_STOP_MAP.get(raw_reason, "end_turn")
            return mapped, None
        case ModelProvider.OPENAI:
            mapped = OPENAI_STOP_MAP.get(raw_reason, "end_turn")
            detail = f"openai finish_reason={raw_reason}" if raw_reason == "content_filter" else None
            return mapped, detail
    return "end_turn", None
```

---

## 11. 与 query() 主循环的集成

```python
# query/loop.py —— 伪代码

from agent_core.llm.base import get_client
from agent_core.llm.events import InternalEvent, StopEvent, ToolUse, AssistantText, TokenCounts, ErrorMessage
from agent_core.llm.tool_schema import tools_for_anthropic, tools_for_openai

async def query(self, prompt: str) -> AsyncGenerator[AgentMessage, None]:
    turn_index = 0
    messages = _build_initial_messages(prompt)   # 含 system prompt 注入
    total_tokens = 0
    tool_uses = 0

    model_id = self._config.model_id
    client = get_client(model_id, self._config)

    # tool schema 按 provider 生成
    match model_id.provider:
        case ModelProvider.ANTHROPIC:
            tools = tools_for_anthropic(self._config.tools)
        case ModelProvider.OPENAI:
            tools = tools_for_openai(self._config.tools)

    while turn_index < self._config.max_turns:
        turn_index += 1

        # 上下文富化作为首条 user message（如果有）
        context_text = _enrich_context()
        if context_text:
            messages.append({"role": "user", "content": context_text})

        has_tool_use_this_turn = False
        stop_event: StopEvent | None = None

        async for event in client.stream_messages(
            messages=messages,
            tools=tools,
            system=None,  # system 已在 messages_for_xxx 中处理
            max_tokens=4096,
            turn_index=turn_index,
        ):
            match event:
                case AssistantText(content=text):
                    yield text

                case ToolUse() as tool_use:
                    yield tool_use          # 给调用方
                    has_tool_use_this_turn = True
                    tool_uses += 1
                    # 工具执行 + 结果，插入 messages
                    await _execute_and_append_tool(tool_use, messages)

                case TokenCounts(input_tokens=n, output_tokens=m):
                    total_tokens += n + m
                    # 预算检查

                case StopEvent() as stop:
                    stop_event = stop

                case ErrorMessage() as error:
                    yield error
                    return  # 致命错误

        # --- 循环结束判断 ---
        if has_tool_use_this_turn:
            continue  # 有 tool_use → 继续下一轮
        else:
            yield DoneMessage(stop_reason="end_turn", total_tokens=total_tokens)
            return
```

---

## 12. 结构化日志 (`logging/tracer.py`)

每个 turn 的结构化日志条目：

```python
{
    "trace_id": "uuid4-贯穿整个Agent生命周期",
    "turn_index": 3,
    "provider": "anthropic",
    "model": "claude-sonnet-4-5",
    "input_tokens": 5432,
    "output_tokens": 876,
    "cache_read_tokens": 4200,   # 仅 Anthropic
    "cache_write_tokens": None,  # 仅 Anthropic
    "tool_uses": 2,
    "tool_names": ["Read", "Grep"],
    "latency_ms": 2340,
    "error_category": None,      # "rate_limit" / "overloaded" / ...
    "retry_count": 0,
}
```

Tracer 通过消费 `InternalEvent` 流来自动填充这些字段：`StreamStart` → 重置 turn 统计，`TokenCounts` → 填充 token 字段，`StopEvent` → 计算 latency 并写入日志。

---

## 13. 实现顺序

| 步骤 | 文件 | 预估 | 依赖 |
|------|------|------|------|
| 1 | `models.py` — ModelProvider / ModelId | 0.25d | 无 |
| 2 | `llm/events.py` — InternalEvent 类型 | 0.25d | 1 |
| 3 | `llm/base.py` — BaseClient + get_client 工厂 | 0.25d | 1, 2 |
| 4 | `llm/normalize.py` — 消息双向转换 | 0.5d | 1, 2 |
| 5 | `llm/tool_schema.py` — Tool schema 转换 | 0.25d | 1 |
| 6 | `llm/errors.py` — 错误分类 + 重试策略 | 0.25d | 1 |
| 7 | `llm/anthropic.py` — AnthropicClient | 0.25d | 3, 4, 5, 6 |
| 8 | `llm/openai.py` — OpenAIClient | 0.5d | 3, 4, 5, 6 |
| 9 | `logging/tracer.py` — 结构化日志 | 0.25d | 2 |
| 10 | `tests/` — 单元 + mock 集成测试 | 0.25d | 1-9 |
| **合计** | | **3 天** | |

---

## 14. 测试策略

### 单元测试

- `test_model_id.py`: `ModelId.parse()` 各种格式（有前缀 / 无前缀 / 无效 provider / 无效模型名）
- `test_normalize.py`: 消息双向转换正确性
  - `messages_for_openai()` 输入 Anthropic content-block → 输出 OpenAI messages 格式
  - 含 tool_use / tool_result 的复杂消息序列
- `test_tool_schema.py`: Anthropic vs OpenAI tool schema 格式差异
- `test_errors.py`: `classify_error()` per provider 分类正确性

### Mock 集成测试（不依赖真实 API）

- `MockAnthropicStream`: 模拟 Anthropic SDK 流式事件序列 → 验证 AnthropicClient 输出正确的 InternalEvent
- `MockOpenAIStream`: 模拟 OpenAI SDK 流式 chunk 序列 → 验证 OpenAIClient 输出正确的 InternalEvent
- `test_multiprovider_integration`: 同一个 `query()` prompt 分别经 AnthropicClient 和 OpenAIClient mock → 验证产出相同的 AgentMessage 序列

### vcrpy 录制回放（需要一次真实 API key）

- Anthropic: 端到端 turn flow (text + tool_use + text)
- OpenAI: 端到端 turn flow (text + tool_call + text)
