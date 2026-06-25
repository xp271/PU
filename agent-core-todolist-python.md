# Agent 核心库开发 TodoList (Python)

> 目标：纯 Python 库， `Agent(config).run(prompt)` 即可调用，不绑定任何 UI/CLI。
> 每个条目含具体开发事项和预估工时。

---

## Phase 1: 基础设施 —— 已实现

> 以下为已实现的公开 API 与 Tool 框架的完整接口文档。剩余未实现部分（LLM 集成层）见后续 TODO 条目。

---

### 1. 项目架构与公开 API

#### 1.1 包结构与依赖

```
src/agent_core/
├── __init__.py          # 公开 API 导出
├── __main__.py          # python -m agent_core 入口
├── agent.py             # Agent 主类
├── cli.py               # 交互式 REPL 测试入口
├── models.py            # 所有 Pydantic 数据模型
├── agents/              # Agent 定义 + 执行引擎（详见第 7-8 节）
│   ├── __init__.py      # 公开导出 + 触发内置 Agent 注册
│   ├── definition.py    # AgentDefinition Pydantic 模型 + AgentMemory 枚举
│   ├── registry.py      # AgentRegistry 四层优先级链注册表
│   ├── loader.py        # markdown/JSON 加载器 + 目录发现
│   ├── resolve.py       # §7.6 消费契约纯函数
│   ├── run.py           # §8.1 run_agent() 执行引擎
│   ├── tool.py          # §8.2 AgentTool — LLM 可调用的子 Agent 工具
│   ├── lifecycle.py     # §8.3 异步生命周期管理 + 通知
│   ├── fork.py          # §8.4 Fork/恢复
│   └── builtin/         # 内置 Agent 定义
│       ├── __init__.py  # import 触发注册
│       ├── general.py   # GeneralPurpose
│       ├── explore.py   # Explore
│       └── plan.py      # Plan
├── llm/                 # LLM 多 Provider 集成（详见第 3 节）
├── query/               # Agent 主循环（详见第 4 节）
├── session/             # 会话运行时层（Phase A 已完成）
│   └── state.py          # SessionState（跨 prompt 历史 + 权限记忆延续）
├── logging/             # 结构化日志
├── mcp/                 # MCP 协议集成（详见第 6 节）
│   ├── __init__.py      # 公开导出: McpSessionManager, register_mcp_tools
│   ├── _transport.py    # stdio/SSE/WebSocket transport 适配器
│   ├── auth.py          # OAuth 2.0/2.1 token 管理
│   ├── client.py        # McpSessionManager + McpConnectionState
│   ├── config.py        # 配置加载链: .mcp.json → env 展开 → McpServerConfig
│   ├── registry.py      # MCP 工具 → Tool 子类动态桥接
│   └── resources.py     # MCP Resource/Prompt 获取（预留 Phase 3）
└── tools/               # 工具系统（详见第 2 节）
```

**依赖**（`pyproject.toml`）: Python >= 3.12, pydantic >= 2, anthropic >= 0.105.2, openai >= 2, aiofiles >= 24, mcp >= 1.0, tenacity >= 9, tree-sitter >= 0.24, tree-sitter-bash >= 0.23, charset-normalizer >= 3, python-frontmatter >= 1。

**测试框架**: pytest >= 9.0.3 + pytest-asyncio >= 1.4.0 + vcrpy >= 7（录制回放）。

#### 1.2 公开导出 (`agent_core/__init__.py`)

```python
from agent_core import (
    # Agent 入口
    Agent,

    # 配置与结果
    AgentConfig,
    AgentResult,
    AgentMessage,

    # 流式消息类型
    AssistantText,
    ToolUse,
    ToolResult,
    ErrorMessage,
    CompactMessage,
    CompressionConfig,

    # 模型标识 (Provider-agnostic)
    ModelProvider,
    ModelId,

    # Agent 定义系统
    AgentDefinition,
    AgentMemory,
    get_agent,
    list_agents,
    register_agent,
    # 子 Agent 执行引擎
    run_agent,
    AgentTool,
    # 权限
    PermissionEngine,
    PermissionOutcome,
    SessionPermissionStore,

    # 工具系统
    Tool,
    ToolCallResult,
    ToolUseContext,
    ToolRegistry,
    BashTool,
    FileEditTool,
    FileReadTool,
    FileWriteTool,
    GlobTool,
    GrepTool,
    get_tool,
    register_tool,
    registered_tools,

    # MCP 集成
    McpSessionManager,
    McpServerConfig,
    McpServerTransport,
    McpOAuthConfig,
    register_mcp_tools,

    # Hooks 系统
    HookRegistry,
    PreToolUseHook,
    PostToolUseHook,
    StopHook,
    SubagentStartHook,
    HookBlock,
    HookModify,
    HookContinue,
    HookResult,
    StopResult,

    # Plugin 系统
    Plugin,
    PluginRegistry,

    # Skills 系统
    SkillDefinition,
    SkillRegistry,
    SkillTool,
)
```

#### 1.3 `Agent` 类 (`agent.py`)

库唯一的入口类。调用方只通过 `Agent` 交互，内部实现细节完全隐藏。

```python
from agent_core import Agent, AgentConfig

config = AgentConfig(model="anthropic/claude-sonnet-4-5", max_turns=20)
agent = Agent(config)
result = await agent.run("帮我读取 README.md 的内容")
```

**构造函数**

```
Agent(config: AgentConfig) -> None
```

接收一个 `AgentConfig` 实例，内部保存配置引用并初始化权限回调槽位为 `None`。

**方法**

| 方法 | 签名 | 说明 |
|------|------|------|
| `run` | `async run(prompt: str) -> AgentResult` | 消费 `stream()` 的异步生成器，聚合所有 `AgentMessage` 为最终的 `AgentResult`。 |
| `stream` | `async stream(prompt: str) -> AsyncGenerator[AgentMessage, None]` | 委托 `query()` 异步生成器产出 `AgentMessage`。内部用 `aclosing` 保活 MCP 会话清理。 |
| `on_permission_check` | `on_permission_check(callback: PermissionCallback) -> None` | 注册异步权限回调。回调签名为 `async (tool_name: str, tool_input: dict) -> bool`，返回 `True` 表示允许，`False` 表示拒绝。 |

**`PermissionCallback` 类型**

```python
PermissionCallback = Callable[[str, dict[str, Any]], Awaitable[bool]]
```

#### 1.4 配置模型 `AgentConfig` (`models.py`)

所有字段均为可选，有合理默认值。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `model` | `str` | `"anthropic/claude-sonnet-4-5"` | 模型标识符，格式 `"provider/model-name"`（如 `"openai/gpt-4o"`）。省略 provider 时默认 `anthropic`（向后兼容）。非已知模型名触发 `ValueError`，除非 `allow_unknown_model=True`。 |
| `max_turns` | `int` | `20` | 最大 turn 迭代次数。校验范围 1–500。 |
| `permission_mode` | `PermissionMode` | `"ask"` | 默认权限模式：`"bypassPermissions"` / `"acceptEdits"` / `"ask"`。 |
| `tools` | `list[str]` | `["*"]` | 可用工具名称列表。`["*"]` 表示全部已注册工具。不能为空列表。 |
| `mcp_servers` | `dict[str, Any]` | `{}` | MCP 服务端配置，键为服务名。 |
| `skills_dir` | `str \| None` | `None` | Skill 定义文件目录路径。 |
| `hooks` | `list[Any]` | `[]` | Agent 启动时注册的 Hook 实例。 |
| `api_keys` | `dict[str, str]` | `{}` | Provider → API Key 映射（如 `{"deepseek": "sk-..."}`）。缺省时从环境变量读取（`ANTHROPIC_API_KEY`、`DEEPSEEK_API_KEY`、`OPENAI_API_KEY` 等）。 |
| `base_urls` | `dict[str, str]` | `{}` | Provider → 自定义 base URL（代理、自托管场景）。仅替换同 provider 的默认 API 端点。 |
| `allow_unknown_model` | `bool` | `False` | 为 `True` 时允许使用已知 provider 列表之外的模型标识（如代理/自托管/兼容 API 场景）。 |

**`ModelProvider` 枚举**

```python
class ModelProvider(str, Enum):
    ANTHROPIC = "anthropic"
    DEEPSEEK = "deepseek"
    OPENAI = "openai"
```

**`ModelId` —— Provider 无关的模型标识**

```python
@dataclass(frozen=True)
class ModelId:
    provider: ModelProvider
    name: str  # 例: "claude-sonnet-4-5", "gpt-4o"
    
    def __str__(self) -> str:
        return f"{self.provider.value}/{self.name}"
    
    @classmethod
    def parse(cls, raw: str) -> "ModelId":
        """'provider/model' 格式解析；省略 provider 时默认 anthropic（向后兼容）。"""
```

`ModelProvider.known_models()` 返回 `{provider: [模型名列表]}`（Anthropic: claude-sonnet-4-5/opus-4-7/haiku-4-5；DeepSeek: deepseek-v4-pro/v4-flash；OpenAI: gpt-4o/4.1/4.1-mini/o4-mini），`known_providers()` 返回 provider 枚举成员集合，`ModelId.known_values()` 返回所有合法 `"provider/model"` 字符串的集合。

#### 1.5 运行结果 `AgentResult`

```python
class AgentResult(BaseModel):
    content: str                        # LLM 最终文本响应
    total_tokens: int                   # 全流程 token 消耗总计
    tool_uses: int                      # 工具调用总次数
    stop_reason: StopReason             # 停止原因
    error_detail: str | None = None     # 错误详情（stop_reason="error" 时）
```

`StopReason = Literal["end_turn", "max_turns", "permission_denied", "error"]`

#### 1.6 流式消息类型 `AgentMessage`

基于 Pydantic discriminated union，通过 `type` 字段区分 5 种消息变体：

**`AssistantText`** — LLM 产出的文本块
```python
class AssistantText(BaseModel):
    type: Literal["assistant_text"] = "assistant_text"
    content: str
```

**`ToolUse`** — LLM 请求的工具调用
```python
class ToolUse(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    tool_use_id: str
    tool_name: str
    tool_input: dict[str, Any]
```

**`ToolResult`** — 工具执行结果
```python
class ToolResult(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    tool_name: str
    content: Any
    is_error: bool = False
```

**`ErrorMessage`** — 异常/错误
```python
class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    message: str
    error_type: str | None = None
```

**`DoneMessage`** — 流终止信号（携带权威的 stop_reason 和 token 统计）
```python
class DoneMessage(BaseModel):
    type: Literal["done"] = "done"
    stop_reason: StopReason
    total_tokens: int = 0
    error_detail: str | None = None
```

`AgentMessage = Annotated[AssistantText | ToolUse | ToolResult | ErrorMessage | DoneMessage, Field(discriminator="type")]`

---

### 2. Tool 框架与内置工具

Tool 框架是所有能力扩展的基石——后续 MCP / Plugin / Skill 本质都是往注册表注入工具。

#### 2.1 抽象基类 `Tool` (`tools/base.py`)

```python
class Tool(ABC):
    name: ClassVar[str]                       # 工具名称（注册表键，必须由子类定义）
    is_concurrency_safe: ClassVar[bool] = False   # 可否与其他工具并发执行
    is_read_only: ClassVar[bool] = False          # 是否只读（不修改文件系统）
    is_destructive: ClassVar[bool] = False        # 是否可破坏性操作

    @abstractmethod
    async def call(self, input: BaseModel, context: ToolUseContext) -> ToolCallResult: ...

    @abstractmethod
    def validate_input(self, input: dict[str, Any]) -> BaseModel: ...

    @classmethod
    def input_schema(cls) -> dict[str, Any]: ...     # 返回 Pydantic JSON Schema

    @classmethod
    def output_schema(cls) -> dict[str, Any]: ...    # 默认返回 ToolCallResult 的 JSON Schema
```

**设计要点**：
- `is_concurrency_safe` / `is_read_only` / `is_destructive` 为**类属性**（ClassVar），工具执行管线做并发分组时无需实例化即可查询。
- 每个具体工具定义自己的 Input Pydantic 模型，`validate_input()` 负责将原始 dict 校验为强类型 Input 对象。
- `input_schema()` 返回 Input 模型的 JSON Schema，供 LLM tool_use 声明使用。

#### 2.2 `ToolUseContext` — 执行上下文

```python
@dataclass
class ToolUseContext:
    abort_signal: asyncio.Event           # 外部取消信号（工具实现需在长任务中检查此信号）
    read_file_state: dict[str, Any]       # 文件读取状态缓存（路径→{mtime_ns, size, content?}）
    messages: list[dict[str, Any]]        # 当前对话消息历史
    agent_id: str = ""                    # Agent 实例标识
    workspace_root: str | None = None     # 工作区根目录（安全边界）
    # -- 父级句柄（供 AgentTool 派生子 Agent，普通工具忽略）--
    parent_config: Any = None             # AgentConfig | None
    parent_trace_id: str = ""
    parent_permission_callback: Any = None  # PermissionCallback | None
    parent_system: str = ""
    parent_allowed_tools: frozenset[str] = field(default_factory=frozenset)
    parent_disallowed_tools: frozenset[str] = field(default_factory=frozenset)
    agent_depth: int = 0                  # 嵌套深度（防止无限递归）
    notification_queue: list[str] | None = None  # 异步通知投递队列
```

**安全边界**：所有文件/路径类工具在操作前必须通过 `check_workspace()` 校验目标路径位于 `workspace_root` 之内，否则返回 `PermissionError`。

#### 2.3 `ToolCallResult` — 工具返回值

```python
class ToolCallResult(BaseModel):
    content: Any                          # 工具输出内容
    is_error: bool = False                # 是否为错误结果
    truncated: bool = False               # 输出是否被截断
    truncation_notice: str | None = None  # 截断说明
    metadata: dict[str, Any] | None = None  # 结构化元数据（如 diff hunks）
```

#### 2.4 Tool 注册表 (`tools/registry.py`)

```python
# 装饰器注册
@register_tool
class MyTool(Tool):
    name: ClassVar[str] = "MyTool"
    ...

# 按名称获取实例（单例缓存）
tool_instance = get_tool("MyTool")       # -> Tool 实例

# 遍历所有已注册工具类
all_tools = registered_tools()           # -> dict[str, type[Tool]]
```

**行为细节**：
- `register_tool` 装饰器在模块导入时执行，注册工具类到全局 `_registry` dict。
- `get_tool()` 返回缓存的单例实例（首次调用时创建），避免重复实例化。
- 重新注册同名工具会清除旧的实例缓存。
- 查询未注册的工具名会抛出 `KeyError`。

#### 2.5 内置工具一览

##### FileReadTool (`name = "Read"`)

| 属性 | 值 |
|------|-----|
| 并发安全 | `True` |
| 只读 | `True` |
| 破坏性 | `False` |

**Input**

```python
class FileReadInput(BaseModel):
    file_path: str           # 文件绝对路径
    offset: int = 0          # 起始行号（0-based），范围 >= 0
    limit: int = 2000        # 最大返回行数，范围 1–10000
    max_bytes: int = 500_000 # 字节读取上限
```

**行为**：
- 文本类型检测：MIME 白名单（`text/*`, `application/json`, `application/xml` 等），非文本文件返回 `[binary file: mime/type, size: N bytes]`。
- 设备文件拦截：块设备/字符设备直接拒绝。
- 智能缓存：未变更且未超字节预算的文件使用内存缓存，避免重复磁盘 I/O。
- 变更检测：通过 `{mtime_ns, size}` 指纹判断文件是否被外部修改，已变更则重新读取。
- 编码检测：utf-8 → gbk → shift_jis → euc_kr → big5 → latin-1 降级尝试，失败则使用 charset-normalizer；含 NUL 字节视为二进制。
- PDF/图片返回占位信息（TODO：后续接入 pymupdf/Pillow）。

##### FileEditTool (`name = "Edit"`)

| 属性 | 值 |
|------|-----|
| 并发安全 | `False` |
| 只读 | `False` |
| 破坏性 | `True` |

**Input**

```python
class FileEditInput(BaseModel):
    file_path: str    # 文件绝对路径
    old_string: str   # 待替换的精确文本（必须唯一匹配）
    new_string: str   # 替换后的文本（空字符串表示删除）
```

**行为**：
- **前置条件**：文件必须被同一 Agent 的 `FileReadTool` 读取过（在 `read_file_state` 中存在记录），否则拒绝并提示先读取。
- **变更检测**：检查文件指纹是否自上次读取后发生变化，变化则拒绝编辑。
- **唯一匹配**：`old_string` 必须在文件中恰好出现一次，0 次返回 "not found"，>1 次返回 "matches multiple locations" 提示。
- **原子写入**：通过 tempfile + `os.replace` 保证写入原子性，保留原文件权限。
- **编码检测**：使用 `tokenize.detect_encoding` 读取文件编码。
- **输出**：`difflib.unified_diff` 生成 unified diff 文本 + `metadata` 字段含结构化变更信息（`changed_hunks`, `lines_added`, `lines_removed`）。
- 编辑后更新 `read_file_state` 缓存（小文件缓存全文，大文件仅更新指纹）。

##### FileWriteTool (`name = "Write"`)

| 属性 | 值 |
|------|-----|
| 并发安全 | `False` |
| 只读 | `False` |
| 破坏性 | `True` |

**Input**

```python
class FileWriteInput(BaseModel):
    file_path: str  # 文件绝对路径
    content: str    # 写入内容
```

**行为**：
- 父目录自动创建（`pathlib.Path.mkdir(parents=True)`）。
- 若文件已存在且在 `read_file_state` 中，检查外部修改后拒绝覆盖。
- 原子写入（tempfile + `os.replace`），保留原文件权限。
- 写入后更新 `read_file_state` 缓存。

##### GlobTool (`name = "Glob"`)

| 属性 | 值 |
|------|-----|
| 并发安全 | `True` |
| 只读 | `True` |
| 破坏性 | `False` |

**Input**

```python
class GlobInput(BaseModel):
    pattern: str       # glob 模式（如 "**/*.py"）
    path: str = "."    # 搜索基目录
```

**行为**：
- 使用 `pathlib.Path.glob` 进行文件匹配。
- workspace 安全检查：解析符号链接目标后验证不越界。
- 默认过滤隐藏文件/目录（以 `.` 开头的路径段），pattern 以 `.` 开头时保留。
- 按 mtime 降序排序，上限 100 条，超出截断并附 truncation_notice。

##### GrepTool (`name = "Grep"`)

| 属性 | 值 |
|------|-----|
| 并发安全 | `True` |
| 只读 | `True` |
| 破坏性 | `False` |

**Input**

```python
OutputMode = Literal["files_with_matches", "content", "count"]

class GrepInput(BaseModel):
    pattern: str                            # 正则表达式
    path: str = "."                         # 搜索文件或目录
    glob: str | None = None                 # 文件名过滤（如 "*.py"）
    output_mode: OutputMode = "files_with_matches"
    head_limit: int = 100                   # 最大输出行数（1–1000）
    offset: int = 0                         # 跳过前 N 条结果
    context_around: int = 0                 # 上下文行数（-C，0–10）
    ignore_case: bool = False               # 忽略大小写（-i）
    multiline: bool = False                 # 多行模式（-U --multiline-dotall）
```

**行为**：
- 调用系统 `rg --json`（ripgrep），解析 JSON 行输出。
- 三种输出模式：`files_with_matches`（去重文件路径）、`content`（`path:line:text`）、`count`（匹配总数）。
- 两级截断：先按 `head_limit`/`offset` 截断行数，再按 100KB 字节上限截断（不会切断行内文本）。
- 30 秒超时保护。
- 无匹配时 `files_with_matches` 模式返回 `[no matches]`。

##### BashTool (`name = "Bash"`)

| 属性 | 值 |
|------|-----|
| 并发安全 | `False` |
| 只读 | `False` |
| 破坏性 | `True` |

**Input**

```python
class BashInput(BaseModel):
    command: str                             # shell 命令
    timeout: float = 120.0                   # 超时秒数（1–600）
    working_dir: str | None = None           # 工作目录
```

**行为**：
- 通过 `asyncio.create_subprocess_shell` 执行 shell 命令。
- 三重终止机制：正常完成 / 超时（发送 SIGKILL）/ abort_signal（用户手动取消）。
- 输出截断：stdout 和 stderr 各自截断到 100KB。
- 非零退出码时在结果末尾追加 `(exit code: N)` 并将 `is_error=True`。
- `working_dir` 受 workspace 边界检查约束。

#### 2.6 文件状态管理 (`tools/_io.py`)

内部工具模块，被所有文件类工具共享使用。

| 函数 | 说明 |
|------|------|
| `file_fingerprint(path) -> dict` | 返回 `{mtime_ns, size}` 用于变更检测 |
| `fingerprint_changed(path, recorded) -> bool` | 对比当前指纹与记录值 |
| `build_state(path, content?, ...) -> dict` | 构建 `read_file_state` 条目 |
| `async_read_bytes(path) -> bytes` | 异步读取文件原始字节 |
| `async_read_text(path, encoding) -> str` | 异步按指定编码读取文本 |
| `async_atomic_write(path, content, encoding)` | 原子写入（tempfile + `os.replace`，保留权限） |
| `check_workspace(path, root) -> None` | workspace 边界检查（越界抛 `PermissionError`） |
| `safe_decode_bytes(data) -> str` | 多编码降级解码，二进制检测 |

`MAX_CACHE_BYTES = 256 * 1024`：文件不超过此大小时缓存全文，超过则仅缓存指纹（后续 Read/Edit/Write 需磁盘 I/O）。

---

### 3. LLM 多 Provider 集成层 & 结构化日志 (3 天)

**功能**: Provider-agnostic LLM 客户端抽象，Anthropic + OpenAI + DeepSeek 三 provider 实现，消息双向规范化，流式响应，内部规范事件流，全错误类型恢复，turn-level 结构化日志。

**原因**: Agent 主循环的唯一外部依赖就是 LLM API，必须在集成层就支持多 provider。采用 Protocol ABC 模式，每个 provider 的细节隔离在各自实现中，内部统一使用 `AgentMessage` discriminated union 衍生的事件流。详案见 `agent-core-llm-multi-provider-plan.md`。

**架构总览**

```
                    ┌──────────────────────────┐
                    │     query() 主循环        │
                    │  (只消费 InternalEvent)   │
                    └──────────┬───────────────┘
                               │ AsyncGenerator[InternalEvent]
                    ┌──────────▼───────────────┐
                    │    BaseClient (ABC)      │
                    │    stream_messages()     │
                    └────┬──────────┬────┬─────┘
                         │          │    │
              ┌──────────▼──┐ ┌─────▼──┐┌▼──────────┐
              │Anthropic    │ │OpenAI  ││DeepSeek   │
              │Client       │ │Client  ││Client     │
              │(anthropic   │ │(openai ││(openai    │
              │ SDK)        │ │ SDK)   ││ SDK)      │
              └─────────────┘ └────────┘└───────────┘
```

**内部规范事件 `InternalEvent`** — 在 `AgentMessage` 基础上扩充内部用类型：

```python
InternalEvent = Annotated[
    AssistantText     # LLM 文本块 (复用已有)
  | ToolUse           # LLM 工具调用请求 (复用已有)
  | ToolResult        # 工具执行结果 (复用已有)
  | ErrorMessage      # 异常/错误 (复用已有)
  | TokenCounts       # 本轮 token 统计 (新增，内部用，不暴露给调用方)
  | StopEvent         # 流终止信号 → 转DoneMessage (新增，内部用)
  | StreamStart       # 流开始，携带 turn_index / model (新增，内部用)
  , Field(discriminator="type")
]
```

#### 3.1 ModelProvider / ModelId (`models.py`)

**`ModelProvider` 枚举**

```python
class ModelProvider(str, Enum):
    ANTHROPIC = "anthropic"
    DEEPSEEK = "deepseek"
    OPENAI = "openai"

    @classmethod
    def known_providers(cls) -> set[str]: ...

    @classmethod
    def known_models(cls) -> dict["ModelProvider", list[str]]:
        """返回每个 provider 的已知模型名列表。"""
```

**`ModelId` frozen dataclass**

```python
@dataclass(frozen=True, order=True)
class ModelId:
    provider: ModelProvider
    name: str                           # 例: "claude-sonnet-4-5", "gpt-4o"

    def __str__(self) -> str:           # "anthropic/claude-sonnet-4-5"
        return f"{self.provider.value}/{self.name}"

    @classmethod
    def parse(cls, raw: str) -> "ModelId":
        """支持:
          "anthropic/claude-sonnet-4-5"  → ModelId(ANTHROPIC, "claude-sonnet-4-5")
          "openai/gpt-4o"                → ModelId(OPENAI, "gpt-4o")
          "claude-sonnet-4-5"            → ModelId(ANTHROPIC, "claude-sonnet-4-5")  # 向后兼容
        """

    @classmethod
    def known_values(cls) -> set[str]:  # 所有合法 "provider/model" 字符串
```

**`AgentConfig` 多 Provider 字段**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `api_keys` | `dict[str, str]` | `{}` | Provider → API Key 映射（缺省从环境变量读取：`ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`） |
| `base_urls` | `dict[str, str]` | `{}` | Provider → 自定义 base URL（代理/自托管场景） |

**辅助属性/方法**：
- `AgentConfig.model_id` property → `ModelId`：解析 `model` 字符串为结构化标识
- `AgentConfig.get_api_key(provider)` → `str | None`：优先 `api_keys` 字典 → 环境变量 Fallback
- `AgentConfig.get_base_url(provider)` → `str | None`：仅替换同 provider 的默认端点

`validate_model` 逻辑已从 `ClaudeModel.known_values()` 升级为 `ModelId.parse()` + `ModelId.known_values()`，同时保留 `allow_unknown_model` 机制。

---

#### 3.2 BaseClient 抽象 + Provider 注册表 (`llm/base.py`)

每个 Provider 实现 **一个自包含的 `.py` 文件**，继承 `BaseClient` 并覆盖 4 个 classmethod + 1 个 instance method。通过注册模式自动路由，无需硬编码 match/case。

```python
class BaseClient(ABC):
    def __init__(self, model: str, api_key: str | None, base_url: str | None): ...

    # -- classmethod（Provider 覆盖，静态工具方法）--

    @classmethod
    def convert_messages(cls, messages: list[dict], system: str | None) -> Any:
        """内部 Anthropic content-block 格式 → Provider 原生 API 格式"""

    @classmethod
    def convert_tools(cls, allowed_tools: list[str]) -> list[dict]:
        """从 Tool 注册表提取 schema → Provider 原生 tools 参数"""

    @classmethod
    def normalize_stop_reason(cls, raw_reason: str) -> tuple[str, str | None]:
        """Provider raw stop reason → (internal StopReason, error_detail)"""

    @classmethod
    def classify_error(cls, exception: Exception) -> ErrorCategory:
        """Provider SDK 异常 → 统一 ErrorCategory"""

    # -- instance method（异步流式调用）--

    @abstractmethod
    async def stream_messages(
        self, messages: list[dict], tools: list[dict],
        system: str | None = None, max_tokens: int = 4096,
        turn_index: int = 0,
    ) -> AsyncGenerator[InternalEvent, None]: ...
```

**注册表 + 装饰器工厂**

```python
# 全局注册表：{ModelProvider: Type[BaseClient]}
_PROVIDER_CLIENTS: dict[ModelProvider, type[BaseClient]] = {}

def register_client(cls: type[BaseClient]) -> type[BaseClient]:
    """装饰器，读取 cls.provider ClassVar 作为注册 key。
    与 @register_tool 使用完全一致的注册模式。"""

def get_client(model_id: ModelId, config: AgentConfig) -> BaseClient:
    """从注册表查找并实例化。无需硬编码分支。"""
```

**设计要点**：
- `@register_client` 装饰器读取 `cls.provider` 自动注册，与 Tool 系统的 `@register_tool` 模式统一
- 新增 Provider 无需修改此文件——装饰器在 import 时自动完成注册
- `convert_messages` / `convert_tools` / `normalize_stop_reason` / `classify_error` 作为 **classmethod** 定义，无需实例化即可调用（例如 `query()` 在创建 client 前获取 tool schema）
- 原计划的 `normalize.py` 和 `tool_schema.py` 已**删除**——其内容按 Provider 内聚到各自的 `anthropic.py` / `openai.py` 中。

---

#### 3.3 内部事件流 (`llm/events.py`)

```
StreamStart → AssistantText* → (ToolUse)* → TokenCounts → StopEvent
```

| 事件类型 | 字段 | 说明 |
|----------|------|------|
| `StreamStart` | `turn_index`, `provider`, `model` | 流开始元信息，供 tracer 使用 |
| `TokenCounts` | `input_tokens`, `output_tokens`, `cache_read_tokens?`, `cache_write_tokens?` | 每 turn 的 token 统计（内部事件，不暴露给调用方） |
| `StopEvent` | `stop_reason`, `finish_reason_detail?` | 流终止信号（Provider 原始 stop reason，由 query() 映射为 StopReason） |

`InternalEvent` discriminated union：`StreamStart | AssistantText | ToolUse | TokenCounts | StopEvent | ErrorMessage`

---

#### 3.4 AnthropicClient (`llm/anthropic.py`)

**自包含**：所有 Anthropic 专属逻辑（消息转换、工具 schema、stop 映射、错误分类、流式解析）集中在此文件。

| 方法 | 说明 |
|------|------|
| `convert_messages()` | 内部格式 → Anthropic Messages API 参数。近乎恒等变换（内部格式即 Anthropic content-block），仅剥离 `_` 前缀的内部元数据字段，返回 `(system, messages)` 元组。 |
| `convert_tools()` | `registry → [{name, description, input_schema}]`，Anthropic 原生格式 |
| `normalize_stop_reason()` | `{"end_turn"→"end_turn", "max_tokens"→"max_turns", "stop_sequence"→"end_turn", "tool_use"→"end_turn"}` |
| `classify_error()` | HTTP status code 驱动：429→rate_limit, 529→overloaded, 413/context→context_length, 5xx→transient, 401/403→fatal |
| `stream_messages()` | 封装 `AsyncAnthropic.messages.stream()` → 解析 ContentBlockStart/Delta/Stop, MessageStop → yield InternalEvent |
| 重试逻辑 | 内联手动重试循环（rate_limit/overloaded/transient 退避，3 次上限），不依赖 tenacity decorator |
| Prompt caching | `cache_read_input_tokens` / `cache_creation_input_tokens` 通过 `TokenCounts` 传递 |

通过 `@register_client` 装饰器 + `provider: ClassVar[ModelProvider] = ModelProvider.ANTHROPIC` 自动注册，与 Tool 系统的 `@register_tool` 模式统一。

---

#### 3.5 OpenAIClient (`llm/openai.py`)

**自包含**：所有 OpenAI 专属逻辑集中在此文件。

| 方法 | 说明 |
|------|------|
| `convert_messages()` | 内部 Anthropic content-block → OpenAI Chat Completions 格式。转换规则：system→首条 system message；user 中的 tool_result block→独立 `role:"tool"` message；assistant 中的 tool_use block→`tool_calls` 数组；空 text → `content=None`。 |
| `convert_tools()` | `registry → [{type:"function", function:{name, description, parameters}}]`，OpenAI 原生格式 |
| `normalize_stop_reason()` | `{"stop"→"end_turn", "length"→"max_turns", "tool_calls"→"end_turn", "content_filter"→"permission_denied"}` |
| `classify_error()` | 429→rate_limit, 503→overloaded, 500/502→transient, 413/context→context_length, content_filter→content_filter, 401/403→fatal |
| `stream_messages()` | 封装 `AsyncOpenAI.chat.completions.create(stream=True)` → 解析 ChatCompletionChunk delta（含 index 聚合 tool_calls）→ yield InternalEvent |
| 特性 | `stream_options={"include_usage": True}` 获取 token 统计；tool_calls 按 index 跨 chunk 累积后在流结束后一次性产出 |
| 重试逻辑 | 同 AnthropicClient，内联手动指数退避 |

通过 `@register_client` 装饰器 + `provider: ClassVar[ModelProvider] = ModelProvider.OPENAI` 自动注册。

---

#### 3.5.5 DeepSeekClient (`llm/deepseek.py`)

**自包含**：所有 DeepSeek 专属逻辑集中在此文件。DeepSeek API 完全兼容 OpenAI 格式，因此复用 `openai` SDK。

| 方法 | 说明 |
|------|------|
| `__init__()` | 默认 `base_url="https://api.deepseek.com"`（用户指定时覆盖） |
| `convert_messages()` | 与 OpenAI 相同（API 格式兼容） |
| `convert_tools()` | 与 OpenAI 相同 |
| `normalize_stop_reason()` | `{"stop"→"end_turn", "length"→"max_turns", "tool_calls"→"end_turn", "content_filter"→"permission_denied"}` |
| `classify_error()` | 429→rate_limit, 503→overloaded, 500/502→transient, **402→fatal**（余额不足，不可重试）, 413/context→context_length, content_filter→content_filter, 401/403→fatal |
| `stream_messages()` | 与 OpenAI 相同结构，`StreamStart.provider="deepseek"` |
| 重试逻辑 | 同 AnthropicClient/OpenAIClient，内联手动指数退避 |

通过 `@register_client` 装饰器 + `provider: ClassVar[ModelProvider] = ModelProvider.DEEPSEEK` 自动注册。

---

#### 3.6 错误分类 + 重试配置 (`llm/errors.py`)

```python
ErrorCategory = Literal[
    "rate_limit",       # 429 → 指数退避重试
    "overloaded",       # 529 (Anthropic) / 503 (OpenAI) → 指数退避重试
    "context_length",   # context_window_exceeded → 不可恢复
    "content_filter",   # 内容过滤 (OpenAI content_filter)
    "transient",        # 500/502/网络错误 → 指数退避重试
    "fatal",            # 认证/参数错误 → 不可恢复
]

def retry_config_for(classify_error_fn, provider: str) -> dict:
    """返回 tenacity.retry(**kwargs) 参数。
    classify_error_fn 为 BaseClient.classify_error 方法。"""
```

`classify_error()` 逻辑已分散到各 Provider 的 classmethod 中——每个 SDK 的异常类型和 status code 语义不同，内聚到 Provider 文件更自然。

---

#### 3.7 新增 Provider 的流程

1. 在 `models.py` 的 `ModelProvider` 枚举中添加成员，`known_models()` 中添加模型列表，`get_api_key()` 中添加环境变量映射
2. 创建 `llm/<provider>.py`：
   - 用 `@register_client` 装饰器 + `provider: ClassVar[ModelProvider]` 声明注册
   - 实现 4 个 classmethod + `stream_messages()`
3. 在 `llm/__init__.py` 中添加 `from . import <provider>` 触发注册
4. 在 `query/budget.py` 中添加该 provider 模型的上下文窗口大小
5. （可选）在 `cli.py` 的 `_ENV_VAR_MAP` 中添加环境变量映射

---

#### 3.8 结构化日志 (`logging/tracer.py`)

**`TurnTracer`** — 透明的 `InternalEvent` 流包装器，收集 turn 级指标并通过 Python `logging` 模块输出结构化记录。

```python
from agent_core.logging import TurnTracer

# query() 中的用法：
tracer = TurnTracer(trace_id=self._trace_id)
async for event in tracer.wrap(client.stream_messages(messages, tools)):
    # event 透明透传，tracer 已在后台收集指标
    match event: ...
```

**设计要点**：
- `TurnTracer.wrap()` 接收 `AsyncGenerator[InternalEvent, None]`，**透明地 re-yield 每个事件**，同时通过 `match/case` 从事件中收集字段
- 流结束（`StopEvent` 或生成器异常退出）时，通过 `logging.getLogger("agent_core.turn").info()` 输出一条结构化日志
- `trace_id` 由 `Agent.__init__()` 生成（`uuid4`），存储在 `self._trace_id`

**日志字段**：

| Field | Source | 说明 |
|-------|--------|------|
| `trace_id` | `TurnTracer.__init__` | uuid4，贯穿 Agent 生命周期 |
| `turn_index` | `StreamStart.turn_index` | |
| `provider` | `StreamStart.provider` | |
| `model` | `StreamStart.model` | |
| `status` | 推断 | `"success"` / `"error"` |
| `input_tokens` | `TokenCounts.input_tokens` | |
| `output_tokens` | `TokenCounts.output_tokens` | |
| `cache_read_tokens` | `TokenCounts.cache_read_tokens` | Anthropic 专用，None 时不输出 |
| `cache_write_tokens` | `TokenCounts.cache_write_tokens` | Anthropic 专用，None 时不输出 |
| `tool_uses` | `ToolUse` 计数 | 本轮工具调用次数 |
| `tool_names` | `ToolUse.tool_name` 去重排序列表 | |
| `latency_ms` | `StreamStart` → `StopEvent` 时间差 | `time.monotonic()` 计时 |
| `error_type` | `ErrorMessage.error_type` | 仅 error 时出现 |

**不包含**：日志持久化（用户自行配置 logging handler）、per-tool 子日志、异步 I/O。

---

## Phase 2: Agent 核心循环 — 已实现

> 以下 #4 已全部实现，#5 及之后为后续任务。

---

### 4. Agent 主循环 (query 系统)

**功能**: `query()` 异步生成器，完整 turn 循环，token 预算管理，上下文硬截断，工具执行管线，CLI 交互式测试入口。

**架构总览**

```
用户 prompt
  │
  ▼
query() ──► enrich_context() ──► 系统提示词 (日期 + git status)
  │
  └── 每轮循环 ──► TokenBudget 检查 ──► 截断检查 ──► LLM 调用 ──►
                     │                     │              │
                     ▼                     ▼              ▼
               is_exhausted?       hard_truncate()   stream_messages()
                                                         │
                                         ┌───────────────┴───────────────┐
                                         ▼                               ▼
                                   文本响应                         工具调用
                                         │                               │
                                         ▼                               ▼
                                   yield DoneMessage              _execute_tools_sequential()
                                                                        │
                                                                        ▼
                                                                  yield ToolResult
                                                                        │
                                                                        ▼
                                                                  下一轮循环
```

**实现文件**:
| 文件 | 职责 |
|------|------|
| `src/agent_core/query/loop.py` | `query()` 主循环 + 消息构建器 + 工具执行管线 |
| `src/agent_core/query/budget.py` | `TokenBudget` 令牌预算 + 收益递减检测 + 上下文窗口感知 |
| `src/agent_core/query/context.py` | `enrich_context()` 上下文富化（日期 + git status） |
| `src/agent_core/query/truncate.py` | `hard_truncate()` 硬截断逻辑 |
| `src/agent_core/cli.py` | 交互式 REPL 测试入口 + 配置文件加载 |
| `src/agent_core/__main__.py` | `python -m agent_core` 模块入口 |

---

#### 4.1 `query()` — Agent 主循环入口

```python
async def query(
    prompt: str,
    config: AgentConfig,
    trace_id: str,
    permission_callback: PermissionCallback | None = None,
    workspace_root: str | None = None,
) -> AsyncGenerator[AgentMessage, None]:
```

整个 Agent 的心脏。协调 LLM 调用、工具执行、token 预算追踪、上下文截断和停止条件检测，产出 `AgentMessage` 流供 `Agent.run()` / `Agent.stream()` 消费。

**参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| `prompt` | `str` | 用户输入文本 |
| `config` | `AgentConfig` | Agent 配置（模型、max_turns、tools、api_keys 等） |
| `trace_id` | `str` | uuid4 hex 字符串，贯穿 Agent 生命周期和日志 |
| `permission_callback` | `PermissionCallback \| None` | 异步权限回调，返回 `True` 允许 / `False` 拒绝 |
| `workspace_root` | `str \| None` | 工作区根目录，用于安全边界和 git status 收集 |

**返回值**: `AsyncGenerator[AgentMessage, None]`，产出 5 种消息类型：

```
AssistantText* → (ToolUse → ToolResult)* → DoneMessage
                     │
                     ├─ 有工具调用：执行后进入下一轮
                     └─ 无工具调用：终端轮，DoneMessage 携带最终统计
```

**主循环流程**

每轮依次执行以下步骤：

```
1. 初始化（仅执行一次）
   ├─ enrich_context(workspace_root) → system 字符串
   ├─ _build_initial_user_message(prompt) → messages[0]
   ├─ get_client(model_id, config) → BaseClient 子类实例
   ├─ client.convert_tools() → tools_schema
   └─ TokenBudget(max_turns, context_window)

2. Turn 循环 (turn_index = 1 .. max_turns)
   │
   ├─ 2a. 预算 / 截断检查
   │   ├─ budget.is_exhausted() → yield DoneMessage("max_turns")
   │   └─ estimate_tokens_from_messages() → hard_truncate() 条件触发
   │
   ├─ 2b. LLM 调用
   │   ├─ client.stream_messages(messages, tools, system)
   │   ├─ TurnTracer.wrap() → 收集 turn 级指标
   │   └─ match event.type:
   │       ├─ "stream_start"    → 内部消费（tracer）
   │       ├─ "assistant_text"   → yield 透传
   │       ├─ "tool_use"         → 累积 + yield
   │       ├─ "token_counts"     → budget.record_turn() + total_tokens 累加
   │       ├─ "stop"             → normalize_stop_reason()
   │       └─ "error"            → yield + DoneMessage("error")
   │
   ├─ 2c. _build_assistant_message() → messages.append()
   │
   ├─ 2d. 无工具调用 → yield DoneMessage（终端轮）
   │
   └─ 2e. 有工具调用
       ├─ _execute_tools_sequential() → yield ToolResult
       ├─ _build_tool_result_user_message() → messages.append()
       └─ budget.reset_diminishing_returns()

3. 循环耗尽 → yield DoneMessage("max_turns")
```

---

#### 4.2 消息构建器 (`loop.py` 内部函数)

消息格式采用 Anthropic content-block 规范：每条消息有 `role` 和 `content` 字段，`content` 是一个 typed block 列表。

**`_build_initial_user_message(prompt: str) -> dict`**

将原始 prompt 包装为 `{"role": "user", "content": [{"type": "text", "text": prompt}]}`。

**`_build_assistant_message(texts: list[str], tool_uses: list[ToolUse]) -> dict`**

从累积的文本块和工具调用构建 assistant 消息：
- 空/纯空白文本块被跳过
- tool_use 块序列化为 `{"type": "tool_use", "id", "name", "input"}`

**`_build_tool_result_user_message(tool_results: list[ToolResult]) -> dict`**

将工具执行结果打包为 `role: "user"` 消息，每个结果为 `{"type": "tool_result", "tool_use_id", "content", "is_error"}` block。

---

#### 4.3 工具执行管线 (`_execute_tools_sequential`)

```python
async def _execute_tools_sequential(
    tool_uses: list[ToolUse],
    context: ToolUseContext,
    permission_callback: PermissionCallback | None,
) -> list[ToolResult]:
```

**当前实现**: 串行执行（Phase 2 item 5 将替换为完整的并发/串行编排管线）。

单个工具执行生命周期（4 步）：

| 步骤 | 说明 | 失败处理 |
|------|------|----------|
| 1. Resolve | `get_tool(name)` 从注册表查找 | `ToolResult(is_error=True, content="Unknown tool")` |
| 2. Validate | `tool.validate_input(raw_dict)` Pydantic 校验 | `ToolResult(is_error=True, content="Invalid input: ...")` |
| 3. Permission | `await permission_callback(name, input)` | `ToolResult(is_error=True, content="Permission denied")` |
| 4. Execute | `await tool.call(validated_input, context)` | 异常 → `ToolResult(is_error=True, content="Tool execution error: ...")` |

---

#### 4.4 `TokenBudget` — 令牌预算管理 (`budget.py`)

```python
@dataclass
class TokenBudget:
    max_turns: int         # 轮次上限
    context_window: int    # 模型上下文窗口大小

    turn_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
```

**属性**

| 属性/方法 | 返回 | 说明 |
|-----------|------|------|
| `truncation_threshold` | `int` | `context_window × 0.8`，超过此值触发硬截断 |
| `is_exhausted()` | `bool` | `turn_count >= max_turns` |
| `diminishing_returns()` | `bool` | 最近 3 轮每轮 output delta < 500 tokens |
| `needs_truncation(est)` | `bool` | 估算 tokens > `truncation_threshold` |
| `total_tokens()` | `int` | `input + output` 总和 |

**方法**

| 方法 | 说明 |
|------|------|
| `record_turn(turn_index, input_tokens, output_tokens)` | 记录本轮用量，更新递减检测窗口（记录 output delta） |
| `reset_diminishing_returns()` | 清空递减检测窗口——工具调用轮次后重置 |

**上下文窗口感知 (`get_context_window`)**

```python
def get_context_window(model_id: ModelId) -> int:
```

| Provider | 模型 | 上下文窗口 |
|----------|------|-----------|
| Anthropic | claude-sonnet-4-5 / opus-4-7 / haiku-4-5 | 200,000 |
| DeepSeek | deepseek-v4-pro / v4-flash | 128,000 |
| OpenAI | gpt-4.1 / gpt-4.1-mini | 1,000,000 |
| OpenAI | gpt-4o | 128,000 |
| OpenAI | o4-mini | 200,000 |
| 未知模型 | — | 200,000 (默认) |

**Token 估算 (`estimate_tokens_from_messages`)**

```python
def estimate_tokens_from_messages(
    messages: list[dict[str, Any]], system: str | None = None
) -> int:
```

chars/4 启发式估算。仅在 API 调用前的截断检查中使用——API 调用后的真实 token 数由 `TokenCounts` 事件提供。

---

#### 4.5 上下文硬截断 (`truncate.py`)

```python
def hard_truncate(
    messages: list[dict[str, Any]],
    max_tokens: int,
    min_rounds: int = 2,
) -> list[dict[str, Any]]:
```

当消息历史超过 context window 的 80% 时丢弃最早轮次。

**算法**: 以 `role: "user"` 为轮次边界分区 → 从最新轮次向前累积 → 估算超过 `max_tokens` 且保留 ≥ `min_rounds` 轮时停止 → 在最前方插入 `<system-reminder>` 通知。

**设计要点**:
- 截断边界对齐 user message（保证消息结构有效）
- `min_rounds` 保证不丢光（至少保留最后 2 轮）
- 无截断时不插入通知（返回原列表）

---

#### 4.6 上下文富化 (`context.py`)

```python
async def enrich_context(workspace_root: str | None = None) -> str:
```

在 `query()` 启动时执行一次（非每轮），收集环境信息作为 LLM 的 system 提示词前缀：

| 信息 | 来源 | 格式 |
|------|------|------|
| 当前日期 | `date.today().isoformat()` | `Today's date is 2026-06-15.` |
| Git 状态 | `git status --porcelain`（5s 超时） | `<git_status>...</git_status>` |

失败静默吞没——git 不可用时仅返回日期，不抛异常。

---

#### 4.7 CLI 交互式测试入口 (`cli.py`)

**两种启动方式**

```powershell
agent-core              # 安装后命令
python -m agent_core    # 模块启动（无需安装）
echo "prompt" | python -m agent_core  # 管道模式
```

**配置查找顺序**（先找到的生效）

1. `$AGENT_CORE_CONFIG` 环境变量指定路径
2. `./agent-core.config.json`（当前工作目录）
3. `~/.agent-core/config.json`（用户目录）

配置文件中未填的 `api_keys` fallback 到环境变量。

**REPL 命令**

| 输入 | 行为 |
|------|------|
| 任意文本 | 作为 prompt 发给 Agent，流式输出回复和工具调用 |
| `/model <id>` | 运行时切换模型（如 `/model openai/gpt-4o`） |
| `/model` | 查看当前模型 |
| `/key <provider> <key>` | 运行时更新 API key |
| `/help` | 显示帮助 |
| `/quit` 或 `Ctrl+C` | 退出 |

**配置文件格式** (`agent-core.config.json`)

```json
{
    "_models": {
        "anthropic": ["anthropic/claude-sonnet-4-5", ...],
        "deepseek": ["deepseek/deepseek-v4-pro", ...],
        "openai": ["openai/gpt-4o", ...]
    },
    "model": "anthropic/claude-sonnet-4-5",
    "api_keys": {
        "anthropic": "sk-ant-xxx",
        "deepseek": "sk-xxx",
        "openai": "sk-xxx"
    },
    "max_turns": 20,
    "permission_mode": "bypassPermissions"
}
```

`_models` 字段仅作文档用途，列出所有支持的模型供参考。实际解析时被忽略。

---

### 5. 工具执行管线 & 权限系统 — 已实现

**功能**: 并发/串行工具编排，7 步单工具生命周期，权限规则引擎（含子 Agent 权限继承契约），Bash AST 级安全检查，大结果异步持久化。

**原因**: 这是 Agent 的安全边界。权限引擎的设计在此时就覆盖了**子 Agent 权限继承**场景——AgentDefinition 的 permission_mode 和 disallowed_tools 如何从父级派生、子 Agent 能否提升自己的权限，这些语义如果等到第 8 项再做会回头返工权限模型。

**实现文件**:

| 文件 | 职责 |
|------|------|
| `src/agent_core/query/loop.py` | `run_tools()` + `run_tool_use()` 工具管线（替代 `_execute_tools_sequential`） |
| `src/agent_core/permissions/__init__.py` | 公开 API 导出 |
| `src/agent_core/permissions/engine.py` | `PermissionEngine` 规则引擎 + `PermissionDecision` |
| `src/agent_core/permissions/rules.py` | `PermissionRule` Pydantic 模型 + `default_rules()` |
| `src/agent_core/permissions/inheritance.py` | 子 Agent 权限继承 3 个纯函数合约 |
| `src/agent_core/permissions/bash_guard.py` | `analyze_bash_command()` + `BashSafetyResult` |
| `tests/unit/test_permissions.py` | 单元测试：PermissionEngine 引擎 (23) + 子Agent权限继承 (6) + Bash AST 安全检查 (22) |
| `tests/unit/test_loop_helpers.py` | 消息构建器 + `run_tool_use` / `run_tools` 工具管线的集成测试 |
| `tests/unit/test_tools_edge_cases.py` | 工具执行管线边界测试：混合并发/串行编排、`_persist_large_result`、设备文件检测、`async_atomic_write` 错误清理 |

---

#### 5.1 工具执行管线 (`query/loop.py`)

##### `run_tools()` — 并发/串行编排

```python
async def run_tools(
    tool_uses: list[ToolUse],
    context: ToolUseContext,
    *,
    permission_callback: PermissionCallback | None = None,
    permission_engine: PermissionEngine | None = None,
    max_concurrency: int = 5,
) -> list[ToolResult]:
```

**编排逻辑**：

```
遍历 tool_uses（保持原始 index）
  │
  ├─ 工具 is_concurrency_safe? 
  │   ├─ True  → 累积到 pending_batch
  │   └─ False → flush pending_batch（并发执行）→ 串行执行当前工具
  │
  └─ 循环结束 → flush 剩余 pending_batch
```

1. 按 `Tool.is_concurrency_safe` 类属性分组（通过 `get_tool(name)` 查询，无需实例化）
2. 并发安全组 → `asyncio.Semaphore(max_concurrency)` + `asyncio.gather()` 
3. 非并发工具 → 串行 `await run_tool_use()`
4. 结果按原始 `tool_uses` 顺序输出（通过 `dict[int, ToolResult]` → index 排序）

##### `run_tool_use()` — 单工具 7 步生命周期

```python
async def run_tool_use(
    tool_use: ToolUse,
    context: ToolUseContext,
    permission_callback: PermissionCallback | None = None,
    permission_engine: PermissionEngine | None = None,
) -> ToolResult:
```

```
1. resolve     → get_tool(name) → 失败返回 error ToolResult
2. validate    → tool.validate_input(raw_dict) → Pydantic 校验，失败返回 error
3. bash_guard  → BashTool 时调用 analyze_bash_command() → 不安全则拒绝
4. permission  → engine.check(tool_name, input, callback, is_read_only, is_destructive)
5. pre-hooks   → (占位 — hooks 系统 Item 9 实现后接入)
6. execute     → await tool.call(validated_input, context)
7. persist     → 结果 > 50KB 时 aiofiles 异步写入临时文件，展示截断预览
```

**步骤 5 (pre-hooks) 说明**：当前 `AgentConfig.hooks` 默认 `[]`，`hooks/` 目录为空。管线保留 hook 调用接口但暂时跳过执行——Item 9 实现后无接口变更即可接入。

---

#### 5.2 权限规则模型 (`permissions/rules.py`)

```python
PermissionAction = Literal["allow", "deny", "ask"]

class PermissionRule(BaseModel):
    tool_pattern: str          # 精确 / 前缀* / fnmatch 通配符
    action: PermissionAction = "ask"
    priority: int = 0          # 越小优先级越高；同优先度按 exact > prefix > wildcard
    path_constraint: str | None = None  # 可选路径 glob 约束
    description: str = ""      # 面向用户的规则说明
```

**匹配模式**：

| 模式 | pattern | 匹配示例 |
|------|---------|----------|
| 精确 | `"Bash"` | 仅匹配 `"Bash"` |
| 前缀 | `"File*"` | `"FileReadTool"`, `"FileWriteTool"` |
| fnmatch | `"mcp__*"` | `"mcp__github__list_issues"` |

**内置默认规则** (`default_rules()`)：

| tool_pattern | action | priority | 说明 |
|-------------|--------|----------|------|
| `Read` | allow | 100 | 文件读取始终安全 |
| `Glob` | allow | 100 | 文件列表始终安全 |
| `Grep` | allow | 100 | 内容搜索始终安全 |
| `Bash` | ask | 100 | Shell 命令需显式批准 |
| `Edit` | ask | 100 | 文件编辑需批准 |
| `Write` | ask | 100 | 文件写入需批准 |

---

#### 5.3 权限规则引擎 (`permissions/engine.py`)

##### `PermissionDecision`

```python
class PermissionDecision(BaseModel):
    allowed: bool
    reason: str
    source: Literal["rule", "mode", "callback", "default"]
```

##### `PermissionEngine`

```python
class PermissionEngine:
    def __init__(
        self,
        rules: list[PermissionRule] | None = None,  # None → default_rules()
        mode: PermissionMode = "ask",
    ) -> None: ...

    @property
    def mode(self) -> PermissionMode: ...

    async def check(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        callback: PermissionCallback | None,
        tool_is_read_only: bool = False,
        tool_is_destructive: bool = False,
    ) -> PermissionDecision: ...

    @classmethod
    def from_config(
        cls,
        permission_mode: PermissionMode = "ask",
        tools: list[str] | None = None,
    ) -> "PermissionEngine": ...
```

**7 步匹配链**：

```
1. 精确 tool_pattern 匹配 (rule.action=allow/deny → 立即返回；ask → fall through)
2. 前缀匹配 (pattern 以 * 结尾，tool_name 以此为前缀)
3. fnmatch 通配符 (支持 * ? [])
4. path_constraint 检查 (规则含路径约束时，校验 tool_input 中的路径字段)
5. PermissionMode 回退
   ├─ bypassPermissions → allowed (source="mode")
   └─ acceptEdits + 非破坏性 → allowed (source="mode")
6. read_only 兜底 → allowed (source="default")
7. 默认 ask → 调用 callback 或 拒绝破坏性/允许只读 (source="callback"|"default")
```

**PermissionMode 行为**：

| Mode | 破坏性工具 | 非破坏性工具 | 绕过规则? |
|------|-----------|-------------|----------|
| `bypassPermissions` | allowed | allowed | 否 — 规则 (step 1-4) 仍先匹配 |
| `acceptEdits` | fall through → ask | allowed (step 5) | 否 |
| `ask` | callback 或拒绝 | callback 或允许 (step 6) | 否 |

> 规则始终先于 mode 匹配。即使 mode=bypassPermissions，一条 priority=0 的 deny 规则仍可阻止工具。

---

#### 5.4 子 Agent 权限继承 (`permissions/inheritance.py`)

**纯函数合约**（不依赖 `AgentDefinition` — Item 7 实现后直接调用）：

```python
def compute_child_permission_mode(
    parent_mode: PermissionMode,
    child_definition_mode: PermissionMode | None,
) -> PermissionMode:
    """子 = min(父, 子定义)。bypassPermissions > acceptEdits > ask
    child_definition_mode=None → 直接继承父模式。"""

def compute_child_allowed_tools(
    parent_allowed: frozenset[str],
    child_allowed_spec: list[str],
) -> frozenset[str]:
    """子 = 父 ∩ 子定义。["*"] 展开为父全部。"""

def compute_child_disallowed_tools(
    parent_disallowed: frozenset[str],
    child_disallowed_spec: list[str],
) -> frozenset[str]:
    """子 = 父 ∪ 子定义。父禁止的，子必然禁止。"""
```

**不变量**：
- 子 Agent **永远不能**提升自身权限（bypassPermissions → ask 方向可降不可升）
- 子 Agent **永远不能**使用父禁止的工具
- 子 Agent **只能**使用父子双方都允许的工具

**测试覆盖** (8 个场景):
- 子模式 = 父（None / 同等级 / bypass）
- 子声明 bypass 但父 ask → 降级为 ask
- 子声明 acceptEdits 但父 ask → 降级为 ask
- 父禁止 Bash → 子 tools=["*"] → Bash 被过滤
- 子显式禁止父允许的工具 → 并集生效
- 子声明的工具父不允许 → 交集过滤为空
- disallowed_tools 并集

---

#### 5.5 Bash AST 安全检查 (`permissions/bash_guard.py`)

```python
class BashSafetyResult(BaseModel):
    safe: bool = True
    warnings: list[str] = []
    dangerous_patterns: list[dict[str, str]] = []
    # [{"pattern": "curl_pipe_bash", "location": "(1:0–1:30)", "severity": "critical"}]

def analyze_bash_command(command: str) -> BashSafetyResult:
    """使用 tree-sitter-bash 解析命令 AST，递归遍历检测危险模式。"""
```

**依赖**: `tree-sitter>=0.24` + `tree-sitter-bash>=0.23`

**危险模式检测**：

| 严重度 | 模式 | 检测逻辑 |
|--------|------|---------|
| critical | `curl/wget ... \| bash/sh` | AST pipeline 左子树含下载器，右子树为 shell |
| critical | `rm -rf /` 或 `rm -rf /*` | rm 命令含 -r/-f/-rf 且路径参数为 `/` / `/*` |
| critical | 重定向覆盖系统文件到 `/dev/null` | `>` 指向 `/dev/null` 且源路径为 `/etc/*` / `~/.*` |
| high | `chmod 777` 在系统路径 | chmod 777/0777 且路径前缀为 `/usr/` `/bin/` `/etc/` 等 |
| high | `sudo` + 危险命令 | sudo 下执行 rm/chmod/dd/mkfs |
| medium | `sudo` (通用) | 任何 sudo 调用 |
| medium | `eval` + 变量引用 | eval 的参数含 `$` |

**复合命令遍历**：递归 walk AST，对 `pipeline`、`command`、`file_redirect` 节点分别检查，`&&` / `;` / `$()` / 反引号中的子命令自动覆盖。

**管线集成**：`run_tool_use()` 在步骤 3 中仅对 `tool.name == "Bash"` 调用，不安全时返回 `is_error=True` 的 `ToolResult`，阻止命令执行。

---

#### 5.6 结果持久化

当 `ToolCallResult.content` 为字符串且 UTF-8 字节数超过 50KB：

```
写入路径: {tempfile.gettempdir()}/agent_core/results/{agent_id}/{timestamp}_{tool_name}_{random}.txt
截断预览: content[:1500] + truncation notice (含文件路径)
```

使用 `aiofiles.open()` + `aiofiles.os.makedirs()` 全程异步 I/O，避免阻塞事件循环。

---

#### 5.7 AgentConfig 新增字段 (`models.py`)

```python
max_concurrency: int = Field(
    default=5,
    ge=1,
    le=20,
    description="Maximum number of concurrency-safe tools to execute in parallel",
)
```

---

#### 5.8 公开导出 (`agent_core/__init__.py` 增量)

权限系统暂未加入公开导出（`permissions` 目前仅内部使用，由 `query/loop.py` 消费）。继承函数在 Item 7/8 实现 `run_agent()` 时随 `AgentDefinition` 一起公开。`analyze_bash_command` 可通过 `from agent_core.permissions import analyze_bash_command` 直接使用。

---

### 5.5 自动压缩 (上下文超限 LLM 摘要) — 已实现

**功能**: 上下文接近窗口上限时优先用 LLM 摘要压缩旧轮次，压缩失败才回退到硬截断，保留关键信息的同时减小上下文体积。

**实现文件**:

| 文件 | 职责 |
|------|------|
| `src/agent_core/compression/__init__.py` | 模块公开导出 |
| `src/agent_core/compression/trigger.py` | `_partition_rounds()` 共享轮次分区 + `can_compress()` 可压缩性判断 |
| `src/agent_core/compression/summarizer.py` | `compress()` 核心：提示词构建 + 重试循环 + LLM 调用 + 消息重组 |
| `src/agent_core/compression/consistency.py` | `verify_compression()` 4 项一致性检查 |
| `src/agent_core/compression/prompts.py` | 可自定义的 prompt 模板常量 |
| `src/agent_core/models.py` | `CompressionConfig` + `CompactMessage` 数据模型 |
| `src/agent_core/query/loop.py` | 主循环集成：压缩优先 → 截断兜底 |
| `src/agent_core/query/truncate.py` | 复用 `_partition_rounds()` 替代内联分区逻辑 |
| `src/agent_core/__init__.py` | 导出 `CompactMessage`、`CompressionConfig` |

**测试文件**:

| 文件 | 说明 |
|------|------|
| `tests/unit/test_compression.py` | 21 个单元测试（触发条件、分区、提示词构建、一致性验证、重试逻辑、配置校验、数据模型） |
| `tests/integration/test_compression.py` | 9 个 MockClient 集成测试（含重试成功/耗尽/禁用/轮次不足等场景） |
| `tests/e2e/test_compression.py` | vcrpy 录制回放测试（端到端压缩触发流程） |

---

#### 5.5.1 压缩在 `query()` 主循环中的位置

压缩和截断共享同一个触发阈值（context window × 0.8，由 `TokenBudget.truncation_threshold` 定义）。到达阈值时**总是先尝试压缩**，仅在以下情况回退到硬截断：

1. `CompressionConfig.enabled = False`（用户主动关闭）
2. 轮次太少没有可压缩的旧轮次
3. 压缩失败且所有重试耗尽

```
query() 主循环中的压缩块:

  estimated = estimate_tokens_from_messages(messages, system)

  if budget.needs_truncation(estimated):            ← 统一 80% 阈值
      if can_compress(messages, config.compression):
          yield CompactMessage(status="started")     ← 事件通知
          try:
              messages, summary = await compress(...) ← 优先压缩
              re-estimate
          except CompressionError:
              messages = hard_truncate(...)          ← 失败 → 截断兜底
      else:
          messages = hard_truncate(...)              ← 不可用 → 直接截断
  → 继续 LLM 调用
```

**行为矩阵**（统一阈值 = context_window × 0.8）：

| 场景 | 阈值触发 | 压缩 | 截断 | 结果 |
|------|---------|------|------|------|
| tokens < 0.8×CW | No | — | No | 无操作 |
| tokens ≥ 0.8×CW | Yes | — | — | 进入处理流程 |
| ├ 压缩已启用 + 有旧轮次 | — | 尝试 | — | 压缩优先 |
| │ ├ 验证一次通过 | — | 成功 | No | 仅压缩 |
| │ ├ 验证失败 → 重试通过 | — | 成功 | No | 压缩（含重试） |
| │ ├ 重试全部耗尽 | — | 失败 | Yes | 回退截断 |
| │ └ 压缩成功但仍 > 0.8×CW | — | 成功 | Yes | 压缩 + 截断 |
| └ 压缩禁用 或 轮次太少 | — | 跳过 | Yes | 直接截断 |

**设计要点**：
- **统一入口**：压缩和截断由 `budget.needs_truncation()`（80% 阈值）统一触发，不存在"80–85% 区间绕过压缩"的窗口
- **压缩优先**：到达阈值后，只要压缩可用，总是先尝试压缩
- **截断兜底**：压缩不可用、压缩失败、或压缩后仍超限时，截断作为最终保障
- 关闭压缩（`enabled=False`）时行为完全回退到仅截断模式

---

#### 5.5.2 数据模型

##### `CompressionConfig` (`models.py`)

```python
class CompressionConfig(BaseModel):
    enabled: bool = True
    keep_recent_rounds: int = 3
    summary_max_tokens: int = 2048
    tool_result_preview_chars: int = 200
    max_retries: int = 2
    prompt_template: str | None = None
```

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | `bool` | `True` | 启用自动压缩（关闭时到达截断阈值直接硬截断） |
| `keep_recent_rounds` | `int` (1–10) | `3` | 保留不压缩的最近 N 轮完整对话 |
| `summary_max_tokens` | `int` (512–8192) | `2048` | LLM 摘要响应的最大 token 数 |
| `tool_result_preview_chars` | `int` (50–500) | `200` | 压缩提示中每个 tool_result 的截断预览字符数 |
| `max_retries` | `int` (0–5) | `2` | 压缩验证失败时的最大重试次数；0 表示失败直接回退到截断 |
| `prompt_template` | `str \| None` | `None` | 自定义压缩请求提示词模板，必须包含 `{preview_chars}` 和 `{conversation_text}` 占位符；`None` 时使用内置模板 |

**AgentConfig 集成**：

```python
class AgentConfig(BaseModel):
    # ... 其他字段 ...
    compression: CompressionConfig = Field(
        default_factory=CompressionConfig,
        description="Auto-compression configuration",
    )
```

##### `CompactMessage` (`models.py`)

压缩事件通知消息，`AgentMessage` discriminated union 的第 6 个变体。

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

| 字段 | 类型 | 说明 |
|------|------|------|
| `type` | `"compact"` | 消息类型标识 |
| `status` | `"started"` \| `"completed"` \| `"skipped"` | 压缩阶段：开始 / 完成 / 跳过 |
| `rounds_compressed` | `int` | 被压缩的轮次数 |
| `rounds_preserved` | `int` | 保留不压缩的轮次数 |
| `tokens_before` | `int` | 压缩前估算 token 数 |
| `tokens_after` | `int` | 压缩后估算 token 数（`skipped` 时为 0） |
| `compression_ratio` | `float` | `tokens_after / tokens_before`（`skipped` 时为 0.0） |
| `summary` | `str \| None` | 生成的摘要文本（仅 `completed` 时有值） |
| `retries_used` | `int` | 实际重试次数（0 表示一次通过） |
| `error_detail` | `str \| None` | 失败原因（仅 `skipped` 时有值） |

`CompactMessage` 是 `AgentMessage` 联合类型的非破坏性扩展——调用方不匹配此变体时自动忽略，完全向后兼容。

```python
AgentMessage = Annotated[
    AssistantText | ToolUse | ToolResult | ErrorMessage | DoneMessage | CompactMessage,
    Field(discriminator="type"),
]
```

---

#### 5.5.3 压缩触发条件 (`compression/trigger.py`)

```python
def _partition_rounds(messages: list[dict]) -> list[list[dict]]:
    """按 role="user" 边界分区。被 hard_truncate 和 compress 共享使用。"""

def can_compress(messages: list[dict], config: CompressionConfig) -> bool:
    """可压缩性判断。不检查阈值——阈值由 budget.needs_truncation() 统一负责。"""
```

**`can_compress()` 返回 True 的条件**：
1. `config.enabled == True`
2. 轮次数 > `config.keep_recent_rounds`（存在可压缩的旧轮次）

**`_partition_rounds()` 分区规则**：
- 以 `role: "user"` 为轮次边界
- 每个轮次包含一个 user 消息 + 后续零或多个 assistant 消息
- tool_result user 消息同样触发新轮次分界

---

#### 5.5.4 压缩函数 (`compression/summarizer.py`)

```python
async def compress(
    client,       # BaseClient 实例（duck-typed 避免循环导入）
    messages: list[dict[str, Any]],
    system: str | None,
    model_id: ModelId,
    config: CompressionConfig,
) -> tuple[list[dict[str, Any]], str, list[CompressionAttempt]]:
```

**参数**

| 参数 | 类型 | 说明 |
|------|------|------|
| `client` | `BaseClient` | 已实例化的 LLM 客户端，用于生成摘要 |
| `messages` | `list[dict]` | 完整消息列表（Anthropic content-block 格式） |
| `system` | `str \| None` | System 提示词（原样传给摘要 LLM 调用） |
| `model_id` | `ModelId` | 当前使用的模型标识 |
| `config` | `CompressionConfig` | 压缩调参 |

**返回值**: `(compressed_messages, summary, attempts)`
- `compressed_messages`: 压缩后的消息列表 = `[摘要消息] + recent_rounds` 展开
- `summary`: 生成的摘要文本
- `attempts`: 所有尝试记录（含 token 消耗和验证结果）

**异常**: 所有重试耗尽后抛出 `CompressionError(Exception)`

**算法流程**：

```
1. _partition_rounds(messages) → rounds
2. old_rounds  = rounds[:-keep_recent_rounds]
   recent_rounds = rounds[-keep_recent_rounds:]
3. 如果 old_rounds 为空 → 返回原列表（无事可压缩）
4. 重试循环 (最多 max_retries + 1 次尝试):
   a. 渲染 old_rounds 为 conversation_text（每轮格式：[Round N] role: text / tool_use / tool_result）
   b. 用 COMPRESSION_REQUEST 模板 + conversation_text 构建提示词
   c. 调用 LLM 生成摘要（无 tools，max_tokens=summary_max_tokens）
   d. 组装 compressed = [summary_msg] + recent_rounds
   e. verify_compression(...) 验证一致性
   f. 验证通过 → 返回 (compressed, summary, attempts)
   g. 验证不通过 → previous_errors 注入 RETRY_FEEDBACK 模板 → 重试
5. 重试耗尽 → 抛出 CompressionError
```

**摘要消息格式**：

```xml
<conversation_summary>
{LLM 生成的摘要}
</conversation_summary>

The above is a summary of earlier conversation rounds.
The following rounds are the most recent unsummarized conversation.
```

**重试反馈格式**（仅当上一次验证失败时追加到提示词末尾）：

```xml
<previous_attempt_feedback>
Your previous summarization was rejected because:
- {验证错误信息}

Please fix these issues in your new summary.
</previous_attempt_feedback>
```

##### 内部数据类型

**`CompressionAttempt`** — 单次压缩尝试记录

```python
@dataclass
class CompressionAttempt:
    attempt: int                        # 尝试编号（1-based）
    summary: str                        # LLM 生成的摘要
    verification: CompressionConsistency  # 验证结果
    tokens_consumed: int = 0            # 本次消耗的总 token 数
    input_tokens_consumed: int = 0      # 输入 token 数
    output_tokens_consumed: int = 0     # 输出 token 数
    error: str | None = None            # 异常信息
```

**`CompressionError`** — 压缩异常

```python
class CompressionError(Exception):
    """压缩不可恢复失败（所有重试耗尽）时抛出。"""
```

---

#### 5.5.5 一致性验证 (`compression/consistency.py`)

```python
@dataclass
class CompressionConsistency:
    valid: bool                          # 无致命错误时为 True
    system_preserved: bool = True        # system prompt 不变（设计保证）
    tool_uses_traceable: bool = True     # 旧轮次 tool_use 在摘要中可追溯
    compressed_rounds: int = 0           # 压缩的轮次数
    preserved_rounds: int = 0            # 保留的轮次数
    warnings: list[str] = []             # 非致命警告
    errors: list[str] = []               # 致命错误
    input_tokens_consumed: int = 0       # 本次 LLM 调用输入 token
    output_tokens_consumed: int = 0      # 本次 LLM 调用输出 token


def verify_compression(
    original_messages: list[dict],
    compressed_messages: list[dict],
    old_rounds: list[list[dict]],
    recent_rounds: list[list[dict]],
    summary: str,
) -> CompressionConsistency:
```

**4 项检查**：

| # | 检查项 | 失败级别 | 说明 |
|---|--------|---------|------|
| 1 | recent_rounds 完整性 | **致命** | 压缩后 recent_rounds 中每条消息必须与原始完全一致，不允许任何修改 |
| 2 | 摘要非空且有意义 | **致命** | 摘要为空字符串或少于 20 字符视为致命错误 |
| 3 | 消息数量减少 | 警告 | old_rounds 仅 1 条消息时发出警告（压缩收益微乎其微） |
| 4 | tool_use 覆盖率 | 警告 | 旧轮次中 tool_use 的工具名在摘要中提及不足 50% 时警告 |

验证结果由 `compress()` 的重试循环消费：
- `errors` 非空 → `valid=False` → 错误信息注入下一次重试的提示词
- 仅 `warnings` 非空 → `valid=True` → 继续使用压缩结果，仅记录日志

---

#### 5.5.6 Prompt 模板 (`compression/prompts.py`)

所有 prompt 模板定义为模块级字符串常量，可被导入和自定义。

| 常量 | 说明 | 占位符 |
|------|------|--------|
| `COMPRESSION_REQUEST` | 主压缩请求提示词 | `{preview_chars}`, `{conversation_text}` |
| `RETRY_FEEDBACK` | 验证失败时的重试反馈 | `{previous_errors}` |
| `CONVERSATION_SUMMARY` | 压缩后注入的摘要包装 | `{summary}` |
| `TOOL_RESULT_TRUNCATED` | tool_result 截断标记 | `{preview}`, `{original_len}` |
| `ROUND_HEADER` | 每轮的分隔标题 | `{round_num}` |

**自定义方式**：

```python
# 方式 1: 代码中覆盖（只需 {preview_chars} 和 {conversation_text} 占位符）
config = AgentConfig(
    compression=CompressionConfig(
        prompt_template="请总结以下对话: {conversation_text}"
    )
)

# 方式 2: 直接编辑 compression/prompts.py 中的模板常量
```

**tool_result 短缩策略**：压缩提示渲染时，每个 `tool_result` block 的 `content` 截断到 `tool_result_preview_chars` 字符，超出部分标记为 `[... truncated, original ~N chars]`。非字符串 content 用 `repr()` 后再截断。

---

#### 5.5.7 调用方集成

`CompactMessage` 是流中的非破坏性事件——调用方可选监听，不匹配时自动忽略。

```python
async for msg in agent.stream(prompt):
    match msg:
        case CompactMessage(status="started", tokens_before=n):
            print(f"[压缩开始] 当前上下文 ~{n} tokens，正在生成摘要...")
        case CompactMessage(status="completed", summary=s, retries_used=r):
            print(f"[压缩完成] 重试 {r} 次，摘要: {s[:100]}...")
        case CompactMessage(status="skipped", error_detail=e):
            print(f"[压缩跳过] {e}，使用硬截断")
        case AssistantText(content=text):
            # 正常响应处理
            ...
```

**关闭压缩**（仅用硬截断）：

```python
config = AgentConfig(
    compression=CompressionConfig(enabled=False),
)
```

**压缩 token 统计**：压缩调用的 token 消耗不计入 `max_turns` 预算，但累加到 `total_input_tokens` 和 `total_output_tokens`，最终反映在 `DoneMessage.total_tokens` 中。

---

### 6. MCP 协议集成 — 已实现

**功能**: MCP 客户端，动态获取服务端工具并注册为 `Tool` 子类，配置来源管理，工具冲突去重，OAuth 认证，启动预检。

**原因**: MCP 是 Agent 扩展能力的标准协议 — 一个协议接入所有兼容服务，避免为每个外部工具写胶水代码。

#### 6.1 配置模型 (`models.py`)

##### `McpServerTransport` (Enum)

```
STDIO = "stdio"         # 子进程 stdin/stdout JSON-RPC
SSE   = "sse"           # HTTP Server-Sent Events
WEBSOCKET = "websocket" # WebSocket（未实现，connect 会抛 NotImplementedError）
```

##### `McpOAuthConfig` (Pydantic BaseModel)

OAuth 2.0/2.1 认证配置，与 transport 层解耦 — Manager 解析 token → transport 注入 `Authorization: Bearer` header。

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `client_id` | `str` | 是 | OAuth client ID |
| `client_secret` | `str \| None` | 否 | client secret（client_credentials flow 需要） |
| `authorization_url` | `str \| None` | 否 | authorization code flow 入口 |
| `token_url` | `str` | 是 | token endpoint |
| `scopes` | `list[str]` | 否 | 权限范围，默认 `[]` |
| `redirect_uri` | `str \| None` | 否 | 回调地址 |
| `device_code_url` | `str \| None` | 否 | 设备码入口 |
| `token` | `str \| None` | 否 | **运行时存储**: 当前 access token |
| `refresh_token` | `str \| None` | 否 | **运行时存储**: refresh token |
| `expires_at` | `float \| None` | 否 | **运行时存储**: token 过期 Unix 时间戳 |
| `audience` | `str \| None` | 否 | JWT aud 校验（未使用） |
| `issuer` | `str \| None` | 否 | JWT iss 校验（未使用） |

##### `McpServerConfig` (Pydantic BaseModel)

单个 MCP 服务器的连接配置。`@model_validator` 校验 transport 与必填字段的对应关系（stdio 需要 `command`，sse/websocket 需要 `url`）。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `transport` | `McpServerTransport` | `"stdio"` | 传输方式 |
| `command` | `str \| None` | `None` | stdio 模式：可执行文件路径 |
| `args` | `list[str]` | `[]` | stdio 模式：启动参数 |
| `env` | `dict[str, str]` | `{}` | stdio 模式：子进程环境变量 |
| `url` | `str \| None` | `None` | sse/websocket 模式：服务端点 URL |
| `headers` | `dict[str, str]` | `{}` | 附加 HTTP headers |
| `cwd` | `str \| None` | `None` | stdio 子进程工作目录 |
| `auth` | `McpOAuthConfig \| None` | `None` | OAuth 认证 |
| `include_tools` | `list[str] \| None` | `None` | 正则白名单，匹配的工具才注册 |
| `exclude_tools` | `list[str] \| None` | `None` | 正则黑名单，匹配的工具不注册 |
| `connect_timeout` | `float` | `30.0` | 连接超时（秒） |
| `request_timeout` | `float` | `60.0` | 单次请求超时（秒，当前未绑定到 call_tool） |
| `max_reconnect_attempts` | `int` | `3` | 失败重试次数 |
| `reconnect_delay` | `float` | `2.0` | 重试退避基数（秒），实际延迟 = delay × 2^attempt |

##### `AgentConfig` 中

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `mcp_servers` | `dict[str, McpServerConfig]` | `{}` | MCP 服务端配置，键为服务名 |

#### 6.2 配置加载链 (`mcp/config.py`)

**`load_mcp_servers() → dict[str, McpServerConfig]`**

加载优先级（高覆盖低）:
1. `AgentConfig.mcp_servers`（代码注入）
2. `.mcp.json`（项目根）
3. `~/.agent/mcp.json`（用户家目录）

同名 server key 时：项目级替换用户级（不是深合并字段）。先展开 `${ENV_VAR}` 占位符（递归），再校验为 `McpServerConfig`。缺失的环境变量直接抛 `KeyError`（fail fast，不静默跳过）。解析失败的单条 server 打 error 日志后跳过。

`.mcp.json` 格式示例:
```json
{
  "mcpServers": {
    "playwright": {
      "command": "C:\\Program Files\\nodejs\\npx.cmd",
      "args": ["-y", "@playwright/mcp"]
    },
    "duckduckgo-search": {
      "command": "C:\\Program Files\\nodejs\\npx.cmd",
      "args": ["-y", "duckduckgo-mcp-server"]
    },
    "excel": {
      "command": "C:\\Program Files\\nodejs\\npx.cmd",
      "args": ["--yes", "@negokaz/excel-mcp-server"]
    }
  }
}
```

`${ENV_VAR}` 占位符示例:
```json
{ "env": { "BRAVE_API_KEY": "${BRAVE_API_KEY}" } }
```

`include_tools` / `exclude_tools` 示例（正则匹配原始工具名）:
```json
{ "include_tools": ["search", "browser_.*"], "exclude_tools": ["dangerous_.*"] }
```

#### 6.3 Transport 适配器 (`mcp/_transport.py`)

**`async open_session(config: McpServerConfig, auth_headers: dict | None) → AsyncGenerator[ClientSession]`**

async context manager，按 `config.transport` 分发:

```
"stdio" → stdio_client(StdioServerParameters(command, args, env, cwd)) → ClientSession
"sse"   → sse_client(url, headers + auth_headers) → ClientSession
"websocket" → raise NotImplementedError
```

Session yield 在 transport 的 async-with 内部 → transport/subprocess 在 session 存活期间保持运行。`initialize()` 在首次创建时调用一次。

#### 6.4 连接管理与生命周期 (`mcp/client.py`)

##### `McpConnectionState` (dataclass)

| 字段 | 类型 | 说明 |
|------|------|------|
| `server_name` | `str` | 服务器名称 |
| `config` | `McpServerConfig` | 配置引用 |
| `session` | `Any` | 活跃的 `ClientSession`（duck-typed） |
| `tools` | `list[dict]` | `list_tools()` 的缓存结果，每个元素 `{"name", "description", "inputSchema"}` |
| `connected_at` | `float` | `time.monotonic()` 时间戳 |
| `connection_error` | `str \| None` | 连接失败时的错误信息 |
| `fatal` | `bool` | 是否为不可重试的错误（401/403/config 错误） |

##### `McpSessionManager`

query 作用域的单例管理器，用一个 `AsyncExitStack` 进入所有 transport/session 对。

| 方法 | 签名 | 说明 |
|------|------|------|
| `connect` | `async connect(name: str, config: McpServerConfig) → McpConnectionState` | 连接一个 server。成功时 session+transport 进入 `AsyncExitStack`。内部循环 `max_reconnect_attempts+1` 次，每次受 `asyncio.timeout(connect_timeout)` 保护。不可重试错误（401/403/config 错误）直接标记 `fatal=True` 停止重试。**约束**: 必须与 `aclose()` 在同一个 asyncio task 调用 |
| `refresh_tools` | `async refresh_tools(name: str) → list[dict]` | 重新 `list_tools()` 并更新缓存（用于 `notifications/list_changed`） |
| `aclose` | `async aclose() → None` | **必须在 connect 的同一个 task 里调用**。逆序退出 AsyncExitStack 中的所有 async context manager → 关闭所有 session → 关闭所有 transport 子进程/连接。清理 `connections` dict。 |

##### 错误分类（`_is_fatal_error`）

| 错误条件 | 结果 | 原因 |
|----------|------|------|
| HTTP 401 / 403 | fatal（不重试） | 认证失败，重试无意义 |
| HTTP 5xx | 重试 | 服务端临时故障 |
| `NotImplementedError` | fatal | 配置错误（如用了 websocket transport） |
| `FileNotFoundError` | fatal | command 路径错误 |
| `ValueError` | fatal | transport 字段校验失败 |
| 错误消息含 `unauthorized`/`forbidden`/`invalid_grant`/`invalid_client` | fatal | 关键字兜底 |

#### 6.5 MCP 工具 → Tool 子类桥接 (`mcp/registry.py`)

##### 命名规则

| 函数 | 输入 | 输出 | 说明 |
|------|------|------|------|
| `_mcp_tool_name(server, tool)` | `("excel", "read_data_from_excel")` | `"mcp__excel__read_data_from_excel"` | 构建名字空间化名称 |
| `_mcp_tool_name(server, tool)` | `("@playwright/mcp", "browser_navigate")` | `"mcp___playwright_mcp__browser_navigate"` | 非法字符 `@`/`/` 被替换为 `_` |
| `_parse_mcp_tool_name(name)` | `"mcp__github__list_issues"` | `("github", "list_issues")` | 反解析（目前未被外部调用） |

**Sanitization 规则**: `_INVALID_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_-]")` — 将不符合 provider 函数名 pattern（`^[a-zA-Z0-9_-]+$`）的字符逐字替换为 `_`。逐字替换保证 `_unregister_server_tools` 用的前缀 `mcp__{server}__` 仍是 sanitized 后名称的有效前缀。

##### `register_mcp_tools(registry, server_name, state) → dict[str, type[Tool]]`

为 `state.tools` 中每个工具动态创建 `Tool` 子类并注入 `registry`。

1. **移除旧条目**: 以 `mcp__{server_name}__` 为前缀 delete registry 中的旧工具。
2. **冲突检测**: 若原始工具名与内置工具同名 → 内置优先，MCP 工具仅以 namespaced 名注册（打印 warning）。
3. **动态类构建**: `_build_mcp_tool_class(namespaced, server_name, tool_def, state)` → 注册 → 返回 dict。

##### 动态 `Tool` 子类结构

`_build_mcp_tool_class` 动态创建一个 `type("MCPTool_{namespaced}", (Tool,), class_dict)`:

| 属性/方法 | 说明 |
|-----------|------|
| `name: ClassVar[str]` | `"mcp__excel__read_data_from_excel"`（sanitized，用于 LLM 侧） |
| `is_concurrency_safe` | `False`（MCP session 非线程安全） |
| `is_read_only` | `False` |
| `is_destructive` | `False` |
| `_server_name: str` | 原始 server name（`"excel"`） |
| `_mcp_tool_name: str` | **原始**工具名（`"read_data_from_excel"`），供 `call_tool` 使用 |
| `_session_getter: staticmethod` | 懒引用 `state.session`（重连后自动指向新 session） |
| `_input_model: type[BaseModel]` | Pydantic 输入模型，由 `inputSchema` 转换 |
| `input_schema() → dict` | **classmethod**。返回 server 提供的 `inputSchema`（非 object 时兜底为 `{"type":"object","additionalProperties":true}`）。**关键**: 无此方法则继承基类 `raise NotImplementedError` → convert_tools 崩溃 |
| `validate_input(dict) → BaseModel` | **classmethod**。通过 `_input_model` 校验输入 |
| `call(input, ctx) → ToolCallResult` | **async**。取 session → `session.call_tool(_mcp_tool_name, args)` → 提取 `list[ContentBlock]` 文本 → 返回 `ToolCallResult`。session 为 None 时返回错误结果 |

##### `_build_input_model(namespaced, tool_def) → type[BaseModel]`

将 MCP `inputSchema`（JSON Schema）转换为 Pydantic model:

| inputSchema 情况 | 结果 |
|-------------------|------|
| `{"type":"object","properties":{...}}` | 逐字段映射：`string→str, integer→int, number→float, boolean→bool, array→list, object→dict`，required 字段用 `...` 标记 |
| 类型为 list（如 `["string","null"]`） | 该字段 fallback 为 `Any` |
| 无 schema / 非 object / 空 properties | 创建 `raw: dict[str,Any]` 单字段 fallback 模型 |

##### 工具名 Sanitization vs 实际调用

```
LLM sees:      mcp___playwright_mcp__browser_navigate   (sanitized)
Registry key:  mcp___playwright_mcp__browser_navigate   (same)
call_tool:     "browser_navigate"                        (original, from _mcp_tool_name)
```

#### 6.6 MCP 在 query() 中的生命周期 (`query/loop.py`)

**核心契约**: `McpSessionManager.connect()` 和 `aclose()` 必须在同一个 asyncio task 中调用（anyio cancel-scope 要求）。`query()` 通过 `try/finally` 保证这点,且消费者层（`Agent.stream` / CLI REPL）都用 `aclose()` 显式关闭流:

```
query():
    mcp = McpSessionManager()
    try:
        await _bootstrap_mcp(mcp, config, registry)  # 连接 + 注册
        async for msg in _run_turns(...):             # 回合循环
            yield msg
    finally:
        await mcp.aclose()  # 必定在当前 task 执行，无论哪个 return 路径
```

`_bootstrap_mcp` 遍历所有 server,连接失败的打 warning 跳过,不阻塞整个 query。

`_run_turns` 是纯回合循环,不持有 mcp 引用 → 它的多个提前 `return` 路径不受影响。

#### 6.7 CLI 启动预检 (`cli.py`)

**`async _preflight_mcp(config: AgentConfig) → None`**

在 REPL 接受输入前,遍历所有配置的 server 做一次连接 + `list_tools` + 立即 `aclose()`:

1. **预热**: 触发 `npx -y` 包下载进 npm 缓存 → 第一个真实 query 不被冷下载阻塞。
2. **健康检查**: 坏掉的 server 立即打印 `[mcp] x <name>: <reason>` 到 stderr,不等到调用时才发现。

在 `_main()` 中 `await _preflight_mcp(config)` → `await _repl(config)`。预检使用独立的 `McpSessionManager`,连接不跨查询复用。

#### 6.8 OAuth 认证 (`mcp/auth.py`)

**`OAuthTokenManager`** — 与 transport 解耦,Manager 解析 bearer token → transport 注入 HTTP header。

| 方法 | 说明 |
|------|------|
| `get_valid_token() → str \| None` | 获取有效 token,必要时自动刷新。按序尝试: ① 已有 token 未过期 → 返回; ② 快过期 → refresh; ③ 无 token 但有 client_secret → client_credentials flow; ④ 都不满足 → 返回 None |
| `start_authorization_code_flow(code, verifier?) → str` | 用 authorization code 换 token（支持 PKCE） |
| `start_device_code_flow() → dict` | 发起设备码流程,返回 `{device_code, user_code, verification_uri}` |

**刷新策略**: token 进入最后 20% 生命周期时刷新（至少 60s 安全余量）。刷新失败 → token 置 None,不清除 refresh_token（下次可重试）。

#### 6.9 MCP 资源 & Prompt 获取 (`mcp/resources.py`)

当前为预留模块,Phase 3 会映射到 Skill 系统。

| 函数 | 说明 |
|------|------|
| `async fetch_resources(session) → list[dict]` | `list_resources()` → 返回 `[{uri, name, description}]` |
| `async read_resource(session, uri: str) → list[Any]` | `read_resource(uri)` → 返回 `contents` |

#### 6.10 公开导出 (`mcp/__init__.py`)

```python
from agent_core.mcp import (
    McpSessionManager,     # 连接生命周期管理
    register_mcp_tools,    # 动态工具注册
)
```

其余模块（config/transport/auth/resources）为内部实现,不直接对调用方暴露。

#### 6.11 MCP 故障排查

| 症状 | 可能原因 | 排查 |
|------|----------|------|
| `NotImplementedError` on every query | 动态 Tool 子类未定义 `input_schema()` → convert_tools 崩溃 | 检查 `registry.py` 中 `_build_mcp_tool_class` 是否挂了 `input_schema` classmethod |
| HTTP 400: `Invalid tools[N].function.name` | server name 含 `@` `/` 等非法字符 | 检查 `_mcp_tool_name` 是否做了 per-character sanitization |
| `RuntimeError: exit cancel scope in a different task` at shutdown | `mcp.aclose()` 在错误的 task 调用或在 shutdown 时才调用 | 检查 `query()` 是否有 `try/finally` 包裹,消费者是否显式 `aclose()` |
| `npx -y` download timeout | 首次下载大包超过 `connect_timeout` (30s) | 调大 `.mcp.json` 中该 server 的 `connect_timeout` |
| `KeyError: Environment variable 'X' referenced...` at startup | `.mcp.json` 用了 `${X}` 占位符但 X 未设 | 设置环境变量或改为硬编码值 |

#### 6.12 当前 `.mcp.json` 配置

项目根已配置 3 个 server,全免 API key,stdio transport:

| Server | 包 | 工具示例 | 说明 |
|--------|-----|----------|------|
| `playwright` | `@playwright/mcp` | `browser_navigate`, `browser_click`, `browser_snapshot` | 浏览器自动化 |
| `duckduckgo-search` | `duckduckgo-mcp-server` | `search` | 免费联网搜索 |
| `excel` | `@negokaz/excel-mcp-server` | `read_data_from_excel`, `write_data_to_excel` | Excel 读写 |

注册后的名字空间化工具名示例: `mcp__playwright__browser_navigate`, `mcp__duckduckgo-search__search`, `mcp__excel__read_data_from_excel`。



---

## Phase 3: 子Agent & 扩展 (Week 5-6) — #7 已实现, #8 已实现, #9 待实现

---

### 7. Agent 定义系统

**功能**: `AgentDefinition` 数据结构、加载器、优先级链、内置 Agent 注册。

**原因**: 不同场景需要不同配置的 Agent——探索代码用只读工具，执行任务用读写工具。AgentDefinition 是子 Agent 的"配置模板"，一次定义反复使用。

**实现文件**:

| 文件 | 职责 |
|------|------|
| `src/agent_core/agents/__init__.py` | 公开 API 聚合 + 触发内置 Agent 注册 |
| `src/agent_core/agents/definition.py` | `AgentDefinition` Pydantic 模型 + `AgentMemory` 枚举 |
| `src/agent_core/agents/registry.py` | `AgentRegistry` 四层优先级链注册表 + 合并算法 |
| `src/agent_core/agents/loader.py` | markdown frontmatter / JSON 加载器 + 目录发现 |
| `src/agent_core/agents/builtin/__init__.py` | 内置 Agent 注册触发点 |
| `src/agent_core/agents/builtin/general.py` | GeneralPurpose |
| `src/agent_core/agents/builtin/explore.py` | Explore |
| `src/agent_core/agents/builtin/plan.py` | Plan |

**测试文件**:

| 文件 | 说明 |
|------|------|
| `tests/unit/test_agent_definition.py` | 16 个单元测试（默认值/校验/序列化/exclude_unset） |
| `tests/unit/test_agent_registry.py` | 37 个单元测试（注册/合并/并集/覆盖/排除默认/unregister） |
| `tests/unit/test_agent_loader.py` | 13 个单元测试（JSON/Markdown 解析/发现/冲突/错误） |
| `tests/unit/test_builtin_agents.py` | 14 个单元测试（注册验证/属性校验/引擎免批准） |

---

#### 7.1 数据模型 (`agents/definition.py`)

##### `AgentMemory` 枚举

```python
class AgentMemory(str, Enum):
    USER = "user"          # 跨会话持久化到用户记忆
    PROJECT = "project"    # 项目级记忆
    LOCAL = "local"        # 仅当前会话有效
    NONE = "none"          # 无记忆
```

##### `AgentDefinition` Pydantic 模型

Agent 能力模板 —— 纯数据容器，不包含任何执行逻辑。执行引擎（Item 8）读取此定义构建运行时环境。

```python
class AgentDefinition(BaseModel):
    agent_type: str                          # 唯一标识符
    when_to_use: str | None = None           # LLM 路由决策描述
    tools: list[str] = ["*"]                 # 工具白名单
    disallowed_tools: list[str] = []          # 工具黑名单
    auto_approve_tools: list[str] = []        # 预授权免问工具
    system_prompt: str = ""                  # 注入子 Agent 的 system prompt
    model: str | None = None                 # 模型标识（None = 继承父级）
    permission_mode: PermissionMode | None = None  # 权限模式（None = 继承父级）
    mcp_servers: dict[str, McpServerConfig] = {}   # 专属 MCP 服务器
    hooks: list[Any] = []                    # 专属 Hook 实例（Item 9）
    skills: list[str] = []                   # 预加载 Skill 名称（Item 9）
    memory: AgentMemory = AgentMemory.NONE   # 记忆持久化模式（Item 10）
    background: bool = False                 # 是否默认后台异步执行（Item 8）
```

**字段详解**

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `agent_type` | `str` | **(必填)** | 唯一标识符，仅允许 `[a-zA-Z0-9_-]`。空字符串或含非法字符触发 `ValueError`。 |
| `when_to_use` | `str \| None` | `None` | LLM 看到的使用场景描述。AgentTool（Item 8）路由时匹配此文本。 |
| `tools` | `list[str]` | `["*"]` | 工具白名单。`["*"]` 展开为父 Agent 全部允许的工具。不能为空列表。 |
| `disallowed_tools` | `list[str]` | `[]` | 工具黑名单。合并时各层取**并集**。 |
| `auto_approve_tools` | `list[str]` | `[]` | **预授权免问工具清单**（第 3 层权限机制）。Item 8 构建子 Agent 的 `PermissionEngine` 时将每项编译为 `PermissionRule(action="allow")`，绕过 ask 弹窗。**安全约束**：工具仍须通过 `tools ∩ 父级允许集` 过滤；只放宽「弹不弹窗」，不放宽「能不能调」。合并时各层取**并集**。 |
| `system_prompt` | `str` | `""` | 注入子 Agent 的 system prompt。Item 8 中与父 system prompt + tool 列表拼接。 |
| `model` | `str \| None` | `None` | 模型标识符（如 `"anthropic/claude-haiku-4-5"`）。`None` 继承父 Agent 当前模型。 |
| `permission_mode` | `PermissionMode \| None` | `None` | 声明的权限模式。`None` 继承父模式。运行时实际 = `min(父, 此声明)`——不可提权。 |
| `mcp_servers` | `dict[str, McpServerConfig]` | `{}` | 专属 MCP 服务器。Item 8 中与父 Agent 合并（同名 server 子覆盖父）。 |
| `hooks` | `list[Any]` | `[]` | 专属 Hook 实例。Item 9 接入后生效。 |
| `skills` | `list[str]` | `[]` | 预加载的 skill 名称。Item 9 接入后生效。 |
| `memory` | `AgentMemory` | `NONE` | 子 Agent 记忆持久化模式。Item 10 接入后生效。 |
| `background` | `bool` | `False` | `True` 时默认以后台异步模式启动（Item 8）。 |

**校验规则**

| 字段 | 规则 | 违反时 |
|------|------|--------|
| `agent_type` | 非空 + `^[a-zA-Z0-9_-]+$` | `ValueError` |
| `tools` | 非空列表 | `ValueError` |

**`model_dump(exclude_unset=True)` 行为**

仅返回**显式赋过值**的字段。这是合并算法正确性的前提——避免高优先级层的默认值覆盖低优先级层的显式设置。

```python
d = AgentDefinition(agent_type="Min")
d.model_dump(exclude_unset=True)  # → {"agent_type": "Min"}

d2 = AgentDefinition(agent_type="T", tools=["Read"])
d2.model_dump(exclude_unset=True)  # → {"agent_type": "T", "tools": ["Read"]}
```

##### `AgentDefinition` vs `AgentConfig`

| 维度 | AgentConfig | AgentDefinition |
|------|-------------|-----------------|
| 用途 | 运行时配置 | 能力模板 |
| API key | ✅ 包含 | ❌ 不包含（继承父级） |
| max_turns | ✅ 包含 | ❌ 不包含（继承父级） |
| tools | ✅ 运行时过滤 | ✅ 能力声明 |
| system_prompt | ❌ 不包含 | ✅ 核心字段 |
| permission_mode | ✅ 运行时模式 | ✅ 声明模式（可被继承降级） |
| mcp_servers | ✅ 全局配置 | ✅ 专属 MCP（与父合并去重） |
| when_to_use | ❌ | ✅ LLM 路由提示 |

---

#### 7.2 注册表 (`agents/registry.py`)

##### `AgentRegistry` 类

多来源分层存储，四层优先级链（低→高）：`builtin` < `plugin` < `project` < `user`。

```python
class AgentRegistry:
    def __init__(self) -> None: ...
```

**方法**

| 方法 | 签名 | 说明 |
|------|------|------|
| `register` | `register(definition: AgentDefinition, source: str = "project") -> AgentDefinition` | 在指定层注册。同层同名覆盖。返回 definition 支持链式调用。 |
| `unregister` | `unregister(agent_type: str, source: str \| None = None) -> None` | 注销。`source=None` 时清除所有层。 |
| `get` | `get(agent_type: str) -> AgentDefinition` | 优先级链合并后返回。未找到抛 `KeyError`。 |
| `list_all` | `list_all() -> dict[str, AgentDefinition]` | 所有已知 agent_type 的合并结果。 |
| `list_by_source` | `list_by_source(source: str) -> dict[str, AgentDefinition]` | 返回特定层的原始定义（不合并）。 |
| `contains` | `contains(agent_type: str) -> bool` | 是否存在该 agent_type。 |

**优先级链合并算法 (`_merge`)**

```
输入: [builtin_def, project_def, user_def]  ← 低→高优先级
输出: 合并后的 AgentDefinition

预处理:
  对每层调用 layer.model_dump(exclude_unset=True)
  仅显式设置过的字段参与合并（避免默认值误覆盖）

规则:
1. agent_type 不变（所有层必须一致）
2. 标量字段 → 最高「显式设置」层覆盖（when_to_use, system_prompt, model,
   permission_mode, memory, background）
3. tools / skills / hooks → 最高「显式设置」层覆盖
4. disallowed_tools / auto_approve_tools → 所有层取并集
5. mcp_servers → 浅合并（高优先级同名 key 覆盖）
```

**便捷函数**（委托到模块级 `_default_registry` 单例）

| 函数 | 签名 | 说明 |
|------|------|------|
| `register_agent` | `register_agent(definition, *, source="project") -> AgentDefinition` | 装饰器/直接调用两用。内置 Agent 用直接调用：`register_agent(my_def, source="builtin")` |
| `get_agent` | `get_agent(agent_type: str) -> AgentDefinition` | 从全局注册表查询合并后的定义。未找到抛 `KeyError`。 |
| `list_agents` | `list_agents() -> dict[str, AgentDefinition]` | 列出全局注册表中所有合并后的定义。 |

**使用示例**

```python
from agent_core import AgentDefinition, get_agent, list_agents, register_agent

# 直接注册
my_agent = AgentDefinition(
    agent_type="my-reviewer",
    when_to_use="When code changes need review",
    tools=["Read", "Glob", "Grep"],
    system_prompt="You are a code reviewer...",
)
register_agent(my_agent, source="project")

# 查询
explore = get_agent("Explore")
print(explore.tools)  # → ["Read", "Glob", "Grep"]

# 列出所有
for name, d in list_agents().items():
    print(f"{name}: {d.when_to_use}")
```

---

#### 7.3 内置 Agent (`agents/builtin/`)

三个内置 Agent 在模块 import 时通过直接调用 `register_agent(definition, source="builtin")` 注册。

##### GeneralPurpose

```python
AgentDefinition(
    agent_type="GeneralPurpose",
    when_to_use="General-purpose agent for any task that doesn't require a specialist.",
    tools=["*"],
    system_prompt="You are a general-purpose assistant.",
    permission_mode=None,       # 继承父级
    memory=AgentMemory.NONE,
)
```

| 属性 | 值 |
|------|-----|
| tools | `["*"]`（全部工具，与父级求交） |
| permission_mode | `None`（继承父级） |
| auto_approve_tools | `[]` |
| 用途 | 默认 Agent，不特化 |

##### Explore

```python
AgentDefinition(
    agent_type="Explore",
    when_to_use=(
        "Subagent used whenever the task requires searching across multiple "
        "files, directories, or patterns..."
    ),
    tools=["Read", "Glob", "Grep"],
    system_prompt="You are a code exploration specialist...",
    permission_mode=None,       # 继承父级；只读免批准由引擎第 6 步保证
    memory=AgentMemory.LOCAL,
)
```

| 属性 | 值 |
|------|-----|
| tools | `["Read", "Glob", "Grep"]`（仅只读） |
| permission_mode | `None`（继承父级） |
| auto_approve_tools | `[]`（只读工具自动通过引擎第 6 步，无需预授权） |
| 用途 | 代码库探索，被频繁调用 |

##### Plan

```python
AgentDefinition(
    agent_type="Plan",
    when_to_use=(
        "Subagent used before writing any code..."
    ),
    tools=["Read", "Glob", "Grep"],
    system_prompt="You are a planning specialist...",
    permission_mode=None,
    memory=AgentMemory.LOCAL,
)
```

| 属性 | 值 |
|------|-----|
| tools | `["Read", "Glob", "Grep"]`（仅只读） |
| permission_mode | `None`（继承父级） |
| auto_approve_tools | `[]` |
| 用途 | 前置规划，产出文本计划 |

**只读免批准的真正机制**：Explore/Plan 之所以「无需用户批准」，**不是**因为 `permission_mode`，而是因为它们只持有 `is_read_only=True` 的工具（`Read`/`Glob`/`Grep`），命中 `PermissionEngine` 判定链第 6 步「只读兜底」——该步**与 mode 无关**，在任何父模式下都直接放行。

---

#### 7.4 加载器 (`agents/loader.py`)

所有加载器返回 `AgentDefinition | None`，解析失败时返回 `None`（不抛异常）。

| 函数 | 签名 | 说明 |
|------|------|------|
| `load_markdown_definition` | `load_markdown_definition(file_path: str) -> AgentDefinition \| None` | 从 `.md` 文件 YAML frontmatter 解析。依赖 `python-frontmatter`。无 `agent_type` 的 frontmatter 返回 `None`（debug 级日志静默跳过）。 |
| `load_json_definition` | `load_json_definition(file_path: str) -> AgentDefinition \| None` | 从 `.json` 文件解析。解码/校验失败返回 `None` + warning。 |
| `discover_agents` | `discover_agents(directory: str, source: str = "project", include_plain_md: bool = False) -> dict[str, AgentDefinition]` | 递归扫描目录。默认仅识别 `<name>.agent.md` 和 `<name>.agent.json`。`include_plain_md=True` 时也尝试裸 `.md`。同 agent_type 冲突时优先级 `.agent.md` > `.agent.json` > `.md`，warning 日志记录。 |

**Markdown frontmatter 格式示例**

```markdown
---
agent_type: code-reviewer
when_to_use: "When code changes need review"
tools:
  - Read
  - Glob
  - Grep
disallowed_tools:
  - Bash
system_prompt: |
  You are a code reviewer...
permission_mode: ask
model: anthropic/claude-haiku-4-5
---

# Code Reviewer (body text ignored)
```

**JSON 格式示例**

```json
{
  "agent_type": "code-reviewer",
  "when_to_use": "When code changes need review",
  "tools": ["Read", "Glob", "Grep"],
  "system_prompt": "You are a code reviewer..."
}
```

**发现路径约定**

| 来源 | 路径 |
|------|------|
| 项目级 | `<workspace_root>/.agent/agents/` |
| 用户级 | `~/.agent/agents/` |

---

#### 7.5 公开导出 (`agent_core/__init__.py` 增量)

```python
from agent_core import (
    AgentDefinition,
    AgentMemory,
    get_agent,
    list_agents,
    register_agent,
)
```

其余模块（registry/loader/builtin）为内部实现，通过 `agents/__init__.py` 聚合后对调用方暴露。

---

#### 7.6 消费契约实现 (`agents/resolve.py`)

上述 5 条契约已落地为 6 个纯函数，由 `run_agent()`（Item 8）调用。全部无副作用、可独立测试。

**实现文件**: `src/agent_core/agents/resolve.py`

| 函数 | 签名 | 说明 |
|------|------|------|
| `resolve_agent_tools` | `(definition, parent_allowed, parent_disallowed) -> tuple[frozenset, frozenset]` | 返回 (child_allowed, child_disallowed)。`tools=["*"]` 展开为父 allowed；disallowed 取 ∪ 并集；allowed 扣掉 disallowed。 |
| `build_child_engine` | `(definition, parent_mode, parent_disallowed) -> PermissionEngine` | 构建子引擎：mode 经 `compute_child_permission_mode` 钳制；`auto_approve_tools` 过滤掉父禁用项后编译为 `priority=0` 的 `PermissionRule(action="allow")`。 |
| `merge_mcp_servers` | `(definition, parent_mcp) -> dict[str, McpServerConfig]` | 浅合并，子覆盖父同名 key。 |
| `build_child_system_prompt` | `(definition, parent_system, tool_names) -> str` | 拼接 `{子 prompt}\n{工具列表}\n{父 prompt}`。 |
| `resolve_model` | `(definition, parent_model_id) -> ModelId` | `definition.model=None` 继承父模型，否则 `ModelId.parse`。 |
| `disallow_agent_at_depth_limit` | `(allowed, depth) -> frozenset[str]` | 深度 ≥ `MAX_AGENT_DEPTH` (3) 时从 allowed 中剔除 `"Agent"`，防止无限递归。 |

**安全不变量**（由 `inheritance.py` + `resolve.py` 保证）:
- 子 Agent **永远不能**提升自身权限（`min(父, 子)` 钳制）
- 子 Agent **永远不能**使用父禁止的工具（`disallowed_tools` 取并集）
- 子 Agent **只能**使用父子双方都允许的工具（`tools` 取交集）
- `auto_approve_tools` 中的工具若父禁用则不编入 allow 规则

**单元测试**: `tests/unit/test_agent_resolve.py` (27 个测试：工具池过滤/交集/∪并集/dedup/空交集/权限构建/auto_approve 编译/父禁用排除/mode 钳制/继承/MCP 合并/SystemPrompt 拼接/模型继承/模型覆盖/深度闸)。

---

### 8. 子Agent 执行引擎 & AgentTool — 已实现

**功能**: `run_agent()` 异步生成器（隔离执行环境 + 独立 MCP + 独立 System Prompt + sidechain 转录），`AgentTool` 让 LLM 通过 tool_use 创建子 Agent（同步/异步/Fork/恢复）。

**实现文件**:

| 文件 | 职责 |
|------|------|
| `src/agent_core/agents/run.py` | `run_agent()` 核心执行引擎 (~250 行) |
| `src/agent_core/agents/tool.py` | `AgentTool` — LLM 可调用的 tool_use 实现 (~240 行) |
| `src/agent_core/agents/lifecycle.py` | 异步生命周期管理 + 进度摘要 + 通知 (~160 行) |
| `src/agent_core/agents/fork.py` | Fork/恢复逻辑 + `filter_incomplete_tool_calls` (~130 行) |
| `src/agent_core/agents/resolve.py` | 消费契约纯函数 (§7.6) |
| `src/agent_core/session/state.py` | `SessionState` — 跨 prompt 对话历史 + 权限记忆延续 |

**测试文件**:

| 文件 | 说明 |
|------|------|
| `tests/unit/test_agent_resolve.py` | 27 个单元测试（工具池/权限/MCP/SysPrompt/模型/深度） |
| `tests/unit/test_agent_tool.py` | 9 个单元测试（输入校验/属性/路由拒绝/深度闸/模型别名） |
| `tests/unit/test_agent_lifecycle.py` | 10 个单元测试（状态/完成/通知 XML/XML 转义/进度摘要） |
| `tests/unit/test_agent_fork.py` | 6 个单元测试（不完整 tool_use 过滤/空 assistant 移除/Fork 不可变） |
| `tests/unit/test_session_state.py` | SessionState 快照/记录/清空 |

#### 8.1 `run_agent()` — 子 Agent 核心执行引擎 (`agents/run.py`)

```python
async def run_agent(
    prompt: str,
    definition: AgentDefinition,
    *,
    parent_config: AgentConfig,
    parent_trace_id: str,
    parent_permission_callback: PermissionCallback | None = None,
    parent_messages: list[dict[str, Any]] | None = None,
    parent_system: str = "",
    parent_allowed_tools: frozenset[str] | None = None,
    parent_disallowed_tools: frozenset[str] | None = None,
    parent_mcp_servers: dict[str, McpServerConfig] | None = None,
    parent_context: ToolUseContext | None = None,
) -> AsyncGenerator[AgentMessage, None]:
```

**执行流程**:

1. **配置解析** — `resolve_model()` 确定模型；`resolve_agent_tools()` 计算 (allowed, disallowed)；`disallow_agent_at_depth_limit()` 深度闸；`compute_child_permission_mode()` 权限钳制；`build_child_engine()` 构造权限引擎
2. **隔离环境** — 创建独立 `ToolUseContext`（`copy.deepcopy` read_file_state，新 `abort_signal`）；独立 `ToolRegistry`（仅允许的工具）；子 `AgentConfig`（权限模式、MCP）；子权限回调 wrapper
3. **open sidechain** — 在 `~/.agent/sessions/{parent_trace_id}/{agent_id}.jsonl` 异步 append
4. **调用 `query()`** — 传入 `system`（§2.4 拼好的子 prompt）和 `initial_messages`（fork 路径）；聚合同一 turn 的 `assistant_text` + `tool_use` → 一条 assistant chat message → 写入 sidechain
5. **finally 清理** — flush + close sidechain 文件

#### 8.2 `AgentTool` — LLM 可调用的子 Agent 工具 (`agents/tool.py`)

继承 `Tool` 基类，通过 `@register_tool` 注册到全局 `_default_registry`。

```python
class AgentTool(Tool):
    name = "Agent"
    is_concurrency_safe = False
    is_read_only = False
    is_destructive = True     # 子 Agent 可能执行写操作
```

**Input Schema** (`AgentInput` Pydantic 模型):

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `description` | `str` | **(必填)** | 3-5 词任务简述 |
| `prompt` | `str` | **(必填)** | 子 Agent 任务 prompt |
| `subagent_type` | `str` | `"GeneralPurpose"` | Agent 类型名（`Explore`/`Plan`/`GeneralPurpose`） |
| `model` | `str \| None` | `None` | 模型别名覆写（`"sonnet"`/`"opus"`/`"haiku"`） |
| `run_in_background` | `bool` | `False` | 后台异步执行 |

**模型别名映射**:

```python
_MODEL_ALIASES = {
    "sonnet": "anthropic/claude-sonnet-4-5",
    "opus":   "anthropic/claude-opus-4-7",
    "haiku":  "anthropic/claude-haiku-4-5",
}
```

**路由逻辑**:
- 深度检查：`agent_depth >= MAX_AGENT_DEPTH` → 拒绝 + error
- 查找 `AgentDefinition`：未知 `subagent_type` → `KeyError` error
- 模型别名翻译：短名 → 完整 `provider/model`
- 同步路径（`_run_sync`）：内联迭代 `run_agent()`，聚合文本返回
- 异步路径（`_run_async`）：`run_async_lifecycle()` 作为 tracked task 启动，立即返回 `{"status":"async_launched","agent_id":"..."}`

**BackgroundAgentRegistry**: 每个 `ToolUseContext` 可关联一个注册表，跟踪 in-flight 后台任务，`drain()` 等待全部完成。

**递归约束**: `MAX_AGENT_DEPTH=3`。`AgentTool.call()` 和 `disallow_agent_at_depth_limit()` 双重把关。

#### 8.3 异步生命周期 (`agents/lifecycle.py`)

```python
@dataclass
class AgentLifecycleState:
    agent_id: str
    status: Literal["running", "completed", "failed", "killed"] = "running"
    total_tokens: int = 0
    tool_use_count: int = 0
    latest_activity: str = "Initializing..."
    started_at: float
    completed_at: float | None = None
    result: str = ""
    error: str | None = None
```

**核心函数**:

| 函数 | 说明 |
|------|------|
| `run_async_lifecycle(agent_id, definition, prompt, context)` | 后台完整生命周期：消费 `run_agent()` stream → 更新 state → `finalize` → `enqueue` |
| `_progress_summarizer(state, interval=30.0)` | 每 30s 从 `state.recent_texts` 提取 3-5 词摘要 |
| `finalize_agent_tool(state)` | 设置 `completed_at`，结果回填 |
| `enqueue_agent_notification(state, queue)` | 生成 XML `<task-notification>`（含 `agent_id`/`status`/`total_tokens`/`result`），XML 转义动态文本，追加到父级 `notification_queue` |

**通知格式**:
```xml
<task-notification>
  <agent_id>abc123</agent_id>
  <status>completed</status>
  <total_tokens>1234</total_tokens>
  <result>All tests passed</result>
</task-notification>
```

父 Agent 的 `query()` 主循环每轮开始时检查 `notification_queue`，非空则作为 user message 注入 `messages`。

#### 8.4 Fork & 恢复 (`agents/fork.py`)

| 函数 | 说明 |
|------|------|
| `filter_incomplete_tool_calls(messages)` | 扫描所有 `tool_use`/`tool_result` block，移除无对应 `tool_result` 的 `tool_use`，删除变空的 assistant 消息。返回新列表不修改输入。 |
| `fork_messages(parent_messages)` | `filter_incomplete_tool_calls(list(parent_messages))` 的便捷封装 |
| `resume_agent(agent_id, sidechain_path, definition, ...)` | 从 sidechain JSONL 加载 transcript → 重建 messages → `run_agent()` 继续。`read_file_state` 重置为 `{}`（stale 标记）。 |

---

### 9. Hooks + Plugin + Skills 系统 (5–7 天) — ✅ 已实现

**功能**: Hook 管线（可阻断/可修改输出），Plugin 热插拔注册表，Skill 定义与执行（inline / fork 两种模式）。

**原因**: 这三个是 Agent 的扩展机制。Hook 让外部代码介入工具执行；Plugin 让能力可热插拔；Skills 让用户无需写代码就能定义可复用工作流。

**实现文件**:

| 文件 | 职责 |
|------|------|
| `src/agent_core/hooks/__init__.py` | 公开导出（4 种 Protocol + 数据模型 + 管线执行器） |
| `src/agent_core/hooks/types.py` | Protocol 定义（PreToolUseHook / PostToolUseHook / StopHook / SubagentStartHook）+ HookBlock / HookModify / HookContinue / StopResult |
| `src/agent_core/hooks/registry.py` | HookRegistry — 显式 `hook_kind` 分类 + 优先级桶 + `copy_with_overrides()` 子Agent 继承 + `_default_hook_registry` 只读模板 |
| `src/agent_core/hooks/pipeline.py` | 管线执行器：`run_pre_tool_use_hooks` / `run_post_tool_use_hooks` / `run_stop_hooks` / `run_subagent_start_hooks` |
| `src/agent_core/plugins/__init__.py` | 公开导出 |
| `src/agent_core/plugins/base.py` | Plugin 抽象基类（可提供 tools / hooks / agent_definitions / skills） |
| `src/agent_core/plugins/registry.py` | PluginRegistry — `entry_points(group="agent_core.plugins")` 发现 + 手动注册 |
| `src/agent_core/plugins/lifecycle.py` | `safe_enable_plugins()` / `safe_disable_all()` — 错误隔离 |
| `src/agent_core/skills/__init__.py` | 公开导出 |
| `src/agent_core/skills/definition.py` | SkillDefinition Pydantic 模型（name / tools / model / execution_mode / parameters / background） |
| `src/agent_core/skills/registry.py` | SkillRegistry — `register(source=)` / `list_by_source()` / 首遇优先覆盖 |
| `src/agent_core/skills/loader.py` | `load_skill_from_markdown()` + `discover_skills()` — `<dir>/<name>/SKILL.md` 约定 |
| `src/agent_core/skills/executor.py` | `render_template()` 容忍空格展开 + `execute_skill_inline()` / `execute_skill_fork()` — 均走 `run_agent()` |
| `src/agent_core/skills/tool.py` | SkillTool（name="Skill", `is_destructive=True`）— LLM tool_use 触发 skill |
| `src/agent_core/skills/permissions.py` | SkillPermissionEngine — fnmatch 模式匹配 allow/deny/ask |

**测试文件**:

| 文件 | 说明 |
|------|------|
| `tests/unit/test_hooks.py` | 36 个单元测试（类型模型 / 注册表路由+优先级+继承 / pipeline 执行 / 短路语义） |
| `tests/unit/test_plugins.py` | 18 个单元测试（基类默认值 / 注册+enable+disable+注入 / 错误边界） |
| `tests/unit/test_skills.py` | 24 个单元测试（定义模型 / registry CRUD / loader 解析+发现 / 模板渲染 / 权限匹配） |

---

#### 9.1 Hooks — Protocol 定义 (`hooks/types.py`)

Hook 分类靠**显式 `hook_kind` ClassVar**（四种 Protocol 仅有 `__call__`，`@runtime_checkable`+`isinstance` 全部互相匹配，不可用于分类）。

```python
HookKind = Literal["pre_tool_use", "post_tool_use", "stop", "subagent_start"]

class HookBase:
    hook_kind: ClassVar[HookKind]

# -- 返回类型 --
class HookBlock(BaseModel):
    action: Literal["block"] = "block"
    reason: str = ""
    message: str | None = None  # 注入对话的提示

class HookModify(BaseModel):
    action: Literal["modify"] = "modify"
    new_result: Any  # 替换后的 ToolCallResult.content 或 tool_input dict

class HookContinue(BaseModel):
    action: Literal["continue"] = "continue"

HookResult = HookContinue | HookBlock | HookModify

class StopResult(BaseModel):
    should_stop: bool = False
    reason: str = ""
    message: str | None = None
```

**四种 Protocol**:

| Protocol | hook_kind | 挂点 | 返回语义 |
|----------|-----------|------|---------|
| `PreToolUseHook` | `"pre_tool_use"` | `run_tool_use()` 步骤 5（权限之后，执行之前） | `HookModify` 改 input 后**重跑 bash_guard + 权限** |
| `PostToolUseHook` | `"post_tool_use"` | `run_tool_use()` 步骤 7（执行之后，**persist 之前**） | 对完整 `ToolCallResult` 操作 |
| `StopHook` | `"stop"` | `_run_turns()` **turn 循环顶部**（notification drain 后、budget 前） | `should_stop=True` → `DoneMessage("end_turn")` 提前终止 |
| `SubagentStartHook` | `"subagent_start"` | `AgentTool.call()` 步骤 4（子 Agent 启动前） | `HookBlock` 拒绝启动 |

---

#### 9.2 HookRegistry (`hooks/registry.py`)

Per-query 实例（挂在 `ToolUseContext.hook_registry`），**非进程级单例**。模块级 `_default_hook_registry` 仅作只读模板。

```python
class HookRegistry:
    def register(self, hook, priority: int = 100) -> None: ...
    def register_all(self, hooks: list, priority: int = 100) -> None: ...
    def pre_tool_use_hooks(self) -> list: ...      # 按 priority 升序
    def post_tool_use_hooks(self) -> list: ...
    def stop_hooks(self) -> list: ...
    def subagent_start_hooks(self) -> list: ...
    def is_empty(self) -> bool: ...                # 零开销短路
    def copy_with_overrides(self, extra_hooks: list) -> "HookRegistry": ...
```

- 排序三元组 `(priority, monotonic_seq, hook)` — 同 priority 按注册顺序，永不去对比 hook 对象本身
- `copy_with_overrides()` 深拷贝 + 追加 `extra_hooks`（priority=100），用于子 Agent 隔离

#### 9.3 管线集成

##### `run_tool_use()` — 步骤 3~5 收敛 + post-hooks

```
步骤 3~5 收敛循环:
  effective_input = tool_use.tool_input
  _guard_and_permit(effective_input)          # bash_guard + 权限
  if hook_registry:
      modified_input, block_reason = run_pre_tool_use_hooks(...)
      if block_reason → error ToolResult
      if modified_input:
          re-validate + 重跑 _guard_and_permit  # 防提权

步骤 6: execute
步骤 7: post-hooks (对完整 ToolCallResult, persist 之前)
步骤 8: persist
```

##### `_run_turns()` — Stop hook

每个 turn 顶部（notification drain 后、budget/truncation 前）调用 `run_stop_hooks()`。`should_stop=True` 时先 yield `AssistantText(result.message)` 再 yield `DoneMessage("end_turn")`。

##### `agents/run.py` — 子 Agent hook 继承

```python
parent_hr = getattr(parent_context, "hook_registry", None)
child_hook_registry = parent_hr.copy_with_overrides(definition.hooks) if parent_hr else HookRegistry()
child_context.hook_registry = child_hook_registry
```

##### `agents/tool.py` — SubagentStart hook

`AgentTool.call()` 中，`_run_sync` / `_run_async` 之前调用 `run_subagent_start_hooks()`。block 时返回 `is_error=True` 的 `ToolCallResult`。

##### `agent.py` — 启动注入

`Agent.stream()` 构造 per-query `HookRegistry`：`_default_hook_registry.copy_with_overrides(config.hooks)` → 传入 `query(hook_registry=...)`。`query()` 内部若 `hook_registry=None` 也自动执行此逻辑，覆盖 CLI/测试/子Agent 调用路径。

#### 9.4 Plugin (`plugins/`)

```python
class Plugin(ABC):
    name: ClassVar[str]                       # 唯一标识符
    version: ClassVar[str] = "0.1.0"
    async def on_enable(self) -> None: ...
    async def on_disable(self) -> None: ...
    def get_tools(self) -> list[type[Tool]]: return []
    def get_hooks(self) -> list: return []
    def get_agent_definitions(self) -> list[AgentDefinition]: return []
    def get_skills(self) -> list: return []

class PluginRegistry:
    GROUP_NAME = "agent_core.plugins"
    def discover(self) -> list[str]: ...           # entry_points 扫描
    def register(self, plugin_cls) -> None: ...
    async def enable(name, *, tool_registry, hook_registry, skill_registry, agent_registry) -> None: ...
    async def disable(name, ...) -> None: ...
```

- `enable()` 的四个注册表**显式传入**（per-query hook/skill 实例，不是全局单例）
- `query()` 启动时：`PluginRegistry.discover()` → `safe_enable_plugins(auto_enable_plugins, ...)` → 贡献注入到本次 per-query 注册表

#### 9.5 Skills (`skills/`)

##### SkillDefinition (`skills/definition.py`)

```python
class SkillParameter(BaseModel):
    type: str = "string"
    description: str = ""
    required: bool = False

class SkillDefinition(BaseModel):
    name: str                              # 唯一标识符
    description: str = ""
    tools: list[str] = ["*"]
    model: str | None = None
    permission_mode: str | None = None
    system_prompt: str = ""
    prompt: str = ""                       # SKILL.md body（模板文本）
    parameters: dict[str, SkillParameter] = {}
    execution_mode: Literal["inline", "fork"] = "inline"
    background: bool = False
```

##### SKILL.md 格式

```
<dir>/<skill-name>/SKILL.md
```

YAML frontmatter（name, description, tools, execution_mode, parameters）+ Markdown body（`{{ param }}` 模板）。

##### 执行器 (`skills/executor.py`)

两种模式**均走 `run_agent()`**（不重入当前 turn 循环）：

| 模式 | 消息历史 | 用途 |
|------|----------|------|
| `inline` | `parent_messages=context.messages` | 需当前对话上下文的 skill |
| `fork` | 无 parent_messages（全新历史） | 隔离、自包含的 skill |

```python
def render_template(template, arguments, declared) -> str: ...
async def execute_skill_inline(skill, arguments, context) -> ToolCallResult: ...
async def execute_skill_fork(skill, arguments, context) -> ToolCallResult: ...
```

##### SkillTool (`skills/tool.py`)

`@register_tool`，name=`"Skill"`，`is_destructive=True`。

```python
class SkillTool(Tool):
    name = "Skill"
    is_destructive = True   # skill 可能跑 Write/Edit/Bash

class SkillInput(BaseModel):
    skill: str
    arguments: dict[str, str] = {}
    mode: Literal["inline", "fork"] | None = None
```

内部流程：resolve Registry → render_template → execute_skill_inline/fork。

##### Skill 权限 (`skills/permissions.py`)

```python
class SkillPermissionEngine:
    def check(self, skill_name: str) -> str: ...  # "allow" | "deny" | "ask"

def default_skill_permissions() -> SkillPermissionEngine: ...
```

fnmatch 匹配（`review:*` / `dangerous-*` / `*`），低 priority 优先命中。默认规则 `* → ask`。

##### 在 query() 中的注入

`_run_turns()` 启动时 `discover_skills()` 扫描三个来源（config.skills_dir > project .agent/skills > user ~/.agent/skills）→ 首遇优先注册到 per-query `SkillRegistry` → 挂到 `ToolUseContext`。`SkillTool.call()` 通过 `_get_skill_registry(context)` 读取。

#### 9.6 CLI 集成

- `/skills` — 列出所有已发现 skill（名称、模式、描述）
- `/help` 已更新包含 `/skills`

#### 9.7 AgentConfig 新增字段 (`models.py`)

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `auto_enable_plugins` | `list[str]` | `[]` | Agent 启动时自动激活的插件名列表 |

#### 9.8 ToolUseContext 新增字段 (`tools/base.py`)

```python
hook_registry: Any = None  # HookRegistry | None (§Item 9)
```

#### 9.9 公开导出 (`agent_core/__init__.py` 增量)

```python
from agent_core import (
    # Hooks
    HookRegistry, PreToolUseHook, PostToolUseHook, StopHook, SubagentStartHook,
    HookBlock, HookModify, HookContinue, HookResult, StopResult,
    # Plugin
    Plugin, PluginRegistry,
    # Skills
    SkillDefinition, SkillRegistry, SkillTool,
)
```

---

## Phase 4: 持久化 & 收尾 (Week 7)

### 10. 会话持久化 & 恢复 (2 天)

**功能**: JSONL 转录存储（主会话 + 子 Agent sidechain），会话列表与恢复，格式迁移。

**原因**: 对话需要跨进程存活。JSONL 是 append-only 格式，安全简单，崩溃时最多丢失最后一行。

> **详细设计**: 见 [plan/session-state-and-history-plan.md](./plan/session-state-and-history-plan.md) §3（Phase B）。本条目 = 该计划的 **Phase B**（落盘 + 恢复）。

> **前置依赖（Phase A，必须先做）**: 同计划 §2 的**会话运行时层 `SessionState`**（内存中跨 prompt 的对话历史延续）。当前 REPL 每个 prompt 独立、历史不延续（`run_query` 累积 `messages` 但 `DoneMessage` 不回吐、`cli._repl` 不传 `initial_messages`）。Phase B 的落盘以 `SessionState.messages` 为唯一事实源——没有它，落盘无源可写。`SessionState` 同时持有会话权限记忆（见 [plan/permission-session-scope-plan.md](./plan/permission-session-scope-plan.md)），三者建议合并实施。

**Phase A — 会话运行时层（前置）** — ✅ 已实现
- [x] `src/agent_core/session/state.py`：`SessionState`（`session_id` / `messages` / `permission_store` / `transcript_path`）
- [x] `DoneMessage` 增 `final_messages` 字段；`run_query` 各 `yield DoneMessage` 回吐轮末 `messages`
- [x] `Agent(config, session=None)`；`stream` 注入 `initial_messages` + 回写 `session.messages`
- [x] `cli._repl` 循环外创建 `SessionState` 并注入每个 Agent；`/clear` 清空历史
- [x] 会话权限记忆（`SessionPermissionStore`）挂载于 `SessionState.permission_store`，注入链贯通
- [~] `run.py` 父历史来源：当前子 Agent 仍由 `parent_context.messages` 提供（语义不变），未额外改动

**Phase B — 落盘 & 恢复（本条目主体）**
- [ ] 存储路径：`~/.agent/projects/{project_hash}/{session_id}.jsonl`
- [ ] Sidechain 路径：`~/.agent/projects/{project_hash}/{session_id}/{agent_id}.jsonl`（复用现有 `run.py` sidechain）
- [ ] 写入策略：每条 message 产生时 `aiofiles` append 一行；writer 挂在 `SessionState` 单一写入口
- [ ] 消息序列化格式（同构 cc-haha）：每行 `{"uuid", "parentUuid", "sessionId", "timestamp", "isSidechain", "message": {role, content}}`；`parentUuid` 串成链（v1 线性，预留 fork/branch）
- [ ] 会话列表：扫描存储目录 → 按 mtime 排序 → 提取首条 user message 作为标题
- [ ] 会话恢复：读 JSONL → 按 `parentUuid` chain-walk 到 leaf → **清洗**（`filter_unresolved_tool_uses` 丢弃未配对 tool_use、`filter_orphaned_thinking`、`detect_turn_interruption` 检测 mid-turn 中断并可注入 "Continue…"）→ 反序列化 → 塞入新 `SessionState.messages` → 重建 `ToolUseContext`（read_file_state 失效标记）→ `query()` 继续
- [ ] 格式迁移：`migrate_transcript()` 函数，version 字段驱动，旧格式自动升级
- [ ] 会话删除 / 清理：删除对应 JSONL 文件，清理 sidechain 子目录
- [ ] CLI：`--resume <id>` / `--continue`（最近会话）/ `/sessions`（列表）

---

### 11. 测试、文档 & 发布 (2 天) — 部分实现

**功能**: 多层测试（单元 + mock 集成 + vcrpy 录制回放），MkDocs API 文档，PyPI 发布配置。

**原因**: 可被第三方依赖的库需要可信的测试覆盖。vcrpy 覆盖 LLM 调用确定性场景，mock model 覆盖并发/竞态/隔离逻辑，两者互补。

- [x] **单元测试**（pytest） — 现有 ~755 个单元测试（48 个测试文件），覆盖：
  - 每个 Tool 的 validate_input / call / 边界条件
  - AgentConfig / ModelId / AgentMessage 模型校验
  - AgentDefinition / AgentRegistry / 加载器 / 内置 Agent
  - PermissionEngine（7 步链）/ SessionPermissionStore / 权限继承 / Bash AST
  - TokenBudget / hard_truncate / enrich_context
  - Anthropic / OpenAI / DeepSeek provider 客户端
  - 压缩触发 / 提示词 / 一致性验证
  - Agent 生命周期 / Fork / resolve / AgentTool
  - SessionState
  - Hooks（36 个：类型模型 / 注册表 / pipeline / 短路语义）
  - Plugins（18 个：基类 / 注册+enable+disable / 注入 / 错误边界）
  - Skills（24 个：定义模型 / registry / loader / 模板渲染 / 权限）
- [x] **mock model 集成测试**（已实现 `MockModel` + `tests/integration/`）：`test_query_loop.py`（单轮/多轮/max_turns/error）、`test_tool_pipeline.py`（并发/串行）、`test_compression.py`（query loop 压缩路径）
- [ ] **vcrpy 录制回放测试** — 桩已创建（`tests/e2e/test_turn_flow.py` / `test_compression.py`），未录制
- [ ] **Mock 集成测试补齐**: `test_subagent.py` / `test_fork.py` / `test_permission_inheritance.py` 占位桩
- [ ] MkDocs Material API 文档搭建
- [ ] `pyproject.toml` + GitHub Actions PyPI 发布 workflow
- [ ] README 包含快速开始示例

---

## 统计

| 阶段 | 条目 | 预估天数 | 状态 |
|------|------|----------|------|
| Phase 1: 基础设施 | #1 脚手架 (2) + #2 Tool 框架 (2) + #3 多Provider集成 (3) | 7 | ✅ 已完成 |
| Phase 2: Agent 核心循环 | #4 主循环 (4) + CLI测试入口 (1) + #5 工具管线+权限 (4) | 9 | ✅ 已完成 |
| Phase 2: 压缩 & MCP | #5.5 压缩 (2) + #6 MCP (2) | 4 | ✅ 已完成 |
| Phase 3: Agent 定义 | #7 Agent 定义 (2) | 2 | ✅ 已完成 |
| Phase 3: 执行引擎 | #8 执行引擎+AgentTool+生命周期+Fork (5) | 5 | ✅ 已完成 |
| Phase 3: 扩展 | #9 Hooks/Plugin/Skills (5–7) | 5–7 | ✅ 已完成 |
| Phase 4: 持久化 | #10 会话运行时层 Phase A (1.5) | 1.5 | ✅ 已完成 |
| Phase 4: 持久化 | #10 落盘+恢复 Phase B (2) | 2 | ⬜ 待实现 |
| Phase 4: 收尾 | #11 测试补齐+文档+发布 (2) | 2 | 🔶 部分完成（测试 ~80%，文档/发布 0%） |
| **总计** | **13 条目** | **37.5–39.5 天 (~8 周)** | 已完成 ~78% |

---

## 核心依赖

```toml
[project]
name = "agent-core"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "aiofiles>=24",              # 异步文件写入
    "pydantic>=2",               # 配置校验 + Tool schema
    "python-frontmatter>=1",     # Skill/Agent markdown 解析
    "anthropic>=0.60,<1",        # Anthropic API SDK (开发时取最新 stable)
    "openai>=2,<3",              # OpenAI API SDK (开发时取最新 stable)
    "mcp>=1.29,<2",              # MCP 协议 SDK
    "tenacity>=9",               # 重试策略
    "tree-sitter>=0.24",         # AST 运行时核心
    "tree-sitter-bash>=0.23",    # bash 语法包
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.25",
    "vcrpy>=7",
]
```

---

## 项目目录结构

```
agent-core/
├── pyproject.toml                  # uv 项目配置 + 依赖声明
├── README.md                       # 快速开始 (4 个示例)
├── LICENSE
│
├── src/
│   └── agent_core/
│       ├── __init__.py             # 公开 API: Agent, AgentConfig, AgentResult, AgentMessage, ModelProvider, ModelId
│       │
│       ├── agent.py                # Agent 主类 (run / stream / on_permission_check)
│       ├── models.py               # 所有 Pydantic 数据模型 (AgentConfig, AgentResult, AgentMessage, AgentDefinition, 等)
│       ├── cli.py                  # 交互式 REPL 测试入口
│       │
│       ├── agents/                 # Agent 定义 + 执行引擎（详见第 7-8 节）
│       │   ├── __init__.py         # 公开导出 + 触发内置 Agent 注册
│       │   ├── definition.py       # AgentDefinition Pydantic 模型 + AgentMemory 枚举
│       │   ├── registry.py         # AgentRegistry 四层优先级链 + 合并算法
│       │   ├── loader.py           # markdown frontmatter / JSON 加载器 + 目录发现
│       │   ├── resolve.py          # §7.6 消费契约（工具池过滤/权限/MCP/SysPrompt/模型/深度闸）
│       │   ├── run.py              # §8.1 run_agent() 子 Agent 核心执行引擎
│       │   ├── tool.py             # §8.2 AgentTool — LLM 可调用的子 Agent 工具
│       │   ├── lifecycle.py        # §8.3 异步生命周期管理 + 通知
│       │   ├── fork.py             # §8.4 Fork/恢复 + filter_incomplete_tool_calls
│       │   └── builtin/            # 内置 Agent 定义
│       │       ├── __init__.py     # import 触发注册
│       │       ├── general.py      # GeneralPurpose
│       │       ├── explore.py      # Explore
│       │       └── plan.py         # Plan
│       │
│       ├── query/                  # Agent 主循环
│       │   ├── __init__.py
│       │   ├── loop.py             # query() 异步生成器 (turn 循环核心 + 工具执行管线)
│       │   ├── budget.py           # Token 预算管理 + 收益递减检测
│       │   ├── context.py          # 上下文富化 (git status, 日期)
│       │   └── truncate.py         # 硬截断逻辑
│       │
│       ├── tools/                  # 工具系统
│       │   ├── __init__.py         # Tool 注册表 + @register_tool 装饰器
│       │   ├── base.py             # Tool 抽象基类 / ToolUseContext / ToolCallResult
│       │   ├── registry.py         # ToolRegistry 注册表类
│       │   ├── _io.py              # 文件 I/O 共享工具 (指纹, 原子写入, 编码检测, workspace 检查)
│       │   ├── file_read.py        # FileReadTool (Read)
│       │   ├── file_edit.py        # FileEditTool (Edit)
│       │   ├── file_write.py       # FileWriteTool (Write)
│       │   ├── glob_tool.py        # GlobTool (Glob)
│       │   ├── grep_tool.py        # GrepTool (Grep)
│       │   └── bash.py             # BashTool (Bash)
│       │
│       ├── permissions/            # 权限系统
│       │   ├── __init__.py
│       │   ├── engine.py           # 权限规则引擎 (7 步匹配链) + PermissionOutcome 枚举
│       │   ├── rules.py            # 规则 pydantic 模型 + default_rules()
│       │   ├── store.py            # SessionPermissionStore (会话级权限记忆)
│       │   ├── inheritance.py      # 子Agent 权限继承 (min / 并集 / 交集)
│       │   └── bash_guard.py       # Bash AST 安全检查 (tree-sitter)
│       │
│       ├── hooks/                  # ✅ Hooks 系统 (Item 9)
│       │   ├── __init__.py         # 公开导出: 4 Protocol + 数据模型 + pipeline
│       │   ├── types.py            # Protocol (Pre/Post/Stop/SubagentStart) + HookBlock/HookModify/HookContinue/StopResult
│       │   ├── registry.py         # HookRegistry (显式 hook_kind 分类 + 优先级桶 + copy_with_overrides)
│       │   └── pipeline.py         # run_pre_tool_use_hooks / run_post_tool_use_hooks / run_stop_hooks / run_subagent_start_hooks
│       │
│       ├── session/                # 🔶 会话运行时层 (Phase A 已完成, Phase B 待实现)
│       │   ├── __init__.py
│       │   └── state.py            # SessionState (会话历史 + 权限记忆延续)
│       │
│       ├── mcp/                    # MCP 协议集成
│       │   ├── __init__.py         # 公开导出: McpSessionManager, register_mcp_tools
│       │   ├── _transport.py       # stdio/SSE/WebSocket transport 适配器
│       │   ├── auth.py             # OAuth 2.0/2.1 token 管理
│       │   ├── client.py           # McpSessionManager + McpConnectionState
│       │   ├── config.py           # 配置加载链: .mcp.json → env 展开 → McpServerConfig
│       │   ├── registry.py         # MCP 工具 → Tool 子类动态桥接
│       │   └── resources.py        # MCP Resource/Prompt 获取（预留 Phase 3）
│       │
│       ├── llm/                    # LLM 多 Provider 集成
│       │   ├── __init__.py         # 公开导出 + 触发 @register_client 注册
│       │   ├── base.py             # BaseClient 抽象 + get_client() 工厂
│       │   ├── events.py           # InternalEvent 类型定义
│       │   ├── errors.py           # 错误分类 + 统一重试策略
│       │   ├── anthropic.py        # AnthropicClient (anthropic SDK)
│       │   ├── openai.py           # OpenAIClient (openai SDK)
│       │   └── deepseek.py         # DeepSeekClient (openai SDK)
│       │
│       ├── compression/            # 自动压缩
│       │   ├── __init__.py
│       │   ├── trigger.py          # 触发条件判断 + _partition_rounds() 轮次分区
│       │   ├── summarizer.py       # LLM 摘要生成 + 重试循环
│       │   ├── consistency.py      # 4 项一致性验证
│       │   └── prompts.py          # 可自定义的 prompt 模板常量
│       │
│       ├── skills/                 # ✅ Skills 系统 (Item 9)
│       │   ├── __init__.py         # 公开导出
│       │   ├── definition.py       # SkillDefinition + SkillParameter 模型
│       │   ├── registry.py         # SkillRegistry (首遇优先覆盖, source tracking)
│       │   ├── loader.py           # load_skill_from_markdown + discover_skills (<dir>/<name>/SKILL.md)
│       │   ├── executor.py         # render_template + execute_skill_inline / execute_skill_fork
│       │   ├── tool.py             # SkillTool (name="Skill", @register_tool)
│       │   └── permissions.py      # SkillPermissionEngine (fnmatch allow/deny/ask)
│       │
│       ├── plugins/                # ✅ Plugin 系统 (Item 9)
│       │   ├── __init__.py         # 公开导出
│       │   ├── base.py             # Plugin 抽象基类 (tools/hooks/agents/skills)
│       │   ├── registry.py         # PluginRegistry (entry_points 发现 + 手动注册)
│       │   └── lifecycle.py        # safe_enable_plugins / safe_disable_all
│       │
│       ├── persistence/            # ⬜ 会话持久化 (待实现 — Item 10)
│       │   ├── __init__.py
│       │   ├── storage.py          # JSONL 读写 (aiofiles)
│       │   ├── path.py             # 存储路径计算
│       │   ├── session.py          # 会话列表 / 恢复 / 删除
│       │   └── migrate.py          # 格式迁移
│       │
│       └── logging/                # 结构化日志
│           ├── __init__.py
│           └── tracer.py           # TurnTracer 结构化日志记录器
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py                 # pytest fixtures (default_config, custom_config)
│   ├── unit/                       # 单元测试 (~755 tests, 48 files)
│   │   ├── conftest.py
│   │   ├── test_models.py          # AgentConfig / ModelId / CompressionConfig 模型校验
│   │   ├── test_messages.py        # AgentResult / AgentMessage  discriminated union
│   │   ├── test_agent.py           # Agent 类 (init/run/stream/permission callback)
│   │   ├── test_tools_registry.py  # @register_tool / get_tool() / 重注册
│   │   ├── test_tools_context.py   # ToolUseContext / ToolCallResult 数据模型
│   │   ├── test_tools_read.py      # FileReadTool (读取/缓存/编码/binary/字节截断)
│   │   ├── test_tools_edit.py      # FileEditTool (替换/删除/stale检测/权限)
│   │   ├── test_tools_write.py     # FileWriteTool (写入/覆盖/目录创建/权限保留)
│   │   ├── test_tools_glob.py      # GlobTool (匹配/排序/截断/隐藏文件)
│   │   ├── test_tools_grep.py      # GrepTool (rg --json 输出模式/分页/过滤)
│   │   ├── test_tools_bash.py      # BashTool (echo/失败/超时/abort/退出码)
│   │   ├── test_tools_workspace.py # 工作区边界检查 (Read/Write/Edit/Glob/Bash)
│   │   ├── test_tools_attributes.py # Tool 类属性 (is_concurrency_safe 等)
│   │   ├── test_tools_edge_cases.py # 混合并发/大结果持久化/设备文件/写入错误清理
│   │   ├── test_anthropic_provider.py # AnthropicClient (classify/normalize/convert)
│   │   ├── test_openai_provider.py  # OpenAIClient (classify/normalize/convert)
│   │   ├── test_deepseek_provider.py # DeepSeekClient (classify/normalize/convert)
│   │   ├── test_provider_registry.py # register_client / get_client / retry_config
│   │   ├── test_permissions.py      # PermissionEngine + 继承 + Bash AST 安全
│   │   ├── test_permission_store.py # SessionPermissionStore (会话权限记忆)
│   │   ├── test_loop_helpers.py     # 消息构建器 + run_tool_use / run_tools
│   │   ├── test_budget.py           # TokenBudget / token 估算 / context window
│   │   ├── test_truncate.py         # hard_truncate 轮次分区截断
│   │   ├── test_context.py          # enrich_context / git status 采集
│   │   ├── test_compression.py      # 压缩触发器/提示词/一致性验证 (unit)
│   │   ├── test_logging.py          # TurnTracer 结构化日志
│   │   ├── test_agent_definition.py # AgentDefinition 模型/校验/序列化
│   │   ├── test_agent_registry.py   # AgentRegistry 注册/合并/优先级链
│   │   ├── test_agent_loader.py     # Markdown/JSON 加载器 + 目录发现
│   │   ├── test_builtin_agents.py   # 内置 Agent 属性 + 引擎免批准验证
│   │   ├── test_agent_resolve.py   # §7.6 消费契约（工具池/权限/MCP/SysPrompt/模型/深度）
│   │   ├── test_agent_tool.py      # AgentTool 输入校验/路由/深度闸/别名
│   │   ├── test_agent_lifecycle.py # 生命周期状态/通知 XML/进度摘要
│   │   ├── test_agent_fork.py      # filter_incomplete_tool_calls / Fork 不可变
│   │   ├── test_session_state.py   # SessionState 快照/记录/清空
│   │   ├── test_hooks.py            # ✅ Hooks 系统 (36 tests — Item 9)
│   │   ├── test_plugins.py          # ✅ Plugin 系统 (18 tests — Item 9)
│   │   ├── test_skills.py           # ✅ Skills 系统 (24 tests — Item 9)
│   ├── integration/                # mock model 集成测试 (无真实 API)
│   │   ├── conftest.py
│   │   ├── mock_model.py           # MockClient / make_text_turn / make_tool_use_turn
│   │   ├── test_query_loop.py      # query() 单轮/多轮/max_turns/error
│   │   ├── test_tool_pipeline.py   # (桩 — not yet implemented)
│   │   ├── test_compression.py     # compress() + query() loop 压缩路径
│   │   ├── test_subagent.py        # (桩 — not yet implemented)
│   │   ├── test_fork.py            # (桩 — not yet implemented)
│   │   ├── test_permission_inheritance.py  # (桩 — not yet implemented)
│   │   ├── test_run_agent.py       # (桩 — not yet implemented)
│   │   └── test_history_continuity.py  # SessionState 跨 prompt 历史延续
│   └── e2e/                        # vcrpy 录制回放 (需 API key 录制一次)
│       ├── conftest.py
│       ├── test_turn_flow.py       # (桩 — not yet implemented)
│       └── test_compression.py     # (桩 — not yet implemented)
│
├── docs/                           # MkDocs Material
│   ├── mkdocs.yml
│   ├── index.md
│   ├── quickstart.md
│   ├── api/
│   │   ├── agent.md
│   │   ├── tools.md
│   │   ├── permissions.md
│   │   └── hooks.md
│   └── guides/
│       ├── subagent.md
│       ├── mcp.md
│       └── skills.md
│
├── .github/
│   └── workflows/
│       ├── ci.yml                  # pytest + 类型检查
│       └── publish.yml             # PyPI 发布 (tag 触发)
│
└── examples/
    ├── basic_run.py                # 最小 Agent.run() 示例
    ├── streaming.py                # Agent.stream() 示例
    ├── permission_callback.py      # 权限回调注入示例
    └── session_resume.py           # 会话恢复示例
```
