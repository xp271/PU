# Item 5.5: 自动压缩 (上下文超限 LLM 摘要) —— 实现计划

> 目标：上下文接近模型窗口上限时，用 LLM 摘要压缩旧轮次，保留关键信息的同时减小上下文体积。

---

## 1. 架构总览

### 1.1 压缩在 query() 主循环中的位置

```
当前流程 (loop.py turn 循环):
  estimated = estimate_tokens_from_messages(messages, system)
  if budget.needs_truncation(estimated):         ← 80% 阈值
      messages = hard_truncate(...)              ← 直接丢弃旧轮次
  → 调用 LLM

新的流程 (压缩优先于截断):
  estimated = estimate_tokens_from_messages(messages, system)
  
  if budget.needs_truncation(estimated):          ← 统一 80% 阈值
      if 压缩可用 (enabled + 有旧轮次可压缩):
          yield CompactMessage("started")
          try:
              messages = await compress(...)      ← 优先尝试压缩
              estimated = re-estimate
          except CompressionError:
              # 压缩失败（含重试耗尽）→ 回退到截断
              messages = hard_truncate(...)
      else:
          # 压缩禁用或无旧轮次 → 直接截断
          messages = hard_truncate(...)
  → 调用 LLM
```

**关键设计决策**：压缩和截断共享同一个触发阈值（context window 的 80%）。到达阈值时，**总是先尝试压缩**。压缩仅在以下情况才回退到硬截断：
1. `CompressionConfig.enabled = False`（用户主动关闭）
2. 轮次太少没有可压缩的旧轮次（`_partition_rounds` ≤ `keep_recent_rounds`）
3. 压缩失败且所有重试耗尽（LLM 错误 / 验证不通过）

### 1.2 新增文件与模块

```
src/agent_core/compression/
├── __init__.py              # 公开导出: compress(), CompactMessage, CompressionConfig
├── config.py                # CompressionConfig Pydantic 模型
├── trigger.py               # 触发条件判断 (_should_compress)
├── summarizer.py            # compress() 核心实现
└── consistency.py           # 压缩后一致性验证
```

### 1.3 影响/修改的现有文件

| 文件 | 修改内容 |
|------|----------|
| `models.py` | 新增 `CompressionConfig`、`CompactMessage`；`AgentConfig` 增加 `compression` 字段；`AgentMessage` 联合类型增加 `CompactMessage` |
| `query/loop.py` | 在截断检查之前插入压缩逻辑；yield `CompactMessage` |
| `__init__.py` | 导出 `CompactMessage`、`CompressionConfig` |

---

## 2. 数据模型设计

### 2.1 CompressionConfig (models.py)

```python
class CompressionConfig(BaseModel):
    """自动压缩配置。所有字段可选，有合理默认值。

    压缩触发条件与截断共享同一阈值（context window 的 80%，由 TokenBudget.truncation_threshold 定义）。
    到达阈值时优先尝试压缩，压缩失败才回退到硬截断。
    """

    enabled: bool = Field(
        default=True,
        description="启用自动压缩（关闭时到达截断阈值直接硬截断）",
    )
    keep_recent_rounds: int = Field(
        default=3,
        ge=1,
        le=10,
        description="保留最近的 N 轮完整对话不被压缩",
    )
    summary_max_tokens: int = Field(
        default=2048,
        ge=512,
        le=8192,
        description="摘要响应的最大 token 数",
    )
    tool_result_preview_chars: int = Field(
        default=200,
        ge=50,
        le=500,
        description="压缩提示中每个 tool_result 的截断预览字符数",
    )
    max_retries: int = Field(
        default=2,
        ge=0,
        le=5,
        description="压缩验证失败时的最大重试次数。0 表示不重试，失败直接回退到截断",
    )
```

**AgentConfig 新增字段**：

```python
class AgentConfig(BaseModel):
    # ... 现有字段不变 ...
    compression: CompressionConfig = Field(
        default_factory=CompressionConfig,
        description="自动压缩配置",
    )
```

### 2.2 CompactMessage (models.py)

新的 `AgentMessage` 变体，用于通知调用方压缩事件：

```python
class CompactMessage(BaseModel):
    """压缩事件通知消息。"""
    type: Literal["compact"] = "compact"
    status: Literal["started", "completed", "skipped"]
    rounds_compressed: int = 0          # 被压缩的轮次数
    rounds_preserved: int = 0           # 保留的轮次数
    tokens_before: int = 0              # 压缩前估算 token 数
    tokens_after: int = 0               # 压缩后估算 token 数
    compression_ratio: float = 0.0      # tokens_after / tokens_before
    summary: str | None = None          # 生成的摘要文本（status=completed 时）
    retries_used: int = 0               # 实际使用的重试次数（0 = 一次通过）
    error_detail: str | None = None     # 失败原因（status=skipped 时）
```

**AgentMessage 联合类型更新**：

```python
AgentMessage = Annotated[
    AssistantText | ToolUse | ToolResult | ErrorMessage | DoneMessage | CompactMessage,
    Field(discriminator="type"),
]
```

---

## 3. 核心实现

### 3.1 可压缩性判断 (`compression/trigger.py`)

```python
def can_compress(
    messages: list[dict[str, Any]],
    config: CompressionConfig,
) -> bool:
    """
    判断当前是否有可压缩的旧轮次。不检查阈值——阈值判断由 budget.needs_truncation() 统一负责。

    条件（全部满足才可压缩）：
    1. config.enabled == True
    2. 轮次数 > config.keep_recent_rounds（有旧轮次可压缩）
    """
    if not config.enabled:
        return False
    rounds = _partition_rounds(messages)
    return len(rounds) > config.keep_recent_rounds
```

边界情况：
- `enabled=False` → 不可压缩，走 hard_truncate 路径
- 总轮次 ≤ `keep_recent_rounds` → 不可压缩，走 hard_truncate（无旧轮次可摘要）

### 3.2 压缩函数 (`compression/summarizer.py`)

```python
async def compress(
    client: BaseClient,
    messages: list[dict[str, Any]],
    system: str | None,
    model_id: ModelId,
    config: CompressionConfig,
) -> tuple[list[dict[str, Any]], str, list[CompressionAttempt]]:
    """
    压缩旧轮次为一段摘要。内部含重试循环。

    返回: (压缩后的消息列表, 最终摘要文本, 所有尝试记录)

    算法步骤:
    1. 按 role="user" 边界将 messages 分为 rounds
    2. 将 rounds 分为两组: old_rounds + recent_rounds (保留 keep_recent_rounds 个)
    3. 如果 old_rounds 为空，直接返回原列表（无事可压缩）
    4. 重试循环 (最多 max_retries + 1 次尝试):
       a. 构建压缩提示词 → 调用 LLM client 生成摘要
       b. 验证一致性（见 3.3）
       c. 验证通过 → 返回 [摘要消息] + recent_rounds
       d. 验证不通过 → 记录失败原因，调整提示词（附加上一轮的验证错误信息），重试
    5. 所有重试耗尽 → 抛出 CompressionError（调用方 fallback 到 hard_truncate）
    """
```

**步骤 1: 轮次分区**

复用 `hard_truncate()` 中已有的分区逻辑（以 `role: "user"` 为轮次边界），或将其提取为独立函数 `_partition_rounds(messages) -> list[list[dict]]` 放入共享位置。

```python
def _partition_rounds(
    messages: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """按 user message 边界将消息列表分区为轮次列表。每个轮次包含一个 user 消息及后续 assistant 消息。"""
    rounds: list[list[dict[str, Any]]] = []
    current_round: list[dict[str, Any]] = []

    for msg in messages:
        if msg.get("role") == "user" and current_round:
            rounds.append(current_round)
            current_round = []
        current_round.append(msg)

    if current_round:
        rounds.append(current_round)

    return rounds
```

**步骤 2: 分组**

```python
if len(rounds) <= config.keep_recent_rounds:
    return messages, ""  # 无事可压缩

old_rounds = rounds[:-config.keep_recent_rounds]
recent_rounds = rounds[-config.keep_recent_rounds:]
```

**步骤 3: 构建压缩提示词**

```python
def _build_compression_prompt(
    old_rounds: list[list[dict[str, Any]]],
    preview_chars: int,
) -> str:
    """
    构建给 LLM 的压缩提示词。

    格式:
    <compression_request>
    Please summarize the following conversation rounds. For each round, capture:
    - The user's intent/goal
    - Key tool calls (name + essential inputs)
    - Key findings/results (first {preview_chars} chars of output)
    - Any important decisions or state changes

    <conversation_to_summarize>
    [Round 1]
    User: ...
    Assistant (tool_use: Read, input: {"file_path": "..."})
    Tool result: ...
    ...

    [Round 2]
    ...
    </conversation_to_summarize>

    Produce a concise summary. Focus on information that is still relevant to
    the ongoing task. Do not repeat information already present in the system
    prompt (date, git status, environment).
    </compression_request>
    """
```

**tool_result 短缩策略**：每个 `tool_result` block 的 `content` 截断到 `preview_chars` 字符（仅当为字符串时），附 `[truncated, original N chars]` 标记。非字符串 content（如结构化元数据）用 `repr()` 后再截断。

**步骤 4: 带重试的 LLM 调用**

```python
@dataclass
class CompressionAttempt:
    """单次压缩尝试的记录。"""
    attempt: int
    summary: str
    verification: CompressionConsistency
    tokens_consumed: int  # 本次尝试消耗的 token 总数
    error: str | None = None


async def _call_llm_for_summary(
    client: BaseClient,
    prompt: str,
    system: str | None,
    max_tokens: int,
) -> tuple[str, int, int]:
    """调用 LLM 生成摘要，返回 (summary, input_tokens, output_tokens)。"""
    compression_messages = [
        {"role": "user", "content": [{"type": "text", "text": prompt}]},
    ]
    empty_tools = client.__class__.convert_tools([])

    summary_parts: list[str] = []
    input_tokens = 0
    output_tokens = 0

    async for event in client.stream_messages(
        messages=compression_messages,
        tools=empty_tools,
        system=system,
        max_tokens=max_tokens,
        turn_index=0,
    ):
        match event:
            case AssistantText(content=text):
                summary_parts.append(text)
            case TokenCounts(input_tokens=it, output_tokens=ot):
                input_tokens = it
                output_tokens = ot
            case ErrorMessage(message=err):
                raise CompressionError(f"LLM summarization failed: {err}")
            case StopEvent():
                break

    return "".join(summary_parts), input_tokens, output_tokens


# compress() 内部的重试循环:

attempts: list[CompressionAttempt] = []
last_error: str | None = None

for attempt_num in range(config.max_retries + 1):
    # 构建提示词（重试时附加验证失败信息）
    prompt = _build_compression_prompt(
        old_rounds=old_rounds,
        preview_chars=config.tool_result_preview_chars,
        previous_errors=last_error,  # 第 1 次为 None，后续为验证错误
    )

    # 调用 LLM
    summary, input_tok, output_tok = await _call_llm_for_summary(
        client, prompt, system, config.summary_max_tokens,
    )

    # 组装压缩后的消息
    compressed = _build_compressed_messages(summary, recent_rounds)

    # 验证一致性
    verification = verify_compression(
        original_messages=messages,
        compressed_messages=compressed,
        old_rounds=old_rounds,
        recent_rounds=recent_rounds,
        summary=summary,
    )

    attempts.append(CompressionAttempt(
        attempt=attempt_num + 1,
        summary=summary,
        verification=verification,
        tokens_consumed=input_tok + output_tok,
    ))

    if verification.valid:
        # 验证通过，返回成功结果
        return compressed, summary, attempts

    # 验证不通过 → 记录错误，准备重试
    last_error = "; ".join(verification.errors)
    logger.warning(
        f"Compression verification failed (attempt {attempt_num + 1}/{config.max_retries + 1}): "
        f"{last_error}. {'Retrying...' if attempt_num < config.max_retries else 'All retries exhausted.'}"
    )

# 所有重试耗尽
raise CompressionError(
    f"Compression verification failed after {config.max_retries + 1} attempt(s): {last_error}"
)
```

**重试时的提示词调整**：

`_build_compression_prompt` 在 `previous_errors` 非空时，在提示词末尾追加验证反馈：

```
<compression_request>
... (原有内容) ...
</compression_request>

<previous_attempt_feedback>
Your previous summarization was rejected because:
- {previous_errors}

Please fix these issues in your new summary.
</previous_attempt_feedback>
```

**步骤 5: 组装压缩后的消息列表**

```python
summary_message = {
    "role": "user",
    "content": [
        {
            "type": "text",
            "text": (
                "<conversation_summary>\n"
                f"{summary}\n"
                "</conversation_summary>\n\n"
                "The above is a summary of earlier conversation rounds. "
                "The following rounds are the most recent unsummarized conversation."
            ),
        }
    ],
}

# 将摘要消息下的各轮展开回扁平消息列表
compressed_messages = [summary_message] + [
    msg for round_msgs in recent_rounds for msg in round_msgs
]
return compressed_messages, summary
```

### 3.3 一致性验证 (`compression/consistency.py`)

```python
@dataclass
class CompressionConsistency:
    """压缩一致性检查结果。"""
    valid: bool
    system_preserved: bool             # system prompt 不变（设计保证）
    tool_uses_traceable: bool          # 是否有 tool_use 丢失
    compressed_rounds: int             # 压缩的轮次数量
    preserved_rounds: int              # 保留的轮次数量
    warnings: list[str]                # 非致命警告
    errors: list[str]                  # 致命错误


def verify_compression(
    original_messages: list[dict[str, Any]],
    compressed_messages: list[dict[str, Any]],
    old_rounds: list[list[dict[str, Any]]],
    recent_rounds: list[list[dict[str, Any]]],
    summary: str,
) -> CompressionConsistency:
    """
    验证压缩后的一致性。

    检查项:
    1. system prompt 未被修改 → 压缩不改 system，始终 True
    2. tool_use 列表可追溯 → 统计 old_rounds 和 summary 中的 tool_use
    3. recent_rounds 消息完全保留 → 逐条对比
    4. 摘要非空 → 空摘要视为警告
    5. 压缩确实减少了消息数 → old_rounds 消息数 > 1（摘要消息）
    """
```

验证失败策略（在 `compress()` 重试循环内处理）：
- 致命错误（如 recent_rounds 被意外修改、摘要为空导致无信息保留）→ `valid=False`，记录到 `errors` 列表 → 重试循环捕获，反馈给 LLM 后重试
- 警告（如摘要过短但非空、部分 tool_use 未覆盖）→ `valid=True` 但 `warnings` 非空 → 记录日志，继续使用压缩结果（这些是质量警告而非正确性问题）
- 重试耗尽后仍有致命错误 → `compress()` 抛出 `CompressionError` → 调用方（loop.py）catch 并 fallback 到 `hard_truncate`

### 3.4 集成到 query() (`query/loop.py`)

在截断检查处插入压缩逻辑。压缩和截断共享 `budget.needs_truncation()` 作为统一触发点，**压缩优先，截断兜底**。

```python
# 替换现有的截断块 (loop.py 约 L109-L113)
estimated = estimate_tokens_from_messages(messages, system)

# ---- 修改后的截断/压缩块 ----
if budget.needs_truncation(estimated):
    # 先判断是否可以用压缩代替截断
    if can_compress(messages, config.compression):
        round_count = len(_partition_rounds(messages))

        yield CompactMessage(
            status="started",
            rounds_compressed=round_count - config.compression.keep_recent_rounds,
            rounds_preserved=config.compression.keep_recent_rounds,
            tokens_before=estimated,
        )

        try:
            messages, summary, attempts = await compress(
                client=client,
                messages=messages,
                system=system,
                model_id=model_id,
                config=config.compression,
            )

            new_estimated = estimate_tokens_from_messages(messages, system)
            ratio = new_estimated / estimated if estimated > 0 else 0

            # 累加压缩调用消耗的 token（不计入 turn 数）
            for attempt in attempts:
                budget.total_input_tokens += attempt.input_tokens_consumed or 0
                budget.total_output_tokens += attempt.output_tokens_consumed or 0

            yield CompactMessage(
                status="completed",
                rounds_compressed=round_count - config.compression.keep_recent_rounds,
                rounds_preserved=config.compression.keep_recent_rounds,
                tokens_before=estimated,
                tokens_after=new_estimated,
                compression_ratio=ratio,
                summary=summary,
                retries_used=len(attempts) - 1,
            )

            estimated = new_estimated
            context.messages = messages

            # 压缩成功，但可能仍未降到阈值以下 → 继续检查
            if budget.needs_truncation(estimated):
                messages = hard_truncate(
                    messages, max_tokens=budget.truncation_threshold
                )
                context.messages = messages

        except CompressionError as e:
            # 压缩失败（含重试耗尽）→ 回退到截断
            logger.warning(
                f"Compression failed after all retries, "
                f"falling back to hard truncation: {e}"
            )
            yield CompactMessage(
                status="skipped",
                tokens_before=estimated,
                error_detail=str(e),
            )
            messages = hard_truncate(
                messages, max_tokens=budget.truncation_threshold
            )
            context.messages = messages

    else:
        # 压缩不可用（禁用或无旧轮次）→ 直接截断
        messages = hard_truncate(
            messages, max_tokens=budget.truncation_threshold
        )
        context.messages = messages
```

**关键细节**：
- `budget.needs_truncation(estimated)` 是统一入口——无论压缩还是截断都从这里触发
- `can_compress()` 仅检查 enabled + 轮次数，不重复检查阈值
- 压缩成功后仍需检查是否需要额外截断（压缩效果不保证降到 80% 以下）
- 压缩调用不计入 `budget.turn_count`；token 消耗直接累加到 `budget.total_input/output_tokens`

---

## 4. 与截断系统的关系

```
压缩与截断的行为矩阵（统一阈值 = context_window × 0.8）：

| 场景                               | 阈值触发 | 压缩   | 截断   | 结果                              |
|------------------------------------|---------|--------|--------|-----------------------------------|
| tokens < 0.8×CW                   | No      | —      | No     | 无操作                            |
| tokens ≥ 0.8×CW                   | Yes     | —      | —      | 进入处理流程：                     |
|   ├ 压缩已启用 + 有旧轮次           | —       | 尝试   | —      | 压缩优先：                         |
|   │   ├ 一次验证通过               | —       | 成功   | No     | 仅压缩，无需截断                   |
|   │   ├ 验证失败 → 重试通过         | —       | 成功   | No     | 最后一次重试通过                   |
|   │   ├ 重试全部耗尽               | —       | 失败   | Yes    | 回退到截断（处理原消息）            |
|   │   └ 压缩成功但仍 > 0.8×CW      | —       | 成功   | Yes    | 压缩后再截断（双重削减）            |
|   └ 压缩禁用 或 轮次太少            | —       | 跳过   | Yes    | 直接截断                          |
| config.compression.enabled=False   | Yes     | —      | Yes    | 直接截断（退回现有行为）            |
```

**设计要点**：
- **统一入口**：压缩和截断由 `budget.needs_truncation()`（80% 阈值）统一触发
- **压缩优先**：到达阈值后，只要压缩可用，总是先尝试压缩
- **截断兜底**：压缩不可用、压缩失败、或压缩后仍超限时，截断作为最终保障
- 关闭压缩（`enabled=False`）时行为完全回退到现在的仅截断模式

---

## 5. 公开 API 变更

### 5.1 `__init__.py` 增量导出

```python
from .models import (
    # ... 现有导出 ...
    CompactMessage,      # 新增
    CompressionConfig,   # 新增
)

__all__ = [
    # ... 现有 ...
    "CompactMessage",
    "CompressionConfig",
]
```

### 5.2 `Agent.stream()` 行为变更

调用方通过 `Agent.stream()` 会额外收到 `CompactMessage` 事件：

```python
async for msg in agent.stream(prompt):
    match msg:
        case CompactMessage(status="started"):
            print(f"[压缩开始...]")
        case CompactMessage(status="completed", summary=summary):
            print(f"[压缩完成: {summary[:100]}...]")
        case CompactMessage(status="skipped", error_detail=err):
            print(f"[压缩跳过: {err}]")
        case _:
            # 现有处理逻辑
```

调用方无需修改现有代码——`CompactMessage` 匹配不到现有 case 分支时会被忽略（Python match 的 default case `_` 行为），完全向后兼容。

### 5.3 `AgentConfig` 使用示例

```python
from agent_core import Agent, AgentConfig, CompressionConfig

# 使用默认压缩配置
config = AgentConfig(model="anthropic/claude-sonnet-4-5")
agent = Agent(config)

# 自定义压缩配置
config = AgentConfig(
    model="anthropic/claude-opus-4-7",
    compression=CompressionConfig(
        keep_recent_rounds=5,      # 保留更多轮次
        summary_max_tokens=4096,   # 更长的摘要
        max_retries=3,             # 更多重试次数
    ),
)

# 完全禁用压缩（仅用截断）
config = AgentConfig(
    compression=CompressionConfig(enabled=False),
)
```

---

## 6. 测试策略

### 6.1 单元测试 (`tests/unit/test_compression.py`)

| 测试 | 说明 |
|------|------|
| `test_can_compress_all_conditions_met` | enabled=True, rounds > keep → True |
| `test_can_compress_disabled` | enabled=False → False |
| `test_can_compress_few_rounds` | rounds ≤ keep_recent_rounds → False |
| `test_can_compress_empty_messages` | 消息列表为空 → False |
| `test_partition_rounds_basic` | 基本轮次分区 |
| `test_partition_rounds_tool_results` | 含 tool_result user 消息的分区 |
| `test_partition_rounds_single_round` | 单轮消息 |
| `test_partition_rounds_empty` | 空列表 |
| `test_build_compression_prompt` | 验证提示词格式包含必要字段 |
| `test_build_compression_prompt_tool_result_truncation` | tool_result 预览截断 |
| `test_verify_compression_valid` | 有效压缩的一致性验证通过 |
| `test_verify_compression_recent_rounds_mismatch` | recent_rounds 被修改 → 致命错误 |
| `test_verify_compression_empty_summary` | 空摘要 → 致命错误，valid=False |
| `test_compress_no_old_rounds` | old_rounds 为空 → 返回原列表 |
| `test_compression_ratio_calculation` | compression_ratio 计算正确 |
| `test_compact_message_serialization` | CompactMessage JSON 序列化/反序列化 |
| `test_compress_first_attempt_succeeds` | 验证一次通过，retries_used=0 |
| `test_compress_retry_then_succeed` | 第 1 次验证失败，第 2 次通过 |
| `test_compress_all_retries_exhausted` | 所有重试失败，抛出 CompressionError |
| `test_compress_retry_prompt_includes_feedback` | 重试时提示词包含前次验证错误 |
| `test_compress_max_retries_zero` | max_retries=0 → 一次失败即抛异常 |

### 6.2 Mock LLM 集成测试 (`tests/integration/test_compression.py`)

使用一个返回固定摘要文本的 MockClient（不依赖真实 API）：

| 测试 | 说明 |
|------|------|
| `test_compress_with_mock_client` | 模拟 LLM 返回固定摘要，验证压缩后消息结构 |
| `test_compression_in_query_loop` | 在 query() 循环中触发压缩，验证消息流 |
| `test_compression_retry_then_succeed` | mock client 第 1 次返回空摘要，第 2 次返回有效摘要 → 压缩成功 |
| `test_compression_retries_exhausted_fallback` | mock client 所有重试都返回无效摘要 → fallback 到截断 |
| `test_compression_not_counted_in_turns` | 压缩（含重试）不推进 turn_count |
| `test_compression_tokens_accumulated` | 压缩消耗的 token 正确累加到 total |
| `test_compress_disabled_direct_truncation` | enabled=False → 80% 阈值直接触发截断 |
| `test_compress_few_rounds_direct_truncation` | 轮次太少无旧轮次 → 直接截断 |

### 6.3 vcrpy 录制回放测试 (`tests/e2e/test_compression.py`)

需一次真实 API key 录制：

| 测试 | 说明 |
|------|------|
| `test_full_compression_flow` | 构造超长对话 → 触发压缩 → 验证压缩后对话可继续 |

---

## 7. 实现步骤与预估工时

| 步骤 | 文件 | 预估 | 说明 |
|------|------|------|------|
| 1 | `models.py` | 0.25d | 新增 `CompressionConfig`、`CompactMessage`；`AgentConfig` 加字段；`AgentMessage` 联合加变体 |
| 2 | `compression/config.py` | 自动 | 仅 re-export（模型已在 models.py 中定义） |
| 3 | `compression/trigger.py` | 0.25d | `should_compress()` + `_count_rounds()` + `_partition_rounds()`（从 truncate.py 提取共用） |
| 4 | `compression/summarizer.py` | 0.5d | `compress()` 核心：提示词构建 + LLM 调用 + 消息重组 |
| 5 | `compression/consistency.py` | 0.25d | `verify_compression()` 一致性检查 |
| 6 | `compression/__init__.py` | — | 模块公开导出 |
| 7 | `query/loop.py` | 0.25d | 插入压缩逻辑；yield `CompactMessage` |
| 8 | `query/truncate.py` | 0.1d | 可选：提取 `_partition_rounds()` 为共享函数 |
| 9 | `__init__.py` | — | 新增导出 |
| 10 | 单元测试 | 0.35d | `test_compression.py` 全部 21 个用例 |
| 11 | 集成测试 | 0.25d | MockClient 集成测试 9 个用例 |
| 12 | e2e 测试 | 0.15d | vcrpy 录制回放 1 个用例 |
| **总计** | | **~2d** | |

---

## 8. 风险与边界条件

### 8.1 风险

| 风险 | 缓解 |
|------|------|
| 摘要质量差导致上下文丢失关键信息 | `keep_recent_rounds` 保证最近轮次原样保留；摘要提示词设计上要求记录关键 tool_use 和决策 |
| 压缩 API 调用增加延迟 | 压缩仅在接近窗口上限时触发（频率低）；调用方可通过 `CompactMessage` 感知进度 |
| 压缩调用本身消耗 token，可能加重超限 | 压缩调用的 token 与节省的 token 相比微不足道（摘要 2K vs 可能节省 50K+） |
| 重试消耗额外 token 和延迟 | 默认最多 2 次重试，总量可控；每次重试复用相同的 `old_rounds` 只改变提示词尾部反馈 |
| 重试提示词反馈导致 LLM 越改越差 | 反馈仅描述"哪里错了"，不给出"该怎么改"——避免过度约束；如果重试耗尽，截断兜底 |
| 嵌套子 Agent 的压缩行为 | 子 Agent 独立拥有消息列表和配置，压缩自然隔离 |

### 8.2 边界条件

- **消息列表为空或仅 1-2 轮**：`_partition_rounds` 返回 ≤ keep_recent_rounds 轮，`should_compress` 返回 False
- **所有消息都在 keep_recent_rounds 内**：old_rounds 为空，`compress()` 直接返回原列表
- **系统提示词非常大（接近窗口一半）**：压缩不影响 system，截断也不影响 system（两者都是缩小 messages 体积）
- **压缩后 messages 仍然超限**：truncation 作为兜底继续执行
- **压缩过程中 abort_signal 触发**：通过 `ToolUseContext.abort_signal` 检查，抛 `CompressionError` 后走截断路径

---

## 9. 设计决策记录

1. **压缩和截断是串联关系，不是替代关系**：关闭压缩时行为完全回退到当前仅截断模式。
2. **压缩与截断共享 80% 阈值，压缩优先**：不设独立的压缩阈值。`budget.needs_truncation()` 是统一触发点。到达阈值后先尝试压缩，压缩不可用/失败时才回退到截断。不存在"80-85% 区间绕过压缩直接截断"的窗口。
3. **压缩调用不占用 max_turns**：压缩是基础设施操作，不应消耗用户的 turn 预算。
4. **压缩结果以 user message 注入**：用 `<conversation_summary>` 标签包装，LLM 从下一轮开始就能看到。
5. **_partition_rounds 提取为共享函数**：`hard_truncate` 和 `compress` 都需要轮次分区，复用是必要的。
6. **不使用 query() 递归调用**：直接调用 `client.stream_messages()` 避免递归和工具调用闭环。
7. **CompactMessage 是独立消息类型**：不走 `ErrorMessage` 通道（压缩失败 ≠ Agent 出错），调用方可选择关注或忽略。
8. **验证失败先重试再回退**：验证失败不会立即跳到截断。失败信息作为反馈注入下一次压缩提示词，让 LLM 修正。默认最多重试 2 次（共 3 次尝试）。只有重试耗尽后才 fallback 到 `hard_truncate`。`max_retries=0` 可恢复为"一次失败即放弃"的行为。
9. **重试提示词增量更新**：重试时不改变原始提示词结构，仅在末尾追加 `<previous_attempt_feedback>` 块，告知 LLM 上次验证失败的原因，要求修正。
