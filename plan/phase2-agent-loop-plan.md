# Phase 2 条目 #4: Agent 主循环 (query 系统) —— 详细实现计划

> 对应 TodoList 条目 #4，预估 4 天。
> 目标：`query()` 异步生成器，完整 turn 循环，token 预算管理，上下文硬截断，
> 透传 `AgentMessage` 流给 `Agent.run()` / `Agent.stream()` 消费。
> **不含自动压缩**（独立条目 #5.5）。

---

## 1. 架构总览

### 1.1 在系统中的地位

```
                        ┌─────────────────────────────┐
                        │      Agent.run(prompt)       │
                        │  聚合 AgentMessage →          │
                        │  AgentResult                 │
                        └───────────┬─────────────────┘
                                    │
                        ┌───────────▼─────────────────┐
                        │    Agent.stream(prompt)      │
                        │  async for msg in query(...): │
                        │      yield msg               │
                        └───────────┬─────────────────┘
                                    │
          ┌─────────────────────────▼──────────────────────┐
          │              query() 异步生成器                  │
          │  ┌──────────────────────────────────────────┐  │
          │  │          Turn 循环核心                    │  │
          │  │  LLM 请求 → token 预算 → 工具执行 →        │  │
          │  │  截断检查 → 下一轮 / 停止                  │  │
          │  └──────────────────────────────────────────┘  │
          │       │                    │                    │
          │  ┌────▼─────┐    ┌────────▼────────┐          │
          │  │  budget   │    │    truncate      │          │
          │  │  预算管理  │    │    硬截断        │          │
          │  └──────────┘    └─────────────────┘          │
          │                                               │
          │  ┌─────────────┐ (query 初始化时执行一次)     │
          │  │  context     │                              │
          │  │  上下文富化   │                              │
          │  └─────────────┘                              │
          └────────────────────┬───────────────────────────┘
                               │
          ┌────────────────────▼───────────────────────────┐
          │          BaseClient.stream_messages()           │
          │  ┌──────────────┐  ┌──────────────┐           │
          │  │ Anthropic    │  │ OpenAI       │           │
          │  │ Client       │  │ Client       │           │
          │  └──────────────┘  └──────────────┘           │
          │          ↓ (经由 TurnTracer 包装)               │
          │  AsyncGenerator[InternalEvent, None]           │
          └────────────────────────────────────────────────┘
```

### 1.2 核心设计原则

- **`query()` 是纯函数式的异步生成器**：接收配置 + prompt，产出 `AgentMessage`。不依赖 `Agent` 类实例，可独立测试。
- **主循环只消费 `InternalEvent`**：对 provider 零感知，与 Phase 1 LLM 集成层正交。
- **工具执行预留扩展点**：Phase 2 条目 #4 用最简串行执行，在 `loop.py` 中预留 `_execute_tools()` 函数，条目 #5 实现时只替换这个函数签名不变。
- **token 计数用估算**：不引入 tiktoken/Anthropic tokenizer 依赖，用字符数/4 估算。截断阈值用 80%，足够安全。
- **上下文富化只在 query() 初始化时执行一次**：git status、日期等环境快照在进入 turn 循环之前采集，不在每轮重复执行。参照 cc-haha-main 的做法：`getUserContext()` 和 `getSystemContext()` 均为 `lodash/memoize` 单次求值，`queryLoop()` 内永远不重新获取。理由：(1) git status 是对话开始时的快照，工具执行后文件状态由 `read_file_state` 追踪更精确；(2) 每轮跑 git 子进程浪费延迟且无增量价值。
- **预算耗尽 = 优雅终止**：注入 `max_turns_reached` 上下文后让 LLM 产出最后一轮文本，然后 yield `DoneMessage`。

---

## 2. 模块设计

### 2.1 文件树

```
src/agent_core/query/
├── __init__.py       # 仅导出 query()
├── loop.py           # query() 异步生成器 — turn 循环核心 (~200 行)
├── budget.py         # TokenBudget + 收益递减检测 + context window 映射 (~80 行)
├── context.py        # 上下文富化 — git status, 日期 (~60 行)
└── truncate.py       # 硬截断逻辑 (~70 行)
```

### 2.2 `query/__init__.py`

```python
from .loop import query

__all__ = ["query"]
```

单一职责：导出 `query()`，`Agent.stream()` 只需 `from .query import query`。

### 2.3 `query/loop.py` — Turn 循环核心

#### 2.3.1 `query()` 异步生成器

```python
async def query(
    prompt: str,
    config: AgentConfig,
    trace_id: str,
    permission_callback: PermissionCallback | None = None,
    workspace_root: str | None = None,
) -> AsyncGenerator[AgentMessage, None]:
```

**参数说明**：
| 参数 | 来源 | 说明 |
|------|------|------|
| `prompt` | `Agent.stream(prompt)` 透传 | 用户输入的原始 prompt |
| `config` | `Agent._config` | 所有配置项 |
| `trace_id` | `Agent._trace_id` | uuid4 hex，贯穿整个 Agent 生命周期 |
| `permission_callback` | `Agent._permission_callback` | 权限回调，`None` = 全部允许 |
| `workspace_root` | 暂不传入（Phase 2 后期加入） | 工作区安全边界 |

#### 2.3.2 Turn 循环完整流程

```
┌──────────────────────────────────────────────────┐
│  1. 初始化 (仅执行一次)                           │
│     - 上下文富化 (git status, 日期)  ← 仅此一次  │
│     - 构建 system 参数 (环境上下文注入 system)    │
│     - 构建首条 user message (仅含用户 prompt)     │
│     - 创建 ToolUseContext                         │
│     - 获取 BaseClient + 转换 tool schema          │
│     - 初始化 TokenBudget                          │
│     - 初始化 messages 列表                        │
└──────────────────┬───────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────┐
│  2. Turn 循环 (for turn_index in 1..max_turns)   │
│     ┌──────────────────────────────────┐         │
│     │ a. 预算检查                      │         │
│     │    - is_exhausted()? → stop      │         │
│     │    - needs_truncation()? → 截断  │         │
│     ├──────────────────────────────────┤         │
│     │ b. LLM 调用                      │         │
│     │    - client.stream_messages()    │         │
│     │    - TurnTracer.wrap() 包装      │         │
│     │    - 消费 InternalEvent 流       │         │
│     ├──────────────────────────────────┤         │
│     │ c. 事件处理                      │         │
│     │    StreamStart  → (内部消费)     │         │
│     │    AssistantText → accumulate + yield │   │
│     │    ToolUse      → accumulate + yield │   │
│     │    TokenCounts  → budget.record()    │   │
│     │    StopEvent    → 记录 stop_reason   │   │
│     │    ErrorMessage → yield + DoneMessage│   │
│     ├──────────────────────────────────┤         │
│     │ d. 构建 assistant message        │         │
│     │    - 附加到 messages 列表        │         │
│     ├──────────────────────────────────┤         │
│     │ e. 无 tool_use?                  │         │
│     │    → yield DoneMessage → return  │         │
│     ├──────────────────────────────────┤         │
│     │ f. 工具执行                      │         │
│     │    - _execute_tools_sequential() │         │
│     │    - yield ToolResult            │         │
│     │    - 构建 user tool_result msg   │         │
│     │    - 附加到 messages 列表        │         │
│     ├──────────────────────────────────┤         │
│     │ g. 收益递减检测                  │         │
│     │    - diminishing_returns()?      │         │
│     │      → yield DoneMessage → ret  │         │
│     └──────────────────────────────────┘         │
└──────────────────┬───────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────┐
│  3. 退出 (循环自然结束 = max_turns 耗尽)          │
│     yield DoneMessage(stop_reason="max_turns")     │
└──────────────────────────────────────────────────┘
```

#### 2.3.3 内部辅助函数

**`_build_initial_user_message(prompt) -> dict`**
- 仅包含用户的原始 prompt
- 返回 Anthropic content-block 格式的 user message
- 环境上下文（git status、日期）不在此处，而是通过 `system` 参数传递

**`_build_assistant_message(texts, tool_uses) -> dict`**
- 输入：`list[str]` (累积的 assistant_text 片段) + `list[ToolUse]`
- 输出：`{"role": "assistant", "content": [text_blocks..., tool_use_blocks...]}`
- 空 text 片段跳过（不产生 `{"type": "text", "text": ""}` 块）

**`_build_tool_result_user_message(tool_results) -> dict`**
- 输入：`list[ToolResult]`
- 输出：`{"role": "user", "content": [tool_result_blocks...]}`

**`_execute_tools_sequential(tool_uses, context, permission_callback) -> list[ToolResult]`**

最简串行执行，Phase 2 条目 #5 将被替换为完整管线：

```python
async def _execute_tools_sequential(
    tool_uses: list[ToolUse],
    context: ToolUseContext,
    permission_callback: PermissionCallback | None,
) -> list[ToolResult]:
    results: list[ToolResult] = []
    for tu in tool_uses:
        try:
            # 1. Resolve tool
            tool = get_tool(tu.tool_name)
        except KeyError:
            results.append(ToolResult(
                tool_use_id=tu.tool_use_id,
                tool_name=tu.tool_name,
                content=f"Unknown tool: {tu.tool_name}",
                is_error=True,
            ))
            continue

        # 2. Validate input
        try:
            validated_input = tool.validate_input(tu.tool_input)
        except Exception as e:
            results.append(ToolResult(
                tool_use_id=tu.tool_use_id,
                tool_name=tu.tool_name,
                content=f"Invalid input: {e}",
                is_error=True,
            ))
            continue

        # 3. Permission check
        if permission_callback is not None:
            allowed = await permission_callback(tu.tool_name, tu.tool_input)
            if not allowed:
                results.append(ToolResult(
                    tool_use_id=tu.tool_use_id,
                    tool_name=tu.tool_name,
                    content="Permission denied",
                    is_error=True,
                ))
                continue

        # 4. Execute
        try:
            result = await tool.call(validated_input, context)
            results.append(ToolResult(
                tool_use_id=tu.tool_use_id,
                tool_name=tu.tool_name,
                content=result.content,
                is_error=result.is_error,
            ))
        except Exception as e:
            results.append(ToolResult(
                tool_use_id=tu.tool_use_id,
                tool_name=tu.tool_name,
                content=f"Tool execution error: {e}",
                is_error=True,
            ))

    return results
```

#### 2.3.4 `query()` 异常处理策略

`query()` 是整个 Agent 生命周期的最外层生成器。异常分两类：

| 异常层级 | 处理方式 |
|----------|----------|
| **turn 级异常** (LLM 调用失败、工具执行异常) | try/except 包裹单轮 → `yield ErrorMessage` → `yield DoneMessage(stop_reason="error")` → `return` |
| **致命异常** (未预期的 bug) | 不捕获，自然抛出 → `Agent.stream()` / `Agent.run()` 调用方处理 |

**具体 error 场景**：

| 场景 | 处理 |
|------|------|
| `get_client()` 失败 (无注册 provider) | `yield ErrorMessage` → `yield DoneMessage(error)` → `return` |
| `client.stream_messages()` 连接失败 | 内联重试已由 Provider 处理（3 次指数退避）；全部失败则 Provider 的 `stream_messages()` 内部 yield `ErrorMessage` |
| 工具实例化/执行时 Python 异常 | 转换为 `ToolResult(is_error=True)`，不中断循环 |
| `asyncio.CancelledError` | 不捕获，向上传播（调用方可取消整个 Agent） |

#### 2.3.5 `InternalEvent` → `AgentMessage` 映射总结

| InternalEvent | 外部可见 | 映射 |
|---------------|----------|------|
| `StreamStart` | 否 | 内部消费（tracer + 元信息记录） |
| `AssistantText` | 是 | 直接 yield 透传 |
| `ToolUse` | 是 | 直接 yield 透传 |
| `TokenCounts` | 否 | budget.record() 内部累积 |
| `StopEvent` | 否 | 转为 `DoneMessage` 后 yield |
| `ErrorMessage` | 是 | 直接 yield 透传 |

---

### 2.4 `query/budget.py` — Token 预算管理

#### 2.4.1 `TokenBudget` 类

```python
@dataclass
class TokenBudget:
    max_turns: int
    context_window: int       # 模型上下文窗口 token 数
    truncation_threshold: int  # context_window * 0.8

    # 运行时状态
    turn_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0

    # 收益递减追踪
    _last_three_deltas: list[int] = field(default_factory=list)
    _previous_total: int = 0
```

**方法**：

| 方法 | 签名 | 说明 |
|------|------|------|
| `record_turn()` | `(turn_index, input_tokens, output_tokens) -> None` | 记录本轮 token 消耗，更新收益递减窗口 |
| `is_exhausted()` | `() -> bool` | `turn_count >= max_turns` |
| `diminishing_returns()` | `() -> bool` | 最近 3 轮每轮增量 < 500 tokens → True |
| `estimate_tokens()` | `(messages, system) -> int` | 粗略估算当前 messages 的 token 数（字符数/4） |
| `needs_truncation()` | `(estimated_tokens) -> bool` | `estimated_tokens > truncation_threshold` |
| `total_tokens()` | `() -> int` | `total_input_tokens + total_output_tokens` |

#### 2.4.2 收益递减检测算法

```python
def record_turn(self, turn_index: int, input_tokens: int, output_tokens: int) -> None:
    self.turn_count += 1
    self.total_input_tokens += input_tokens
    self.total_output_tokens += output_tokens

    current_total = self.total_output_tokens
    delta = current_total - self._previous_total
    self._previous_total = current_total

    self._last_three_deltas.append(delta)
    if len(self._last_three_deltas) > 3:
        self._last_three_deltas.pop(0)

def diminishing_returns(self) -> bool:
    """连续 3 轮，每轮文本产出 < 500 tokens 且无 tool_use → 自动停止。"""
    if len(self._last_three_deltas) < 3:
        return False
    return all(d < 500 for d in self._last_three_deltas)
```

注意：此检测在 tool 执行之后、下一轮开始之前调用。如果本轮有 tool_use，重置 `_last_three_deltas` 为空。

#### 2.4.3 Model Context Window 映射

```python
_MODEL_CONTEXT_WINDOWS: dict[tuple[ModelProvider, str | None], int] = {
    # Anthropic
    (ModelProvider.ANTHROPIC, "claude-sonnet-4-5"): 200_000,
    (ModelProvider.ANTHROPIC, "claude-opus-4-7"): 200_000,
    (ModelProvider.ANTHROPIC, "claude-haiku-4-5"): 200_000,
    # OpenAI
    (ModelProvider.OPENAI, "gpt-4o"): 128_000,
    (ModelProvider.OPENAI, "gpt-4.1"): 1_000_000,
    (ModelProvider.OPENAI, "gpt-4.1-mini"): 1_000_000,
    (ModelProvider.OPENAI, "o4-mini"): 200_000,
}

_DEFAULT_CONTEXT_WINDOW = 200_000

def get_context_window(model_id: ModelId) -> int:
    """返回模型上下文窗口大小。未知模型返回 200_000 默认值。"""
    return _MODEL_CONTEXT_WINDOWS.get(
        (model_id.provider, model_id.name), _DEFAULT_CONTEXT_WINDOW
    )
```

#### 2.4.4 Token 估算

```python
def estimate_tokens_from_messages(messages: list[dict], system: str | None = None) -> int:
    """粗略 token 估算：所有文本字符数 / 4。"""
    total_chars = 0

    if system:
        total_chars += len(system)

    for msg in messages:
        content = msg.get("content", [])
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    # text blocks
                    if block.get("type") == "text":
                        total_chars += len(str(block.get("text", "")))
                    # tool_use blocks: name + JSON input
                    elif block.get("type") == "tool_use":
                        total_chars += len(str(block.get("name", "")))
                        total_chars += len(str(block.get("input", {})))
                    # tool_result blocks: content
                    elif block.get("type") == "tool_result":
                        total_chars += len(str(block.get("content", "")))

    return total_chars // 4
```

---

### 2.5 `query/context.py` — 上下文富化（仅 query 初始化时执行，非每轮）

#### 2.5.1 设计决策

参照 cc-haha-main 模式：
- `enrich_context()` 在 `query()` 第 1 步调用**一次**
- 结果作为 `system` 参数传给 `client.stream_messages()`，**不拼入 user message**
- 理由：git status、日期是**环境信息**，语义上属于 system prompt 的延伸，放在 user message 里会让 LLM 误以为是用户提供的输入
- 不采用 memoize（每次 `query()` 调用都是新快照，`query()` 本身不会被重复调用——每个用户 prompt 只调用一次）

#### 2.5.2 `enrich_context()` → 返回 system 参数字符串

```python
async def enrich_context(workspace_root: str | None = None) -> str:
    """收集环境上下文信息，返回拼接好的文本块，作为 system 参数注入。

    当前收集：
    - 当前日期 (ISO format)
    - git status (如果 workspace_root 是 git 仓库)

    返回空字符串表示无额外上下文。
    """
```

**行为细节**：
- 日期格式：`YYYY-MM-DD`（通过 `datetime.date.today().isoformat()`）
- `git status`：在 `workspace_root` 目录运行 `git status --porcelain`，子进程 5 秒超时
  - 非 git 仓库 / 超时 / 异常 → 静默跳过（返回不含 git 信息的上下文）
  - git 仓库成功 → 拼接 `<git_status>\n<output>\n</git_status>`
- 当前不实现 AGENTS.md 读取和文件列表（后续迭代加入）

#### 2.5.2 辅助函数

```python
async def _run_git_status(cwd: str) -> str | None:
    """在 cwd 运行 git status --porcelain，返回输出或 None。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=5.0
        )
        if proc.returncode == 0:
            return stdout.decode("utf-8", errors="replace").strip()
    except Exception:
        pass
    return None
```

---

### 2.6 `query/truncate.py` — 硬截断

#### 2.6.1 `hard_truncate()`

```python
def hard_truncate(
    messages: list[dict[str, Any]],
    max_tokens: int,          # 截断阈值 (context_window * 0.8)
    min_rounds: int = 2,      # 最少保留的轮数
) -> list[dict[str, Any]]:
    """当消息历史超过 token 预算时，保留最近 N 轮的对话。

    策略：
    1. 从 messages 末尾向前遍历
    2. 以 user message 为"轮"边界（一个 user + 紧随的 assistant(s) 为一轮）
    3. 累积轮数，每次检查累计 token 是否超过 max_tokens
    4. 超过时：保留当前轮回溯到的 message 及其之后的所有 messages
    5. 在保留部分前面插入一条截断提示 user message
    6. 至少保留 min_rounds 轮

    返回截断后的 messages 列表（可能是原列表的切片）。
    """
```

**算法细节**：

```
输入 messages (时间从旧到新):
  [user_1, assistant_1, user_2, assistant_2, user_3, assistant_3, user_4]
                                                              ↑ 最新

从末尾向前扫描:
  - 轮 4: user_4 (1 条)
  - 轮 3: assistant_3, user_3 (2 条)
  - 轮 2: assistant_2, user_2 (2 条)
  - ...

累积 token 估计:
  - 轮 4+3: 若超过 max_tokens → 保留最近 2 轮(min_rounds)
  - 轮 4+3+2: 若 <= max_tokens → 继续

截断后:
  [truncation_notice_user_msg, user_2, assistant_2, user_3, assistant_3, user_4]
```

**截断提示消息格式**：

```python
TRUNCATION_NOTICE = (
    "[Earlier conversation history has been truncated to stay within "
    "the model's context window. The most recent {rounds} exchanges "
    "have been preserved.]"
)
```

---

## 3. Agent 类集成

### 3.1 `Agent.stream()` 实现

当前 `stream()` 抛出 `NotImplementedError`。替换为：

```python
async def stream(self, prompt: str) -> AsyncGenerator[AgentMessage, None]:
    async for message in query(
        prompt=prompt,
        config=self._config,
        trace_id=self._trace_id,
        permission_callback=self._permission_callback,
    ):
        yield message
```

### 3.2 `Agent.run()` — 无需改动

`Agent.run()` 已经正确消费 `stream()` 并聚合为 `AgentResult`：处理 `AssistantText`、`ToolUse`、`ErrorMessage`、`DoneMessage` 四种消息类型。`stream()` 实现后自动工作。

### 3.3 Agent 属性传递

| Agent 属性 | 传递给 | 用途 |
|------------|--------|------|
| `_config` | `query()` → `get_client()`, `TokenBudget()`, `convert_tools()` | 配置驱动所有行为 |
| `_trace_id` | `query()` → `TurnTracer()` | 结构化日志贯穿 |
| `_permission_callback` | `query()` → `_execute_tools_sequential()` | 工具权限裁决 |

---

## 4. 消息格式契约

### 4.1 内部存储格式（Anthropic content-block）

```python
# user message
{
    "role": "user",
    "content": [
        {"type": "text", "text": "请读取 README.md"},
        {"type": "tool_result", "tool_use_id": "tu_abc123", "content": "..."}
    ]
}

# assistant message
{
    "role": "assistant",
    "content": [
        {"type": "text", "text": "让我读取那个文件。"},
        {"type": "tool_use", "id": "tu_abc123", "name": "Read", "input": {"file_path": "..."}}
    ]
}
```

### 4.2 System Prompt

```python
# query() 初始化阶段
system = await enrich_context(workspace_root)
# system 内容示例:
#   Today's date: 2026-06-13
#   <git_status>
#   M  src/agent_core/agent.py
#   ?? plan/phase2-agent-loop-plan.md
#   </git_status>
```

- 通过 `client.stream_messages(messages, tools, system=system)` 传入
- 当前仅注入环境上下文（日期 + git status），Phase 3 通过 AgentDefinition 追加完整 system prompt
- 环境信息在 system 参数中而非 user message 中——语义正确，不影响多轮对话中 messages 的角色边界

---

## 5. 边缘情况处理

| 边缘情况 | 处理 |
|----------|------|
| **LLM 返回空 assistant_text 但有 tool_use** | 正常：assistant message content 仅含 tool_use blocks |
| **LLM 返回空 assistant_text 且无 tool_use** | 异常：视为空响应 → `yield ErrorMessage` → 终止 |
| **连续多轮仅 tool_use 无文本** | 正常执行工具，继续循环 |
| **同一轮多个 tool_use** | 当前串行执行（条目 #5 改为并发分组） |
| **工具返回超大结果** | 当前不做截断（条目 #5 处理） |
| **工具执行时 abort_signal 触发** | 当前 abort_signal 不会被触发（无外部取消机制），预留检查点 |
| **messages 列表为空时调用截断** | 不做截断（空列表 <= 任何阈值） |
| **截断后只保留 1 条 user message（无 assistant）** | 合法：首轮截断不丢失首条 prompt |
| **token 估算误差导致实际超限** | 80% 阈值留 20% 余量 + LLM 自身 max_tokens 限制 |
| **git status 返回大量输出** | 截断到 500 字符，防止首条 user message 过大 |

---

## 6. 测试策略

### 6.1 单元测试

| 测试文件 | 覆盖内容 |
|----------|----------|
| `test_budget.py` | TokenBudget: record_turn, is_exhausted, diminishing_returns (含边界：恰好 3 轮、重置后、无 tool_use 场景), estimate_tokens_from_messages, needs_truncation, get_context_window |
| `test_truncate.py` | hard_truncate: 空列表, 单轮, 多轮不超限, 多轮超限截断, 保留 min_rounds, 截断提示消息格式 |
| `test_context.py` | enrich_context: 非 git 目录 (仅日期), git 目录 (日期 + git status), git 超时 |
| `test_loop.py` | _build_assistant_message, _build_tool_result_user_message, _build_initial_user_message |

### 6.2 Mock 集成测试（不依赖真实 API）

需要实现 `MockClient`（`BaseClient` 子类），产出预定义的 `InternalEvent` 序列。

| 测试场景 | MockClient 行为 |
|----------|----------------|
| **单轮文本响应** | StreamStart → AssistantText("Hello") → TokenCounts → StopEvent("end_turn") |
| **工具调用 + 文本** | StreamStart → AssistantText → ToolUse("Read") → TokenCounts → StopEvent("tool_use") |
| **多轮交互** | 第1轮: text + ToolUse → 第2轮: 文本响应 |
| **max_turns 耗尽** | 产出 N 轮 tool_use → query 在 max_turns 处终止 |
| **LLM 返回 ErrorMessage** | StreamStart → ErrorMessage("overloaded") → StopEvent |
| **未知工具** | ToolUse("NonExistentTool") → ToolResult(is_error=True) |
| **空 AssistantText** | StreamStart → StopEvent("end_turn") — 无任何文本或工具 |
| **收益递减触发** | 连续 3 轮每轮 < 500 text tokens，无 tool_use |

### 6.3 E2E 测试（vcrpy 录制回放，需 API key 录制一次）

| 测试场景 |
|----------|
| 真实 Anthropic API: "读取 README.md" → Read tool_use → Read result → 最终文本 |
| 真实 OpenAI API: 同上 |

---

## 7. 实现步骤（按依赖排序）

### Day 1: 预算 + 截断 + 上下文模块

| 步骤 | 文件 | 内容 |
|------|------|------|
| 1.1 | `query/budget.py` | 实现 `get_context_window()`, `estimate_tokens_from_messages()`, `TokenBudget` 类 |
| 1.2 | `query/truncate.py` | 实现 `hard_truncate()` 函数 |
| 1.3 | `query/context.py` | 实现 `enrich_context()`, `_run_git_status()` |
| 1.4 | 测试 | `test_budget.py`, `test_truncate.py`, `test_context.py` |

### Day 2: Turn 循环骨架

| 步骤 | 文件 | 内容 |
|------|------|------|
| 2.1 | `query/__init__.py` | 导出 query |
| 2.2 | `query/loop.py` | 实现 `query()` 骨架：初始化 → 单轮 LLM 调用 → 文本响应 → DoneMessage |
| 2.3 | `query/loop.py` | 实现 `_build_*` 辅助函数 |
| 2.4 | 测试 | MockClient 单轮 / 多轮 / 错误场景 |

### Day 3: 工具执行 + 多轮循环

| 步骤 | 文件 | 内容 |
|------|------|------|
| 3.1 | `query/loop.py` | 实现 `_execute_tools_sequential()` + tool_result → user message 回填 |
| 3.2 | `query/loop.py` | 多轮循环：tool_use → 执行 → 结果 → 下一轮 |
| 3.3 | `query/loop.py` | 收益递减检测集成 |
| 3.4 | `query/loop.py` | 预算检查 + 硬截断集成 |
| 3.5 | 测试 | 多轮工具调用、收益递减、预算耗尽、截断触发 |

### Day 4: Agent 集成 + 收尾

| 步骤 | 文件 | 内容 |
|------|------|------|
| 4.1 | `agent.py` | `Agent.stream()` 替换 NotImplementedError → 调用 `query()` |
| 4.2 | 测试 | 更新 `test_agent.py`：验证 stream() yield 正确的消息序列 |
| 4.3 | 测试 | E2E 测试 (可选，需 API key) |
| 4.4 | 文档 | 更新 `agent-core-todolist-python.md` 条目 #4 状态 |

---

## 8. 与后续条目的接口契约

### 8.1 条目 #5: 工具执行管线 & 权限系统

当前 `loop.py` 中的 `_execute_tools_sequential()` 是一个占位函数。条目 #5 实现后：

```python
# 在 loop.py 中替换为：
from agent_core.tools.pipeline import run_tools  # 条目 #5 实现

# 调用处从：
results = await _execute_tools_sequential(tool_uses, context, permission_callback)

# 变为：
results = await run_tools(tool_uses, context, permission_callback)
```

**契约**：`run_tools()` 签名必须与 `_execute_tools_sequential()` 兼容（接收 `list[ToolUse]` + `ToolUseContext` + callback，返回 `list[ToolResult]`）。

### 8.2 条目 #5.5: 自动压缩

`query/loop.py` 在每轮开始前的预算检查处预留压缩触发点：

```python
# 第 N 轮开始前
if budget.needs_compression(estimated_tokens):  # 阈值 > 85%
    compression_event = await compress_messages(messages, config, trace_id)
    yield compression_event
    messages = compression_event.compressed_messages
```

### 8.3 条目 #9: Stop Hooks

在每轮 assistant message 产出后预留 hook 管线调用点：

```python
# 预留 stop hook check
if stop_hooks:
    for hook in stop_hooks:
        result = await hook(messages, context)
        if result.should_stop:
            yield DoneMessage(stop_reason="end_turn", ...)
            return
```

### 8.4 Phase 3: System Prompt

当前 `query()` 传 `system=None`。Phase 3 引入 AgentDefinition 后：

```python
system = agent_definition.system_prompt or agent_definition.build_system_prompt(tools)
```

---

## 9. 依赖关系

```
query/__init__.py
    └── query/loop.py
            ├── query/budget.py   (TokenBudget, get_context_window, estimate_tokens)
            ├── query/context.py  (enrich_context)
            ├── query/truncate.py (hard_truncate)
            ├── llm/base.py       (get_client, BaseClient)
            ├── llm/events.py     (InternalEvent 类型)
            ├── logging/tracer.py (TurnTracer)
            ├── tools/registry.py (get_tool)
            └── models.py        (AgentConfig, AgentMessage, ...)

agent.py
    ├── query/__init__.py  (query 函数)
    └── models.py          (AgentConfig, AgentResult, AgentMessage)
```

---

## 10. 风险与缓解

| 风险 | 缓解 |
|------|------|
| token 估算不准确导致截断过激或不足 | 80% 阈值比通常的 tokenizer 精确值保守约 10%，留足安全余量 |
| 收益递减误判（工具密集场景每轮文本少但有进展） | 只在无 tool_use 的轮累积 delta；有 tool_use 的轮重置计数器 |
| 工具执行异常中断整个 query | 所有工具异常转为 `ToolResult(is_error=True)`，不传播异常 |
| LLM SDK 异常 (网络/超时) 导致流断裂 | 内联重试已由 Provider 层承担（3 次指数退避）；全部失败时 SDK 抛异常 → query 层转为 ErrorMessage → 终止 |
