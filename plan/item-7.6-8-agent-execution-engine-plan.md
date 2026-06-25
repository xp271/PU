# Item 7.6 & 8: 消费契约实现 + 子Agent 执行引擎 & AgentTool —— 实现计划

> 目标：将 7.6 消费契约落地到 `run_agent()` 中，实现完整的子 Agent 执行引擎、AgentTool、异步生命周期管理、Fork/恢复机制。
> 对应 TodoList 条目 #7.6 + #8，预估 5–6 天（含 §1.5 对 `query()`/`ToolUseContext` 的前置接口扩展）。

---

> **状态** (2026-06-24)：计划阶段。`AgentDefinition` 及数据模型（Item 7.1–7.5）已实现，`permissions/inheritance.py` 三个纯函数合约已就绪，`PermissionEngine` 规则匹配链完整，`query()` 主循环已就绪。`AgentTool` 和 `run_agent()` 均待实现。

---

## 1. 架构总览

### 1.1 子 Agent 在系统中的地位

```
Agent(config).run(prompt)
  │
  ▼
Agent.stream() ──► query()                              ← 当前主 Agent 路径
                      │
                      ├─ 用户直接交互: AgentConfig 驱动
                      │
                      └─ LLM 调用 AgentTool → AgentTool.call()
                           │
                           ▼
                    ┌──────────────┐
                    │  AgentTool   │  ← Item 8 新增
                    │  .call()     │     让 LLM 通过 tool_use 创建子 Agent
                    └──────┬───────┘
                           │
              ┌────────────▼───────────┐
              │  resolve_agent_tools() │  ← §2 工具池过滤
              │  build_child_engine()  │  ← §2 权限构建
              │  merge_mcp_servers()   │  ← §2 MCP 合并
              │  build_system_prompt() │  ← §2 System Prompt 构建
              └────────────┬───────────┘
                           │
              ┌────────────▼───────────┐
              │    run_agent()         │  ← Item 8 核心: 异步生成器
              │  隔离 ToolUseContext     │
              │  子 query() 调用         │
              │  sidechain JSONL 记录     │
              │  finally MCP / hook 清理  │
              └────────────┬───────────┘
                           │
              ┌────────────▼───────────┐
              │   Agent 生命周期管理     │  ← §5 异步/恢复/Fork
              │   run_async_lifecycle() │
              │   resume_agent()        │
              │   fork_agent()          │
              └─────────────────────────┘
```

### 1.2 核心设计原则

- **`run_agent()` 是 `query()` 的包装器，但 `query()` 需要小幅扩展接口**：`run_agent()` 在调用 `query()` 之前完成环境准备（工具池过滤、权限引擎构建、MCP 初始化、System Prompt 拼接），在 `query()` 结束后做清理。**注意**：自定义 System Prompt、Fork/Resume 的历史消息注入、异步通知投递这三件事**无法**在不改 `query()` 的前提下实现——`query()` 当前签名既不接受 `system` 也不接受初始 `messages`，主循环内也没有通知检查点。因此本计划**明确要求**对 `query()`、`ToolUseContext` 做受控的向后兼容扩展（详见 §1.5），而非"零侵入"。
- **AgentTool 是普通 tool 实现**：继承现有抽象基类 `Tool`（`tools/base.py`，**不是** `ToolBase`），定义自己的 Pydantic Input 模型，实现 `validate_input(dict) -> BaseModel`、`call(input: BaseModel, context) -> ToolCallResult`、`input_schema()`。由 LLM 通过 tool_use 调用，内部路由根据 `subagent_type` 查找 `AgentDefinition` 后调用 `run_agent()`。
- **隔离语义**：子 Agent 有独立的 `ToolUseContext`（`copy.deepcopy` read_file_state，独立 `abort_signal`），独立 MCP 连接，独立 JSONL sidechain。
- **权限不可提权**（§2 安全不变量）：子 Agent `permission_mode = min(父, 子声明)`，`disallowed_tools` 取并集，`auto_approve_tools` 只在父允许范围内生效。
- **递归有界**：`AgentTool` 必须携带并递增 `depth`，超过 `MAX_AGENT_DEPTH` 时拒绝派生（详见 §2.6 / §4.3），防止子 Agent 继承 `Agent` 工具后无限递归。
- **同步/异步/Fork 三条路径统一入口**：`AgentTool.call()` 是唯一入口，根据参数选择执行路径。

### 1.3 新增文件与模块

```
src/agent_core/agents/
├── run.py                   # run_agent() 异步生成器 — 核心执行引擎 (~250 行)
├── tool.py                  # AgentTool — LLM 可调用的 tool_use 实现 (~200 行)
├── lifecycle.py             # 异步生命周期管理 (run_async_lifecycle, finalize, enqueue) (~150 行)
├── fork.py                  # Fork / 恢复逻辑 (fork_agent, resume_agent) (~120 行)
└── resolve.py               # 工具池过滤 + 权限构建 + MCP 合并 + System Prompt 拼接 (~150 行)
```

### 1.4 影响/修改的现有文件

| 文件 | 变更内容 |
|------|---------|
| `src/agent_core/agents/__init__.py` | 新增导出：`run_agent`, `AgentTool`, `resolve_agent_tools`, `build_child_engine` |
| `src/agent_core/tools/__init__.py` | 注册 `AgentTool` 到 `_default_registry` |
| `src/agent_core/tools/base.py` | **修改**。`ToolUseContext` 新增可选字段携带父级句柄（详见 §1.5），全部带默认值，向后兼容 |
| `src/agent_core/query/loop.py` | **修改**。`query()` 新增 `system` 与 `initial_messages` 可选参数；主循环开头新增通知队列检查点（详见 §1.5），均向后兼容 |
| `src/agent_core/agent.py` | **不修改**。`AgentTool` 作为 tool 被注入，`Agent` 类无需感知子 Agent 逻辑 |
| `src/agent_core/permissions/inheritance.py` | **不修改**。三个纯函数已被 `resolve.py` 调用 |
| `src/agent_core/permissions/engine.py` | **不修改**。`build_child_engine()` 在 `resolve.py` 中构造 PermissionEngine |
| `src/agent_core/permissions/rules.py` | **不修改**。复用 `PermissionRule` 类型 |

### 1.5 对 `query()` / `ToolUseContext` 的受控扩展（前置依赖）

> 以下改动是 §3/§5/§6 多项功能的硬前置。全部向后兼容（新增参数/字段带默认值），不影响现有调用方。

**(a) `query()` 新增两个可选参数**（`query/loop.py`）：

```python
async def query(
    prompt: str,
    config: AgentConfig,
    trace_id: str,
    permission_callback: PermissionCallback | None = None,
    workspace_root: str | None = None,
    registry: ToolRegistry | None = None,
    system: str | None = None,              # 新增：外部注入的 system，非 None 时跳过/前置于 enrich_context
    initial_messages: list[dict] | None = None,  # 新增：预置消息历史（fork/resume），非 None 时替代 [_build_initial_user_message(prompt)]
) -> AsyncGenerator[AgentMessage, None]:
```

- `system` 用途：让 `run_agent()` 传入 §2.4 拼好的子 Agent system prompt。语义：`system` 非 None 时，作为 system 前缀，`enrich_context()` 的环境信息仍可附加（实现时确定拼接顺序）。
- `initial_messages` 用途：fork（§6.1）和 resume（§6.3）注入历史。非 None 时主循环以它为起点，`prompt` 作为最后一条 user message 追加（或在 resume 时为空）。

**(b) `ToolUseContext` 新增父级句柄字段**（`tools/base.py`，全部带默认值）：

```python
@dataclass
class ToolUseContext:
    abort_signal: asyncio.Event = field(default_factory=asyncio.Event)
    read_file_state: dict[str, Any] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    agent_id: str = ""
    workspace_root: str | None = None
    # -- 新增：供 AgentTool 派生子 Agent 使用（普通工具忽略即可）--
    parent_config: "AgentConfig | None" = None
    parent_trace_id: str = ""
    parent_permission_callback: "PermissionCallback | None" = None
    parent_system: str = ""
    parent_allowed_tools: frozenset[str] = field(default_factory=frozenset)
    parent_disallowed_tools: frozenset[str] = field(default_factory=frozenset)
    agent_depth: int = 0                    # 递归深度（§2.6）
    notification_queue: "list[str] | None" = None  # 异步通知投递队列（§5.5）
```

- 这些字段由 `query()` 在构造主循环的 `ToolUseContext` 时填充（父级的 config / trace_id / callback / 有效工具集等）。`parent_allowed_tools` 由 `query()` 用 `config.tools` 对 registry 求值得到（`["*"]` → registry 全部工具名）。
- `notification_queue` 是父子共享的列表引用；§5.5 的检查点读取并清空它。

---

## 2. 消费契约落地 (`agents/resolve.py`)

这是 Item 7.6 的全部内容——将 [Item 7 计划中定义的 5 条契约](item-7-agent-definition-plan.md) 转化为可调用的函数，供 `run_agent()` 使用。

### 2.1 `resolve_agent_tools()`

```python
def resolve_agent_tools(
    definition: AgentDefinition,
    parent_allowed: frozenset[str],
    parent_disallowed: frozenset[str],
) -> tuple[frozenset[str], frozenset[str]]:
    """Compute the effective tool set for a child agent.

    Returns (allowed, disallowed) — both frozensets of tool names.

    Steps:
    1. If definition.tools == ["*"], start with parent_allowed.
       Otherwise, intersect definition.tools with parent_allowed.
    2. disallowed = parent_disallowed ∪ definition.disallowed_tools
    3. Remove any tool in disallowed from the allowed set.

    This implements the safety invariant: child can only use tools
    that both parent and child allow, and can never use tools either
    party forbids.
    """
```

**实现要点**：
- 调用 `compute_child_allowed_tools(parent_allowed, definition.tools)` 和 `compute_child_disallowed_tools(parent_disallowed, definition.disallowed_tools)`
- 去重：`allowed - disallowed`

### 2.2 `build_child_engine()`

```python
def build_child_engine(
    definition: AgentDefinition,
    parent_mode: PermissionMode,
    parent_disallowed: frozenset[str],
) -> PermissionEngine:
    """Build a PermissionEngine for the child agent.

    Steps:
    1. Compute effective mode: child_mode = compute_child_permission_mode(
       parent_mode, definition.permission_mode)
    2. Generate extra allow rules from definition.auto_approve_tools,
       AFTER filtering out any tool in parent_disallowed (安全约束 —
       父禁用的工具即使列在 auto_approve_tools 中也无效).
    3. Construct PermissionEngine with default_rules + extra_rules,
       mode=child_mode.
    """
```

**安全约束实现**（Item 7 计划 §7.5）：
```python
# 父禁用的工具从 auto_approve_tools 中剔除
safe_auto = [
    name for name in definition.auto_approve_tools
    if name not in parent_disallowed
]
extra_rules = [
    PermissionRule(
        tool_pattern=name,
        action="allow",
        priority=0,
        description=f"pre-authorized for agent '{definition.agent_type}'",
    )
    for name in safe_auto
]
```

### 2.3 `merge_mcp_servers()`

```python
def merge_mcp_servers(
    definition: AgentDefinition,
    parent_mcp: dict[str, McpServerConfig],
) -> dict[str, McpServerConfig]:
    """Shallow-merge MCP server configs. Child wins on same-name key.

    Returns a new dict (does not mutate inputs).
    """
```

### 2.4 `build_child_system_prompt()`

```python
def build_child_system_prompt(
    definition: AgentDefinition,
    parent_system: str,
    tool_names: frozenset[str],
) -> str:
    """Build the child agent's system prompt.

    Format:
        {definition.system_prompt}

        ---
        Available tools: {sorted tool_names}
        {parent_system}
    """
```

**顺序决策**：子 Agent 的 `system_prompt` 在前（最高优先级指令），父 prompt 在后（保留上下文），工具列表在中间（方便 LLM 查阅可用工具）。

**投递路径**：本函数产出的字符串由 `run_agent()` 通过 `query(system=...)`（§1.5 新增参数）注入——这是它唯一的注入点，`query()` 原本不接受外部 system。`parent_system` 来源于 `ToolUseContext.parent_system`（§1.5）。

### 2.5 `resolve_model()`

```python
def resolve_model(
    definition: AgentDefinition,
    parent_model_id: ModelId,
) -> ModelId:
    """Resolve the model for a child agent.

    definition.model == None → inherit parent_model_id.
    Otherwise, parse and return.
    """
```

**注意 model 别名映射**：`AgentTool` 输入 schema 暴露的是短别名（`"sonnet"` / `"opus"` / `"haiku"`，见 §4.2），而 `AgentDefinition.model` 与 `resolve_model()` 用的是完整 `"provider/model"`。两者之间需要一张映射表，在 `AgentTool.call()` 里把输入的短别名翻译为完整 id 后再覆盖 `definition.model`：

```python
_MODEL_ALIASES = {
    "sonnet": "anthropic/claude-sonnet-4-5",
    "opus": "anthropic/claude-opus-4-7",
    "haiku": "anthropic/claude-haiku-4-5",
}
```

### 2.6 `MAX_AGENT_DEPTH` 与递归约束

子 Agent 默认会从父级继承 `Agent` 工具（内置 `GeneralPurpose` 的 `tools=["*"]` 经 `compute_child_allowed_tools` 展开后**包含** `Agent`），因此**必须**有显式的递归上限，不能依赖工具池过滤来阻断（见 §11 修订）。

```python
MAX_AGENT_DEPTH = 3   # 父=0，其子=1，以此类推

def disallow_agent_at_depth_limit(
    allowed: frozenset[str], depth: int
) -> frozenset[str]:
    """达到深度上限时，从 allowed 中剔除 'Agent'，禁止再派生。"""
    if depth >= MAX_AGENT_DEPTH:
        return allowed - {"Agent"}
    return allowed
```

`AgentTool.call()` 在派生前检查 `context.agent_depth`；`run_agent()` 给子 context 设 `agent_depth = context.agent_depth + 1`。

---

## 3. 核心执行引擎 (`agents/run.py`)

### 3.1 `run_agent()` 异步生成器

```python
async def run_agent(
    prompt: str,
    definition: AgentDefinition,
    *,
    parent_config: AgentConfig,
    parent_trace_id: str,
    parent_permission_callback: PermissionCallback | None,
    parent_messages: list[dict[str, Any]] | None = None,
    parent_system: str = "",
    parent_allowed_tools: frozenset[str] | None = None,
    parent_disallowed_tools: frozenset[str] | None = None,
    parent_mcp_servers: dict[str, McpServerConfig] | None = None,
) -> AsyncGenerator[AgentMessage, None]:
    """Execute a child agent in an isolated environment.

    This is the main entry point for all sub-agent execution paths
    (sync, async, fork).  It yields AgentMessage items that the caller
    can consume or aggregate.

    Parameters:
        prompt: The task prompt for the sub-agent.
        definition: Merged AgentDefinition from the registry.
        parent_config: Parent agent's AgentConfig (API keys, etc.).
        parent_trace_id: Parent's trace_id for linking.
        parent_permission_callback: Parent's ask callback (propagated).
        parent_messages: Parent's message history (for fork mode).
        parent_system: Parent's effective system prompt.
        parent_allowed_tools: Parent's effective allowed tool set.
        parent_disallowed_tools: Parent's effective disallowed tool set.
        parent_mcp_servers: Parent's active MCP server configs.
    """
```

### 3.2 执行流程

```
┌──────────────────────────────────────────────────┐
│  1. 初始化                                         │
│     - agent_id = uuid4().hex                      │
│     - 模型解析 (§2.5)                               │
│     - 工具池过滤 (§2.1)                             │
│     - 权限引擎构建 (§2.2)                           │
│     - MCP 合并 (§2.3)                              │
│     - System Prompt 构建 (§2.4)                    │
│     - 创建隔离 ToolUseContext                       │
│       · read_file_state = copy.deepcopy(parent)   │
│       · abort_signal = asyncio.Event()            │
│       · workspace_root 继承自 parent               │
│     - 创建子 AgentConfig (model 使用解析结果)        │
│     - 创建子 ToolRegistry (仅包含允许的工具)          │
│     - 初始化 MCP 连接 (后台任务)                     │
│     - 打开 sidechain JSONL 文件                     │
└──────────────────┬───────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────┐
│  2. 调用 query() 主循环                            │
│     async for msg in query(                        │
│         prompt=prompt,                             │
│         config=child_config,                       │
│         trace_id=f"{parent_trace_id}/{agent_id}",  │
│         permission_callback=child_permission_cb,    │
│         workspace_root=...,                         │
│         system=child_system,        # §1.5/§2.4    │
│         initial_messages=fork_msgs, # §6 fork/resume│
│         registry=child_registry,    # 仅允许的工具  │
│     ):                                             │
│         # 写入 sidechain JSONL（§3.3 transcript 格式）│
│         await append_sidechain(sidechain_fh, msg)   │
│         yield msg                                  │
└──────────────────┬───────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────┐
│  3. finally 清理                                   │
│     - 关闭 MCP 连接                                │
│     - flush + close sidechain JSONL               │
│     - 注销 hooks (Item 9 接入后)                    │
│     - 清理临时文件                                  │
└──────────────────────────────────────────────────┘
```

### 3.3 关键实现细节

**隔离 `ToolUseContext`**（子 context 隔离自身状态，但向下传递父级句柄 + 递增深度，使更深层的 `AgentTool` 仍能派生）：
```python
import copy

child_context = ToolUseContext(
    abort_signal=asyncio.Event(),                                  # 独立取消信号
    read_file_state=copy.deepcopy(parent_context.read_file_state), # 隔离副本
    messages=[],                                                   # 由 query() 维护
    agent_id=agent_id,
    workspace_root=parent_context.workspace_root,
    # -- 透传父级句柄，供子 Agent 内部的 AgentTool 继续派生（§1.5）--
    parent_config=child_config,
    parent_trace_id=f"{parent_trace_id}/{agent_id}",
    parent_permission_callback=parent_permission_callback,
    parent_system=child_system,
    parent_allowed_tools=child_allowed,
    parent_disallowed_tools=child_disallowed,
    agent_depth=parent_context.agent_depth + 1,                    # §2.6 递归计数
    notification_queue=parent_context.notification_queue,
)
```

**子 Agent 权限回调**：
```python
async def child_permission_callback(tool_name: str, tool_input: dict) -> bool:
    decision = await child_engine.check(
        tool_name, tool_input,
        callback=parent_permission_callback,
        tool_is_read_only=is_tool_read_only(tool_name),
        tool_is_destructive=is_tool_destructive(tool_name),
    )
    return decision.allowed
```

**sidechain JSONL 写入**：

> **格式必须与 Item 10 的 transcript 格式一致**，否则 `resume_agent()`（§6.3）无法从 JSONL 重建合法的 `messages` 列表。`AgentMessage.type` 是 `"assistant_text"`/`"tool_use"`/`"done"` 等**流事件类型，不是 chat role**，且 `ToolUse` 没有 `.content`、`AgentMessage` 没有 `agent_id`——不能直接把 `msg.type` 当 role 写。正确做法是把流式事件**聚合回 Anthropic content-block 消息**后再落盘。

```python
# agent_id 在 sidechain 文件名中已体现，无需逐行重复
async def _append_sidechain(fh, record: dict) -> None:
    """record 形如 {"role": "user|assistant", "content": [...blocks...],
    "timestamp": ..., "turn": N}，与 Item 10 transcript 行格式一致。"""
    await fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    await fh.flush()
```

聚合策略（在 `run_agent()` 消费 `query()` 流时进行）：
- `assistant_text` + 同一轮的 `tool_use` → 合并为一条 `{"role":"assistant","content":[{type:text}, {type:tool_use}...]}`；
- `tool_result` → `{"role":"user","content":[{type:tool_result, tool_use_id, content, is_error}]}`；
- `done` 事件不写入 transcript（仅用于统计）。

这样 `resume_agent()` 直接 `load → messages` 即可喂回 `query(initial_messages=...)`（§1.5）。

**sidechain 路径约定**：
```
~/.agent/projects/{project_hash}/{parent_session_id}/{agent_id}.jsonl
```

### 3.4 错误处理

| 场景 | 处理 |
|------|------|
| `AgentDefinition` 的 `agent_type` 不存在 | `AgentTool.call()` 阶段抛 `KeyError`，LLM 收到 error tool_result |
| MCP 连接初始化失败 | warning 日志，继续执行（MCP 工具不可用但内置工具仍然可用） |
| `query()` 内部异常 | 正常终止 + sidechain 记录 error 事件 |
| `finally` 清理异常 | 独立 try/except 包裹每个清理步骤，一个失败不影响其他的 |

---

## 4. AgentTool (`agents/tool.py`)

### 4.1 Tool 定义

继承现有抽象基类 `Tool`（`tools/base.py`），与其它内置工具完全一致的接口（Pydantic Input + `validate_input` + `call` + `input_schema`）。

```python
from typing import ClassVar
from pydantic import BaseModel, Field
from ..tools.base import Tool, ToolCallResult, ToolUseContext


class AgentInput(BaseModel):
    description: str = Field(description="A short (3-5 word) description of the task.")
    prompt: str = Field(description="The task for the agent to perform.")
    subagent_type: str = Field(
        default="GeneralPurpose",
        description="Agent type, e.g. 'Explore', 'Plan', 'GeneralPurpose'.",
    )
    model: str | None = Field(
        default=None, description="Optional model override (alias)."
    )
    run_in_background: bool = Field(default=False)


class AgentTool(Tool):
    name: ClassVar[str] = "Agent"
    is_concurrency_safe: ClassVar[bool] = False
    is_read_only: ClassVar[bool] = False
    # 子 Agent 可执行 Write/Edit/Bash 等破坏性操作，故 Agent 工具本身视为破坏性，
    # 不能被引擎 step6/7 的"非破坏性默认放行"绕过审批（见 §6 安全说明）。
    is_destructive: ClassVar[bool] = True

    def validate_input(self, input: dict) -> AgentInput:
        return AgentInput.model_validate(input)

    @classmethod
    def input_schema(cls) -> dict:
        return AgentInput.model_json_schema()

    async def call(self, input: AgentInput, context: ToolUseContext) -> ToolCallResult:
        ...
```

### 4.2 输入字段说明

| 字段 | 必填 | 说明 |
|------|------|------|
| `description` | ✅ | 3–5 词任务摘要 |
| `prompt` | ✅ | 交给子 Agent 的任务 |
| `subagent_type` | — | 默认 `"GeneralPurpose"`；不存在则返回 error result |
| `model` | — | 短别名（`sonnet`/`opus`/`haiku`），经 `_MODEL_ALIASES`（§2.5）翻译为完整 id 后覆盖 `definition.model`；`None` 时继承 |
| `run_in_background` | — | `True` 或 `definition.background` → 异步路径（§4.5） |

`model` 字段**必须**在 `call()` 路由里实际读取并经 §2.5 的别名表翻译——不能像旧稿那样声明却不消费。

### 4.3 路由逻辑

`call()` 接收的是已校验的 `AgentInput`（不是 dict），父级状态从 `context`（§1.5 扩展字段）取得，而**不是**从不存在的 `context.parent_config` 之类裸属性硬取。

```python
async def call(self, input: AgentInput, context: ToolUseContext) -> ToolCallResult:
    # 0. 递归深度闸（§2.6）——必须在派生前检查
    if context.agent_depth >= MAX_AGENT_DEPTH:
        return ToolCallResult(
            content=f"Agent nesting depth limit ({MAX_AGENT_DEPTH}) reached.",
            is_error=True,
        )

    # 1. 查找 AgentDefinition
    try:
        definition = get_agent(input.subagent_type)
    except KeyError:
        return ToolCallResult(
            content=f"Unknown agent type: '{input.subagent_type}'", is_error=True,
        )

    # 2. model 别名覆盖（§2.5）
    if input.model is not None:
        resolved = _MODEL_ALIASES.get(input.model, input.model)
        definition = definition.model_copy(update={"model": resolved})

    # 3. 校验父级句柄齐备（context 由 query() 填充，§1.5）
    if context.parent_config is None:
        return ToolCallResult(
            content="AgentTool requires parent context (parent_config missing).",
            is_error=True,
        )

    agent_id = uuid.uuid4().hex

    # 4. 路由
    if input.run_in_background or definition.background:
        return await self._run_async(agent_id, definition, input.prompt, context)
    return await self._run_sync(agent_id, definition, input.prompt, context)
```

### 4.4 同步模式

父级状态全部来自 `context`（§1.5 扩展字段，已在 §4.3 step 3 校验非空）。

```python
async def _run_sync(self, agent_id, definition, prompt, context) -> ToolCallResult:
    """Inline iteration: drain the child stream, return aggregated text."""
    parts: list[str] = []
    is_error = False

    async for msg in run_agent(
        prompt=prompt,
        definition=definition,
        parent_config=context.parent_config,
        parent_trace_id=context.parent_trace_id,
        parent_permission_callback=context.parent_permission_callback,
        parent_messages=context.messages,                 # 当前对话历史
        parent_system=context.parent_system,
        parent_allowed_tools=context.parent_allowed_tools,
        parent_disallowed_tools=context.parent_disallowed_tools,
        parent_mcp_servers=context.parent_config.mcp_servers,
        _parent_context=context,                          # 透传以构造子 context（§3.3）
    ):
        if msg.type == "assistant_text":
            parts.append(msg.content)
        elif msg.type == "done" and msg.stop_reason == "error":
            is_error = True

    return ToolCallResult(content="\n".join(parts), is_error=is_error)
```

> `run_agent()` 需要 `parent_context`（§3.3 用它构造隔离子 context：拷贝 `read_file_state`、递增 `agent_depth`、透传 `notification_queue`）。因此签名应增加一个 `_parent_context: ToolUseContext` 形参，或直接把上面这些 `parent_*` 收敛为「传入 `context`」一个参数——实现时二选一并与 §3.1 的签名对齐。

### 4.5 异步模式

**不能裸 `asyncio.create_task()` 发射后不管**——裸 task 不被持有时可能被 GC，且父 `query()` 流结束、`aclosing`/MCP `finally` 触发后该 task 会被孤儿化/在事件循环收尾时取消。必须把 task 句柄登记到一个**与父 Agent 同生命周期的注册表**里，并对完成/异常做回收。

```python
class BackgroundAgentRegistry:
    """父 Agent 生命周期内的后台子 Agent 任务表。
    存于父 ToolUseContext（或 query 作用域），父循环结束时 await/cancel 所有未完成项。"""
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._states: dict[str, AgentLifecycleState] = {}

    def launch(self, agent_id, coro) -> None:
        task = asyncio.create_task(coro, name=f"agent:{agent_id}")
        self._tasks[agent_id] = task
        task.add_done_callback(lambda t: self._on_done(agent_id, t))

    async def drain(self) -> None:
        """父循环收尾：等待仍在运行的后台 Agent（或按策略取消）。"""
        ...

async def _run_async(self, agent_id, definition, prompt, context) -> ToolCallResult:
    """Launch tracked background task, return immediately with agent_id."""
    registry = _get_or_create_bg_registry(context)   # 挂在父 context 上
    registry.launch(
        agent_id,
        run_async_lifecycle(
            agent_id=agent_id, definition=definition, prompt=prompt,
            context=context,        # 透传父级句柄 + notification_queue
        ),
    )
    return ToolCallResult(
        content=json.dumps({"status": "async_launched", "agent_id": agent_id}),
        is_error=False,
    )
```

> **生命周期归属**：后台 task 必须挂在比单次工具调用更长命的对象上（父 `ToolUseContext` 或 query 作用域）。父 `query()` 主循环退出前需 `await registry.drain()`，否则结果/通知永远到不了父 Agent。这也是 §1.5 要求改 `query()` 的原因之一。

---

## 5. 异步生命周期管理 (`agents/lifecycle.py`)

### 5.1 `run_async_lifecycle()`

```python
async def run_async_lifecycle(
    agent_id: str,
    definition: AgentDefinition,
    prompt: str,
    context: ToolUseContext,     # 父级句柄 + notification_queue（§1.5）
) -> None:
    """Run an agent in background and manage its full lifecycle.

    本协程是 run_agent() 流的【唯一】消费者——边迭代边更新共享的
    AgentLifecycleState（token / tool_use_count / latest_activity）。
    进度摘要器只【读】state、不碰 stream（见 §5.3），从根本上避免
    同一个 async generator 被两个协程驱动。

    1. state = AgentLifecycleState(agent_id, status="running")
    2. summarizer = asyncio.create_task(_progress_summarizer(state))  # 只读 state
    3. try:
         transcript = []
         async for msg in run_agent(prompt, definition, ...context...):
             _update_state(state, msg)          # 累计 token / tool_use / 最近文本
             transcript.append(msg)
       finally:
         summarizer.cancel()
    4. finalize_agent_tool(state, transcript)
    5. note = enqueue_agent_notification(state)  # 写入 context.notification_queue
    6. state.status = "completed" | "failed"
    """
```

### 5.2 `AgentLifecycleState`

```python
@dataclass
class AgentLifecycleState:
    agent_id: str
    status: Literal["running", "completed", "failed", "killed"]
    total_tokens: int = 0
    tool_use_count: int = 0
    latest_activity: str = "Initializing..."
    recent_texts: list[str] = field(default_factory=list)  # summarizer 读取
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    result: str = ""
    error: str | None = None
```

### 5.3 背景摘要

摘要器**不消费消息流**——它只是定时器，周期性读取由 `run_async_lifecycle` 更新的 `state`。消息流的唯一消费者是 `run_async_lifecycle`（§5.1），从根本上杜绝"同一 async generator 被两个协程驱动"。

```python
async def _progress_summarizer(
    state: AgentLifecycleState,
    interval: float = 30.0,
) -> None:
    """每 interval 秒，从 state.recent_texts 生成 3-5 词进度摘要。
    不触碰 run_agent() 的消息流。"""
    while True:
        await asyncio.sleep(interval)
        state.latest_activity = _extract_3_5_words(state.recent_texts)


def _update_state(state: AgentLifecycleState, msg: AgentMessage) -> None:
    """由 run_async_lifecycle 在迭代消息流时调用。"""
    if msg.type == "tool_use":
        state.tool_use_count += 1
    elif msg.type == "assistant_text":
        state.recent_texts.append(msg.content[:200])
        del state.recent_texts[:-10]            # 仅保留最近 10 条
    elif msg.type == "done":
        state.total_tokens = msg.total_tokens
```

**简化方案**：背景摘要用最简单的启发式方法（取最近助理文本的前几个词），避免在后台线程中做 LLM 调用。Item 9 引入 Skill 系统后可升级为真正的 LLM 摘要。

### 5.4 `finalize_agent_tool()`

```python
def finalize_agent_tool(
    state: AgentLifecycleState,
    messages: list[dict[str, Any]],
) -> AgentLifecycleState:
    """Extract final result and usage statistics from completed agent run.

    - Final result = concatenated assistant_text messages
    - Usage = sum of token counts from DoneMessage
    - Mark state.completed_at
    """
```

### 5.5 `enqueue_agent_notification()`

```python
from xml.sax.saxutils import escape

def enqueue_agent_notification(
    state: AgentLifecycleState,
    queue: list[str],
) -> str:
    """Build an XML <task-notification> block and append it to *queue*.

    *queue* 是父 ToolUseContext.notification_queue（§1.5）的引用。

    Format（result/status 等动态文本必须经 XML 转义）:
        <task-notification>
          <agent_id>{agent_id}</agent_id>
          <status>{status}</status>
          <total_tokens>{total_tokens}</total_tokens>
          <result>{escaped result}</result>
        </task-notification>
    """
    note = (
        "<task-notification>"
        f"<agent_id>{escape(state.agent_id)}</agent_id>"
        f"<status>{escape(state.status)}</status>"
        f"<total_tokens>{state.total_tokens}</total_tokens>"
        f"<result>{escape(state.result)}</result>"
        "</task-notification>"
    )
    queue.append(note)
    return note
```

> **XML 转义**：`result`/`status` 等是子 Agent 的自由文本，可能含 `<`、`&`、`>`，必须 `xml.sax.saxutils.escape`，否则会破坏 `<task-notification>` 结构。

**通知投递机制**（依赖 §1.5 的 `query()` 改动 + 共享 `notification_queue`）：
- `notification_queue` 是父 `ToolUseContext` 上的 list，引用透传给所有后台子 Agent（§3.3）。
- `run_async_lifecycle` 完成时调用 `enqueue_agent_notification(state, queue)` 追加一条。
- 父 `query()` 主循环**每轮开始时**检查该队列（§1.5 新增检查点）：非空则把累积的通知合并为一条 user message 插入 `messages`，并清空队列。
- 这一步**必须**修改 `query/loop.py`——这是 §1.5 列为前置依赖、且 §11 风险表已据此更新的原因。

---

## 6. Fork & 恢复 (`agents/fork.py`)

### 6.1 Fork Agent

```python
def fork_messages(
    parent_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Filter parent messages for forking a sub-agent.

    - Copy all parent messages
    - Call filter_incomplete_tool_calls() to remove dangling
      tool_use blocks (those without a corresponding tool_result)
    - Return filtered copy (does not mutate input)
    """
```

### 6.2 `filter_incomplete_tool_calls()`

```python
def filter_incomplete_tool_calls(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove tool_use blocks that lack a corresponding tool_result.

    Algorithm:
    1. Scan all messages for tool_use IDs and tool_result tool_use_ids
    2. Build a set of "completed" tool_use IDs (those with a tool_result)
    3. For each assistant message, filter its content blocks:
       - Keep text blocks
       - Keep tool_use blocks only if their ID is in the completed set
       - Remove assistant messages that become empty after filtering
    """
```

### 6.3 `resume_agent()`

```python
async def resume_agent(
    agent_id: str,
    sidechain_path: str,
    **kwargs,  # Same kwargs as run_agent()
) -> AsyncGenerator[AgentMessage, None]:
    """Resume a previously started agent from its sidechain JSONL.

    1. Load JSONL from sidechain_path（行格式即 §3.3 的 transcript blocks）
    2. Reconstruct messages list（直接是 [{"role","content":[...]}, ...]）
    3. Rebuild ToolUseContext，read_file_state 置空（见下）
    4. Call run_agent() / query(initial_messages=reconstructed)（§1.5）
    """
```

**"stale" 的落地含义**：`read_file_state` 直接**置为空 dict**。该缓存的作用是「Edit/Write 前要求文件未被外部改动」；恢复时无法保证文件未变，清空即强制子 Agent 重新 `Read` 后才能编辑（`FileEditTool` 已有"必须先读取"的前置校验）。无需新增 stale 标志位。

**消息注入路径**：重建出的 `messages` 经 §1.5 的 `query(initial_messages=...)` 喂入——这正是 §3.3 要求 sidechain 落盘格式与 transcript 一致的原因；若沿用旧稿那种 `role=msg.type` 的有损格式，本步无法还原。

---

## 7. 公开导出 (`agents/__init__.py` 增量)

```python
from .run import run_agent
from .tool import AgentTool
from .resolve import (
    resolve_agent_tools,
    build_child_engine,
    merge_mcp_servers,
    build_child_system_prompt,
    resolve_model,
)
from .lifecycle import (
    AgentLifecycleState,
    enqueue_agent_notification,
    finalize_agent_tool,
    run_async_lifecycle,
)
from .fork import (
    filter_incomplete_tool_calls,
    fork_messages,
    resume_agent,
)

__all__ = [
    # 已有
    "AgentDefinition", "AgentMemory", "AgentRegistry",
    "get_agent", "list_agents", "register_agent",
    # 新增
    "run_agent", "AgentTool",
    "resolve_agent_tools", "build_child_engine",
    "merge_mcp_servers", "build_child_system_prompt", "resolve_model",
    "AgentLifecycleState", "enqueue_agent_notification",
    "finalize_agent_tool", "run_async_lifecycle",
    "filter_incomplete_tool_calls", "fork_messages", "resume_agent",
]
```

---

## 8. 测试计划

### 8.1 单元测试 — resolve (`tests/unit/test_agent_resolve.py`)

| # | 测试项 | 描述 |
|---|--------|------|
| 1 | `resolve_agent_tools` — `["*"]` 展开 | 子声明 `["*"]` → 返回父全部 allowed |
| 2 | `resolve_agent_tools` — 交集 | 子声明 `["Read", "Bash"]`，父 allowed `{"Read", "Glob"}` → 仅 `{"Read"}` |
| 3 | `resolve_agent_tools` — disallowed 并集 | 父禁 `{"Bash"}`，子禁 `{"Write"}` → `{"Bash", "Write"}`，且 allowed 中移除 `Bash` |
| 4 | `resolve_agent_tools` — 空交集 | 子声明 `["Bash"]`，父 allowed `{"Read"}` → `{}` allowed |
| 5 | `build_child_engine` — `auto_approve_tools` 编译 | 子声明 `auto_approve=["Edit"]` → 引擎含 priority=0 的 allow 规则 |
| 6 | `build_child_engine` — 父禁用的工具不编入 allow | 父 disallowed 含 `"Bash"`，子 `auto_approve=["Bash"]` → allow 规则不包含 Bash |
| 7 | `build_child_engine` — mode 钳制 | 父 `bypassPermissions`，子声明 `ask` → 子引擎 mode=`ask` |
| 8 | `build_child_engine` — mode 继承 | 父 `ask`，子 `None` → 子引擎 mode=`ask` |
| 9 | `merge_mcp_servers` — 同名覆盖 | 子同名 server name 覆盖父 |
| 10 | `merge_mcp_servers` — 不同名合并 | 父子不同 server name 均保留 |
| 11 | `build_child_system_prompt` — 拼接顺序 | 验证 `{子 prompt}\n---\nAvailable tools: [...]\n{父 prompt}` |
| 12 | `resolve_model` — 继承 | definition.model=None → 返回 parent_model_id |
| 13 | `resolve_model` — 覆盖 | definition.model="anthropic/claude-haiku-4-5" → 返回对应 ModelId |
| 14 | `disallow_agent_at_depth_limit` — 到顶 | depth>=MAX_AGENT_DEPTH → allowed 中移除 `Agent` |
| 15 | `disallow_agent_at_depth_limit` — 未到顶 | depth<上限 → allowed 不变 |

### 8.2 单元测试 — fork (`tests/unit/test_agent_fork.py`)

| # | 测试项 | 描述 |
|---|--------|------|
| 1 | `filter_incomplete_tool_calls` — 完整对话 | 所有 tool_use 都有 tool_result → 不删除 |
| 2 | `filter_incomplete_tool_calls` — 末轮 tool_use 无结果 | 最后一轮 assistant 含 tool_use 但无 tool_result → 删除该 tool_use 块 |
| 3 | `filter_incomplete_tool_calls` — 空 assistant 移除 | assistant 仅含被删的 tool_use → 移除整个 assistant message |
| 4 | `filter_incomplete_tool_calls` — 空输入 | `[]` → `[]` |
| 5 | `fork_messages` — 不修改原列表 | 验证输入 messages 未被修改 |

### 8.3 单元测试 — lifecycle (`tests/unit/test_agent_lifecycle.py`)

| # | 测试项 | 描述 |
|---|--------|------|
| 1 | `AgentLifecycleState` 默认值 | status="running", 时间戳自动设置 |
| 2 | `finalize_agent_tool` — 正常完成 | status → "completed", result 正确拼接 |
| 3 | `enqueue_agent_notification` — XML 格式 | 验证含 `<agent_id>{id}</agent_id>` 子元素，且追加进传入的 queue |
| 4 | `enqueue_agent_notification` — 完成状态 | 验证 `<status>`/`<result>` 正确嵌入 |
| 5 | `enqueue_agent_notification` — XML 转义 | result 含 `<`/`&` → 输出被正确转义，不破坏结构 |

### 8.4 单元测试 — AgentTool (`tests/unit/test_agent_tool.py`)

| # | 测试项 | 描述 |
|---|--------|------|
| 1 | input validation — 最小字段 | `{description, prompt}` 通过（`AgentInput` Pydantic 校验） |
| 2 | input validation — 缺必填字段 | 缺 `prompt` → ValidationError |
| 3 | `is_destructive` 标记 | `AgentTool.is_destructive is True`（防止父层非破坏性默认放行） |
| 4 | 未知 agent_type 路由 | 不存在的 agent_type → ToolCallResult(is_error=True) |
| 5 | 缺父级上下文 | `context.parent_config is None` → ToolCallResult(is_error=True) |
| 6 | 递归深度闸 | `context.agent_depth >= MAX_AGENT_DEPTH` → 拒绝派生(is_error=True) |
| 7 | model 别名翻译 | input.model="haiku" → definition.model 被覆盖为 `anthropic/claude-haiku-4-5` |

### 8.5 Mock 集成测试 (`tests/integration/test_run_agent.py`)

使用 `MockModel`（已存在于代码库或新建）产出预定义 tool_use 序列。

| # | 测试项 | 描述 |
|---|--------|------|
| 1 | `run_agent()` 基础流程 | MockClient 单轮文本 → Agent yield AssistantText + DoneMessage |
| 2 | 工具池过滤生效 | 子 Agent 声明 `tools=["Read"]`，调用 `Bash` → 工具不可用 |
| 3 | 权限降级 | 父 `bypassPermissions`，子声明 `ask` → 子 Agent 触发权限回调 |
| 4 | sidechain JSONL 输出 | 验证 sidechain 文件内容包含正确的 JSONL 行 |
| 5 | `finally` MCP 清理 | 验证 MCP 连接正常关闭 |
| 6 | 权限继承链 — 父禁 Bash → 子 `tools=["*"]` → Bash 被过滤 | 验证 `["*"]` 展开 + disallowed 并集正确 |
| 7 | 两个子 Agent 并行不干扰 | 两个 `run_agent()` 并行写不同 JSONL，验证无交叉 |
| 8 | Fork 消息预处理 | 父 messages 末轮有未完成的 tool_use → fork 后过滤 |
| 9 | 自定义 system 注入 | definition.system_prompt 经 `query(system=)` 进入子 Agent system（验证 LLM 收到的 system 含子 prompt） |
| 10 | sidechain ↔ resume 往返 | run_agent 写出的 JSONL 能被 `resume_agent` 重建为合法 messages 并喂回 `query(initial_messages=)` |
| 11 | 递归深度上限 | 嵌套 Agent 调用到 `MAX_AGENT_DEPTH` 后再派生被拒 |
| 12 | 异步任务被回收 | `run_in_background=True` 后父循环 `drain()` 能等到后台 Agent 完成并收到 `<task-notification>` |

---

## 9. 实现步骤（按依赖排序）

### Day 1（上午）: 前置接口扩展 (§1.5) — 硬依赖，必须先做

| 步骤 | 文件 | 内容 |
|------|------|------|
| 0.1 | `tools/base.py` | `ToolUseContext` 新增 §1.5(b) 的父级句柄字段（全部带默认值） |
| 0.2 | `query/loop.py` | `query()` 新增 `system` / `initial_messages` 参数；填充主循环 `ToolUseContext` 的父级字段 |
| 0.3 | `query/loop.py` | 主循环开头新增 `notification_queue` 检查点 + 退出前 `drain()` 后台任务 |
| 0.4 | 回归 | 跑全部已有测试，确认新增参数向后兼容、零回归 |

### Day 1（下午）: 消费契约落地 (resolve.py)

| 步骤 | 文件 | 内容 |
|------|------|------|
| 1.1 | `agents/resolve.py` | 实现 `resolve_agent_tools()` |
| 1.2 | `agents/resolve.py` | 实现 `build_child_engine()` |
| 1.3 | `agents/resolve.py` | 实现 `merge_mcp_servers()` |
| 1.4 | `agents/resolve.py` | 实现 `build_child_system_prompt()`, `resolve_model()`, `disallow_agent_at_depth_limit()` (§2.6) |
| 1.5 | 测试 | `tests/unit/test_agent_resolve.py` 全部 15 个测试 |

### Day 2: 核心执行引擎 (run.py)

| 步骤 | 文件 | 内容 |
|------|------|------|
| 2.1 | `agents/run.py` | 实现 `run_agent()` 骨架：初始化子 config + context + MCP |
| 2.2 | `agents/run.py` | 实现 `query()` 调用 + sidechain 记录 |
| 2.3 | `agents/run.py` | 实现 `finally` 清理逻辑 |
| 2.4 | 测试 | Mock 集成测试：基础流程 + 工具池 + 权限 + sidechain |

### Day 3: AgentTool (tool.py)

| 步骤 | 文件 | 内容 |
|------|------|------|
| 3.1 | `agents/tool.py` | 实现 `AgentTool` 类 + `validate_input()` + 路由逻辑 |
| 3.2 | `agents/tool.py` | 实现同步模式：`_run_sync()` |
| 3.3 | `agents/tool.py` | 实现异步模式：`_run_async()` |
| 3.4 | `tools/__init__.py` | 注册 `AgentTool` 到 tool registry |
| 3.5 | 测试 | AgentTool 单元测试 + Mock 集成测试 |

### Day 4: 异步生命周期 (lifecycle.py) + Fork (fork.py)

| 步骤 | 文件 | 内容 |
|------|------|------|
| 4.1 | `agents/lifecycle.py` | 实现 `AgentLifecycleState`, `run_async_lifecycle()` |
| 4.2 | `agents/lifecycle.py` | 实现 `finalize_agent_tool()`, `enqueue_agent_notification()` |
| 4.3 | `agents/fork.py` | 实现 `filter_incomplete_tool_calls()`, `fork_messages()` |
| 4.4 | `agents/fork.py` | 实现 `resume_agent()` |
| 4.5 | 测试 | lifecycle + fork 单元测试 |

### Day 5: 集成 + 收尾

| 步骤 | 文件 | 内容 |
|------|------|------|
| 5.1 | `agents/__init__.py` | 新增所有公开导出 |
| 5.2 | 测试 | 完整 Mock 集成测试套件（权限继承链、并行隔离、Fork 预处理） |
| 5.3 | 回归 | 运行全部已有测试确保不破坏现有功能 |

---

## 10. 与后续条目的接口契约

### 10.1 Item 9: Hooks + Plugin + Skills

- `run_agent()` 中留 `SubagentStart` hook 触发点（在调用 `query()` 之前）
- `AgentDefinition.hooks` / `AgentDefinition.skills` 两个字段已保留，Item 9 接入后通过 `run_agent()` 传入的 `definition` 直接启用
- Skills preload: 在 `run_agent()` 初始化阶段，若 `definition.skills` 非空，追加 skill 的 system context

### 10.2 Item 10: 会话持久化 & 恢复

- sidechain JSONL 路径和格式已在 Item 8 中定义（`~/.agent/projects/{project_hash}/{session_id}/{agent_id}.jsonl`）
- `resume_agent()` 依赖 JSONL 加载器（Item 10 的 `load_transcript()`）
- Item 10 的 `migrate_transcript()` 在恢复前调用

---

## 11. 风险与缓解

| 风险 | 缓解 |
|------|------|
| **`query()` 接口扩展引入回归** | §1.5 的 `system`/`initial_messages` 参数与 `ToolUseContext` 新字段**全部带默认值**，现有调用路径不传即行为不变；Day 1 step 0.4 强制跑全量回归后才继续 |
| MCP 连接泄漏（子 Agent 异常退出时未清理） | `run_agent()` 用 `try/finally` 包裹 `query()` 调用，`finally` 中独立 try/except 每个 MCP 连接关闭 |
| `copy.deepcopy(read_file_state)` 对大项目性能差 | 当前 `read_file_state` 只保留路径→hash 映射，体积小（O(读取文件数)）；后续可改为 COW |
| **AgentTool 被 LLM 嵌套调用导致无限递归** | **不能**依赖工具池过滤——内置 `GeneralPurpose` 的 `tools=["*"]` 展开后**包含** `Agent`，子 Agent 默认能继续派生。靠 §2.6 的 `MAX_AGENT_DEPTH` 显式深度闸（`AgentTool.call()` 派生前检查 `context.agent_depth`）+ 到顶时 `disallow_agent_at_depth_limit()` 剔除 `Agent` |
| 并行子 Agent 的 JSONL 写冲突 | 每个子 Agent 有独立 agent_id → 独立 JSONL 文件，零冲突 |
| **异步子 Agent 任务孤儿化 / 结果丢失** | 后台 task 登记到 §4.5 `BackgroundAgentRegistry`，父 `query()` 退出前 `await drain()`；结果经 `notification_queue` + §1.5 检查点回投父 Agent。通知在父 agent 下一轮开始时注入（**需**在 `query()` 主循环新增检查点，详见 §1.5/§5.5） |
| 异步通知破坏父消息 XML 结构 | `enqueue_agent_notification` 对动态文本做 `xml.sax.saxutils.escape`（§5.5） |
| 父级"非破坏性默认放行"绕过 Agent 派生审批 | `AgentTool.is_destructive=True`（§4.1），`ask` 模式下经权限回调而非引擎 step6/7 默认放行 |
| Fork 时 prompt cache 复用失败（Anthropic API 特性） | 先实现正确的消息预处理（`filter_incomplete_tool_calls`），prompt cache 复用是性能优化，作为后续迭代 |
