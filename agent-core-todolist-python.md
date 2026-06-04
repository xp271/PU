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
├── agent.py             # Agent 主类
├── models.py            # 所有 Pydantic 数据模型
└── tools/               # 工具系统（详见第 2 节）
```

**依赖**（`pyproject.toml`）: Python >= 3.12, pydantic >= 2, anthropic >= 0.105.2, aiofiles >= 24, mcp >= 1.0, tenacity >= 9, tree-sitter >= 0.24, tree-sitter-bash >= 0.23, charset-normalizer >= 3, python-frontmatter >= 1。

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

    # 模型常量
    ClaudeModel,

    # 工具系统
    Tool,
    ToolCallResult,
    ToolUseContext,
    BashTool,
    FileEditTool,
    FileReadTool,
    FileWriteTool,
    GlobTool,
    GrepTool,
    get_tool,
    register_tool,
    registered_tools,
)
```

#### 1.3 `Agent` 类 (`agent.py`)

库唯一的入口类。调用方只通过 `Agent` 交互，内部实现细节完全隐藏。

```python
from agent_core import Agent, AgentConfig

config = AgentConfig(model="claude-sonnet-4-5", max_turns=20)
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
| `run` | `async run(prompt: str) -> AgentResult` | 消费 `stream()` 的异步生成器，聚合所有 `AgentMessage` 为最终的 `AgentResult`。当前 `stream()` 尚未实现，会抛出 `NotImplementedError`。 |
| `stream` | `async stream(prompt: str) -> AsyncGenerator[AgentMessage, None]` | **尚未实现**。调用 LLM API、执行工具、逐轮产出 `AgentMessage`。 |
| `on_permission_check` | `on_permission_check(callback: PermissionCallback) -> None` | 注册异步权限回调。回调签名为 `async (tool_name: str, tool_input: dict) -> bool`，返回 `True` 表示允许，`False` 表示拒绝。 |

**`PermissionCallback` 类型**

```python
PermissionCallback = Callable[[str, dict[str, Any]], Awaitable[bool]]
```

#### 1.4 配置模型 `AgentConfig` (`models.py`)

所有字段均为可选，有合理默认值。

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `model` | `str` | `"claude-sonnet-4-5"` | LLM 模型标识符。非已知模型名会触发 `ValueError`，除非设置 `allow_unknown_model=True`（此时仅发 warning）。 |
| `max_turns` | `int` | `20` | 最大 turn 迭代次数。校验范围 1–500。 |
| `permission_mode` | `PermissionMode` | `"ask"` | 默认权限模式：`"bypassPermissions"` / `"acceptEdits"` / `"ask"`。 |
| `tools` | `list[str]` | `["*"]` | 可用工具名称列表。`["*"]` 表示全部已注册工具。不能为空列表。 |
| `mcp_servers` | `dict[str, Any]` | `{}` | MCP 服务端配置，键为服务名。 |
| `skills_dir` | `str \| None` | `None` | Skill 定义文件目录路径。 |
| `hooks` | `list[Any]` | `[]` | Agent 启动时注册的 Hook 实例。 |
| `allow_unknown_model` | `bool` | `False` | 为 `True` 时允许使用 `ClaudeModel` 枚举之外的模型标识（如代理/自托管场景）。 |

**`ClaudeModel` 枚举**

```python
class ClaudeModel(str, Enum):
    SONNET_4_5 = "claude-sonnet-4-5"
    OPUS_4_7   = "claude-opus-4-7"
    HAIKU_4_5  = "claude-haiku-4-5"
```

提供 `known_values()` 类方法返回所有合法模型名的集合。

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

### 3. LLM API 集成层 & 结构化日志 (2 天)

**功能**: 封装 Anthropic Messages API，消息规范化，流式响应，全错误类型恢复，以及 turn-level 结构化日志。

**原因**: Agent 主循环的唯一外部依赖就是 LLM API。流式断连、529 overloaded、context_length 超限在长任务中很常见，必须在集成层就预留恢复逻辑。没有结构化日志，Agent 循环出问题时几乎无法排查。

**注意**: API 模型名以 Anthropic 官方文档为准。当前可用模型 ID 格式为 `claude-sonnet-4-5`、`claude-opus-4-7`、`claude-haiku-4-5` 等，不存在 `4-6` 的 Sonnet。代码中应以常量枚举当前合法值，启动时做校验。

- [ ] `AsyncAnthropic` 封装，支持 api_key / base_url 配置注入
- [ ] 模型名常量枚举（`ClaudeModel.SONNET_4_5 = "claude-sonnet-4-5"` 等），`AgentConfig` 启动时校验是否为已知合法值
- [ ] 消息规范化 `normalize_messages()`：处理 system / user / assistant 角色交替，处理 tool_use / tool_result 配对校验
- [ ] 流式响应异步迭代器：content_block_start / delta / stop，message_start / delta / stop 完整事件解析
- [ ] 错误恢复策略：
  - `max_output_tokens` → 自动截断追加 continue 请求（最多 3 次）
  - `rate_limit` → 指数退避重试（tenacity，Retry-After header 优先）
  - `overloaded` (HTTP 529) → 指数退避重试，区别于 rate_limit
  - `context_length_exceeded` → 抛出不可恢复异常，通知调用方
  - 流式中途断连 → 重连 + 从断点续接（发送 stop_reason 为 null 的 continue）
- [ ] **结构化日志**：每个 turn 记录 `turn_index / model / input_tokens / output_tokens / tool_uses / latency_ms / error_type`，使用 Python `logging` + 统一 trace_id 贯穿整个 Agent 生命周期

---

## Phase 2: Agent 核心循环 (Week 3-4)

### 4. Agent 主循环 (query 系统) (4 天)

**功能**: `query()` 异步生成器，完整 turn 循环，token 预算管理，上下文硬截断，Stop hooks 集成。**不含自动压缩**（自动压缩拆为独立条目 5.5）。

**原因**: 这是整个 Agent 的心脏。自动压缩是个独立且复杂的子系统（摘要质量、保留哪些 tool_result、压缩后能否恢复），绑在主循环里会引爆首次集成风险。先做硬截断 + 预算检查跑通主循环，压缩单独迭代。

- [ ] `query()` 异步生成器签名与骨架
- [ ] Turn 循环核心：
  - 上下文富化（git status 子进程, 当前日期, AGENTS.md 文件读取, 工作区文件列表）
  - 发送 LLM 请求 → 流式接收 assistant message
  - 检测 tool_use 块 → 分发到 `run_tools()` 工具管线
  - tool_result 回填为 user message（tool_result content block）
  - 检查 token 预算 → 继续下一轮 或 停止
- [ ] Token 预算管理：
  - 总 token 预算（由 AgentConfig.max_turns 或 token 计数上限定义）
  - 90% 阈值检测 → 发出警告继续
  - "收益递减"检测：连续 3 轮 delta < 500 tokens → 自动停止
  - 预算耗尽 → 注入 `max_turns_reached` 系统消息并 yield 最终结果
- [ ] 上下文硬截断：总 token 超过模型 context window 的 80% → 保留最近 N 轮，丢弃更早轮次（以 user message 为切割边界）
- [ ] Stop hooks 调用：每轮 assistant message 产出后，执行 Stop 类型 hook → hook 可返回 "should_stop=True" 提前终止循环
- [ ] `Agent.run()` 实现：消费 `query()` generator，聚合为 `AgentResult`
- [ ] `Agent.stream()` 实现：透传 `query()` generator 的 `AgentMessage` 流

---

### 5. 工具执行管线 & 权限系统 (4 天)

**功能**: 并发/串行工具编排，单工具执行生命周期，权限规则引擎（含子 Agent 权限继承语义），Bash 专用的 AST 级安全检查。

**原因**: 这是 Agent 的安全边界。权限引擎的设计必须在此时就覆盖**子 Agent 权限继承**场景——AgentDefinition 的 permission_mode 和 disallowed_tools 如何从父级派生、子 Agent 能否提升自己的权限，这些语义如果等到第 8 项再做会回头返工权限模型。

- [ ] `run_tools(tool_use_blocks, context)` → AsyncGenerator：
  - 按 `Tool.is_concurrency_safe` 类属性分组（无需实例化）
  - 并发安全组 → `asyncio.gather()` + `asyncio.Semaphore(max_concurrency)` 控制并发上限
  - 非并发工具 → 串行 `for ... await` 执行
  - 同一轮工具调用的结果按 tool_use 原始顺序输出
- [ ] `run_tool_use(tool_use, context)` → 单工具生命周期：
  - resolve_tool（注册表按名查找）
  - validate_input（Pydantic 校验，失败 → 返回 error tool_result，跳过权限检查）
  - check_permissions（权限引擎裁决）
  - 执行 PreToolUse hooks（任一 hook 返回 block → 终止）
  - `tool.call()`
  - 执行 PostToolUse hooks（可修改输出 / 添加上下文消息 / 阻断后续）
  - 结果持久化（超大结果分片存储到临时文件，tool_result 只保留前 N 字符 + 文件路径引用）
- [ ] 权限规则引擎：
  - 匹配链（按优先级）：精确匹配 → 前缀匹配 → `fnmatch` 通配符 → 路径约束 → mode-specific → read-only → fallback
  - 规则持久化格式（settings.json permissions 段，pydantic model）
  - 权限回调注入：`agent.on_permission_check(async callable)` —— 调用方自定义决策
- [ ] 子 Agent 权限继承（与第 7/8 项联动，此时先定义契约）：
  - 子 Agent 的 permission_mode 取 `min(父agent实际模式, AgentDefinition.permission_mode)`
  - disallowed_tools 取父级并集（父级禁止的，子级必然禁止）
  - allowed_tools 取交集（子级只能使用父级也允许的工具）
  - 子 Agent 永远不能提升自己权限（bypassPermissions → ask 方向可降不可升）
- [ ] Bash 工具专用安全检查：
  - tree-sitter + tree-sitter-bash 解析 shell 命令 AST
  - 复合命令逐子命令检查（`&&` / `|` / `;` / `$()` / 反引号）
  - 危险模式检测（`curl | bash`、`rm -rf /`、`chmod 777`、`/dev/null` 重定向覆盖关键文件）
  - 异步轻量 LLM 分类器作为第二道防线（可选，调用方注入）

---

### 5.5 自动压缩 (上下文超限 LLM 摘要) (2 天)

**功能**: 上下文接近窗口上限时触发 LLM 摘要压缩，保留关键信息的同时减小上下文体积。

**原因**: 从主循环中拆分出来独立开发。压缩的摘要质量、保留策略、压缩后的 tool_result 一致性是独立的设计问题，单独迭代可以稳步打磨而不影响主循环稳定性。

- [ ] 触发条件：总 token 超过模型 context_window 的 85% 或 AgentConfig 中配置的阈值
- [ ] 摘要策略：
  - 保留最近 N 轮原始消息（N 可配置，默认 3）
  - 更早的对话压缩为一段 `<conversation_summary>` user message
  - tool_result 压缩：保留 tool_use 名称 + 输入参数 + 结果摘要（前 200 字符），截断大结果
- [ ] 压缩调用：用独立的一次 `query()` 调用让 LLM 生成摘要（不计入 max_turns）
- [ ] 压缩后一致性验证：压缩前后 system prompt 不变，tool_use 列表可追溯
- [ ] 压缩事件通知：yield `AgentMessage(type="compact")` 让调用方可感知

---

### 6. MCP 协议集成 (2 天)

**功能**: MCP 客户端，动态获取服务端工具并注册，配置来源管理，工具冲突去重，OAuth 认证。

**原因**: MCP 是 Agent 扩展能力的标准协议。一个协议接入所有兼容服务，避免为每个外部工具写胶水代码。

- [ ] MCP transport 抽象：stdio / SSE / WebSocket（使用 `mcp` Python SDK 的 transport 层）
- [ ] `connect_to_server(name, McpServerConfig)` → 建立连接 → 返回 client handle
- [ ] `fetch_tools_for_client(client)` → 获取服务端工具列表 → 为每个工具动态创建 Tool 子类 → 注册到全局 Tool 注册表
- [ ] 工具名格式：`mcp__{server_name}__{tool_name}`，避免与内置工具冲突
- [ ] 服务端工具与内置工具冲突检测：同名时内置优先，打印 warning
- [ ] MCP 配置加载链：项目 `.mcp.json` → 全局 `~/.agent/mcp.json` → `AgentConfig.mcp_servers` 代码注入
- [ ] MCP 资源 / Prompt 获取（映射到 Skill 系统，在 Phase 3 接入）
- [ ] OAuth 认证流程：token 刷新 / step-up 认证 / 过期处理

---

## Phase 3: 子Agent & 多Agent (Week 5-6)

### 7. Agent 定义系统 (2 天)

**功能**: `AgentDefinition` 数据结构、加载器、优先级链、内置 Agent 注册。

**原因**: 不同场景需要不同配置的 Agent——探索代码用只读工具，执行任务用读写工具。AgentDefinition 是子 Agent 的"配置模板"，一次定义反复使用。

- [ ] `AgentDefinition` pydantic 模型：
  ```python
  class AgentDefinition:
      agent_type: str
      when_to_use: str  # LLM 看到的描述
      tools: list[str]  # ["*"] 或 ["Read", "Glob", ...]
      disallowed_tools: list[str]
      system_prompt: str  # 或 callable
      model: str | None  # None = 继承父级
      permission_mode: "bypassPermissions" | "acceptEdits" | "ask" | None
      mcp_servers: dict[str, McpServerConfig]
      hooks: list[Hook]
      skills: list[str]
      memory: "user" | "project" | "local" | None
      background: bool  # 默认是否异步执行
  ```
- [ ] Agent 定义加载器：
  - markdown frontmatter 解析（`python-frontmatter`）→ AgentDefinition
  - JSON 文件加载
  - Python 代码直接注册（`agent_registry.register(my_definition)`）
- [ ] 优先级链实现：user_settings > project_settings > local_settings > plugin > built-in（后加载的覆盖前者的同名字段，disallowed_tools 取并集）
- [ ] 内置 Agent 注册：
  - **GeneralPurpose**：tools=["*"]，permission_mode=ask，默认 agent
  - **Explore**：tools=["Read", "Glob", "Grep"]，只读，用于代码库探索
  - **Plan**：tools=["Read", "Glob", "Grep"]，只读 + 产出文本计划
- [ ] Agent 定义发现机制：扫描 `~/.agent/agents/` 目录 → `.md` / `.json` 文件

---

### 8. 子Agent 执行引擎 & AgentTool (5 天)

**功能**: `run_agent()` 异步生成器（隔离执行环境 + 独立 MCP + 独立 System Prompt + sidechain 转录），`AgentTool` 让 LLM 通过 tool_use 创建子 Agent（同步/异步/Fork/恢复）。

**原因**: 复杂任务需要拆解并行执行。这是全库最复杂的模块——涉及隔离、并发、Fork prompt cache 复用、异步通知、中断恢复。需要 4-5 天充分打磨。

- [ ] `run_agent()` 异步生成器：
  - agent_id 生成（uuid4）
  - 模型解析：AgentDefinition.model > AgentConfig.model > 父 Agent 当前模型
  - 隔离 ToolUseContext 创建（`copy.deepcopy` read_file_state，新 abort_signal）
  - Agent 专属 MCP 服务器初始化（与父 Agent 合并去重，同名 server 子覆盖父）
  - Agent 专属 System Prompt 构建（AgentDefinition.system_prompt + tool 列表注入）
  - 权限模式计算（子 Agent 权限 = min(父级实际权限, AgentDefinition 声明的权限)，不可提升）
  - SubagentStart hooks 执行
  - frontmatter skills 预加载（追加为 system context）
  - `resolve_agent_tools()` 工具池过滤：
    - allow = AgentDefinition.tools ∩ 父 Agent allowed_tools
    - disallow = AgentDefinition.disallowed_tools ∪ 父 Agent disallowed_tools
    - `["*"]` → 展开为父 Agent 全部 allowed tools
  - 调用 `query()` 执行主循环 → sidechain JSONL 逐行记录
  - `finally` 清理：MCP 连接断开 / hooks 注销 / 临时文件清理
- [ ] 消息 fork 预处理（`filter_incomplete_tool_calls()`）：fork 场景下过滤父消息中 tool_use 无对应 tool_result 的块
- [ ] `AgentTool` — 让 LLM 通过 tool_use 块创建子 Agent：
  - 输入 schema：`{description, prompt, subagent_type?, run_in_background?, isolation?}`
  - 路由逻辑：subagent_type → AgentDefinition 查找 → fork 路径 → 标准子 Agent
  - 同步模式：内联迭代 `run_agent()` generator，实时 yield 子 Agent 的 messages
  - 异步模式：`asyncio.create_task(run_async_lifecycle())`，立即返回 `{status: "async_launched", agent_id}`
  - Auto-background：同步 Agent 超过 120s 自动转后台
- [ ] 异步 Agent 生命周期管理：
  - 进度追踪：token count / tool use count / 最近 activity 描述
  - 后台摘要：每 30s 用一次独立 query 生成 3-5 词进度摘要
  - `finalize_agent_tool()` → 提取最终结果 + 用量统计
  - `enqueue_agent_notification()` → 生成 XML `<task-notification>` → 父 Agent 下一轮以 user message 形式收到
  - Agent 完成/失败/被杀 → 对应状态转换 + 通知
- [ ] Agent 恢复：`resume_agent(agent_id)` → 从 JSONL 加载 transcript → 重建 ToolUseContext → `run_agent()` 继续
- [ ] Fork Agent：子 Agent 继承父 Agent 完整对话上下文，共享 Anthropic prompt cache（父 Agent system prompt + messages prefix 与子 Agent 字节一致）

---

### 9. Hooks + Plugin + Skills 系统 (2 天)

**功能**: Hook 管线（可阻断/可修改输出），Plugin 热插拔注册表，Skill 定义与执行（inline / fork 两种模式）。

**原因**: 这三个是 Agent 的扩展机制。Hook 让外部代码介入工具执行；Plugin 让能力可热插拔；Skills 让用户无需写代码就能定义可复用工作流。

- [ ] Hook 类型定义（Protocol）：
  ```python
  class PreToolUseHook(Protocol):
      async def __call__(self, tool_name: str, tool_input: dict, context: ToolUseContext) -> HookResult
  class PostToolUseHook(Protocol):
      async def __call__(self, tool_name: str, tool_result: ToolResult, context: ToolUseContext) -> HookResult
  class StopHook(Protocol):
      async def __call__(self, messages: list, context: ToolUseContext) -> StopResult
  ```
  HookResult = `{"continue": True}` | `{"block": True, "reason": str}` | `{"modify_output": new_result}`
- [ ] Hook 注册表：`dict[HookType, list[Hook]]`，全局 + 项目级隔离
- [ ] PreToolUse 管线：任一 hook 返回 block → 工具终止执行，返回 error tool_result
- [ ] PostToolUse 管线：按顺序执行，可修改 tool_result / 注入上下文消息 / 标记 should_stop
- [ ] Stop hook 周期性评估：每轮 assistant message 后调用，可提前终止循环（Goal 检测的基础）
- [ ] Plugin 注册表：`importlib.metadata.entry_points(group="agent_core.plugins")` 自动发现 + 手动注册 API
- [ ] Plugin 生命周期：`plugin.enable()` / `plugin.disable()`，插件可提供 tools / hooks / agent_definitions / skills
- [ ] Skill 定义格式：`SKILL.md` frontmatter（name, description, tools, model_override, context）
- [ ] Skill 加载器：递归扫描 `AgentConfig.skills_dir` → 解析 frontmatter → 注册
- [ ] Skill 执行：
  - inline 模式：展开 SKILL.md body 为 user message → 注入 tool 白名单覆盖 → 继续当前 Agent 循环
  - fork 模式：在隔离子 Agent 中执行（用于长时间 skill）→ 结果回传
- [ ] Skill 权限：复用权限引擎，按 skill name 匹配 deny/ask/allow 规则（含前缀通配 `review:*`）

---

## Phase 4: 持久化 & 收尾 (Week 7)

### 10. 会话持久化 & 恢复 (2 天)

**功能**: JSONL 转录存储（主会话 + 子 Agent sidechain），会话列表与恢复，格式迁移。

**原因**: 对话需要跨进程存活。JSONL 是 append-only 格式，安全简单，崩溃时最多丢失最后一行。

- [ ] 存储路径：`~/.agent/projects/{project_hash}/{session_id}.jsonl`
- [ ] Sidechain 路径：`~/.agent/projects/{project_hash}/{session_id}/{agent_id}.jsonl`
- [ ] 写入策略：每轮 assistant message + tool_result 完成后 `aiofiles` append 一行
- [ ] 消息序列化格式：每行 `{"role": ..., "content": [...], "timestamp": ..., "turn": N}`
- [ ] 会话列表：扫描存储目录 → 按 mtime 排序 → 提取首条 user message 作为标题
- [ ] 会话恢复：加载 JSONL → 重建 `ToolUseContext`（read_file_state 失效标记）→ `query()` 继续
- [ ] 格式迁移：`migrate_transcript()` 函数，version 字段驱动，旧格式自动升级
- [ ] 会话删除 / 清理：删除对应 JSONL 文件，清理 sidechain 子目录

---

### 11. 测试、文档 & 发布 (2 天)

**功能**: 多层测试（单元 + mock 集成 + vcrpy 录制回放），MkDocs API 文档，PyPI 发布配置。

**原因**: 可被第三方依赖的库需要可信的测试覆盖。vcrpy 覆盖 LLM 调用确定性场景，mock model 覆盖并发/竞态/隔离逻辑，两者互补。

- [ ] **单元测试**（pytest）：每个 Tool 的 validate_input / call / 边界条件
- [ ] **mock model 集成测试**（不依赖真实 API）：
  - 实现一个 `MockModel` 产出预定义的 tool_use 序列
  - 测试并发工具管线（多 tool_use 并行执行，验证 Semaphore 并发控制）
  - 测试子 Agent sidechain 隔离（两个子 Agent 并行写 JSONL 不互相干扰）
  - 测试 Fork Agent 消息预处理（filter_incomplete_tool_calls）
  - 测试权限继承链（父禁止 Bash → 子 Agent 的 tools=["*"] → Bash 被过滤）
  - 测试异步 Agent 完成通知（XML task-notification 格式正确性）
- [ ] **vcrpy 录制回放测试**（需要一次真实 API key 录制）：
  - 工具搜索 → 代码读取 → 代码修改的端到端 turn flow
  - max_output_tokens 恢复场景
  - 自动压缩触发场景
- [ ] MkDocs Material API 文档搭建
- [ ] `pyproject.toml` + GitHub Actions PyPI 发布 workflow
- [ ] README 包含快速开始示例（Agent.run / Agent.stream / 权限回调 / 会话恢复 四个最小示例）

---

## 统计

| 阶段 | 条目 | 预估天数 |
|------|------|----------|
| Phase 1: 基础设施 | #1 脚手架 (2) + #2 Tool 框架 (2) + #3 API 集成 (2) | 6 |
| Phase 2: Agent 核心循环 | #4 主循环 (4) + #5 管线+权限 (4) + #5.5 压缩 (2) + #6 MCP (2) | 12 |
| Phase 3: 子Agent & 扩展 | #7 Agent 定义 (2) + #8 执行引擎 (5) + #9 Hooks/Plugin/Skills (2) | 9 |
| Phase 4: 持久化 & 收尾 | #10 持久化 (2) + #11 测试/文档/发布 (2) | 4 |
| **总计** | **12 条目** | **31 天 (~6.5 周)** |

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
│       ├── __init__.py             # 公开 API: Agent, AgentConfig, AgentResult, AgentMessage
│       │
│       ├── agent.py                # Agent 主类 (run / stream / on_permission_check)
│       ├── config.py               # AgentConfig pydantic 模型 + 校验
│       ├── result.py               # AgentResult / AgentMessage / stop_reason 枚举
│       │
│       ├── query/                  # Agent 主循环
│       │   ├── __init__.py
│       │   ├── loop.py             # query() 异步生成器 (turn 循环核心)
│       │   ├── budget.py           # Token 预算管理 + 收益递减检测
│       │   ├── context.py          # 上下文富化 (git status, 日期, AGENTS.md)
│       │   └── truncate.py         # 硬截断逻辑
│       │
│       ├── tools/                  # 工具系统
│       │   ├── __init__.py         # Tool 注册表 + @register_tool 装饰器
│       │   ├── base.py             # Tool 抽象基类 / ToolUseContext / ToolResult
│       │   ├── schema.py           # Pydantic → JSON Schema 自动生成
│       │   ├── builtin/
│       │   │   ├── __init__.py     # 注册所有内置工具
│       │   │   ├── read.py         # FileReadTool
│       │   │   ├── glob.py         # GlobTool
│       │   │   ├── grep.py         # GrepTool
│       │   │   ├── edit.py         # FileEditTool
│       │   │   ├── write.py        # FileWriteTool
│       │   │   └── bash.py         # BashTool
│       │   └── agent_tool/         # AgentTool (子Agent 创建)
│       │       ├── __init__.py
│       │       ├── tool.py         # AgentTool 实现 (call / 路由 / 同步异步分发)
│       │       ├── runner.py       # run_agent() 异步生成器
│       │       ├── lifecycle.py    # run_async_lifecycle / finalize_agent_tool
│       │       ├── fork.py         # Fork Agent (共享 prompt cache)
│       │       └── resume.py       # resume_agent (从 JSONL 恢复)
│       │
│       ├── permissions/            # 权限系统
│       │   ├── __init__.py
│       │   ├── engine.py           # 权限规则引擎 (匹配链)
│       │   ├── rules.py            # 规则 pydantic model + 持久化
│       │   ├── inheritance.py      # 子Agent 权限继承 (min / 并集 / 交集)
│       │   └── bash_guard.py       # Bash AST 安全检查 (tree-sitter)
│       │
│       ├── hooks/                  # Hooks 系统
│       │   ├── __init__.py
│       │   ├── types.py            # Protocol 定义 (Pre/Post/Stop/SubagentStart)
│       │   ├── registry.py         # Hook 注册表 (全局 + 项目级)
│       │   └── pipeline.py         # 管线执行器 (PreToolUse / PostToolUse)
│       │
│       ├── mcp/                    # MCP 协议集成
│       │   ├── __init__.py
│       │   ├── client.py           # connect_to_server / fetch_tools
│       │   ├── config.py           # McpServerConfig + 配置加载链
│       │   ├── registry.py         # MCP 工具 → Tool 注册表注入
│       │   └── auth.py             # OAuth 认证流程
│       │
│       ├── agents/                 # Agent 定义系统
│       │   ├── __init__.py
│       │   ├── definition.py       # AgentDefinition pydantic 模型
│       │   ├── registry.py         # 注册表 + 优先级链
│       │   ├── loader.py           # markdown / JSON / 代码 加载器
│       │   └── builtin/            # 内置 Agent 定义
│       │       ├── general.py      # GeneralPurpose
│       │       ├── explore.py      # Explore
│       │       └── plan.py         # Plan
│       │
│       ├── skills/                 # Skills 系统
│       │   ├── __init__.py
│       │   ├── loader.py           # SKILL.md 扫描 + frontmatter 解析
│       │   ├── executor.py         # inline / fork 执行
│       │   └── permissions.py      # Skill 权限匹配
│       │
│       ├── plugins/                # Plugin 系统
│       │   ├── __init__.py
│       │   ├── registry.py         # entry_points 发现 + 手动注册
│       │   └── lifecycle.py        # enable / disable
│       │
│       ├── llm/                    # LLM API 集成
│       │   ├── __init__.py
│       │   ├── client.py           # AsyncAnthropic 封装 + 流式
│       │   ├── models.py           # ClaudeModel 常量枚举
│       │   ├── normalize.py        # normalize_messages()
│       │   └── errors.py           # 错误恢复 (rate_limit / overloaded / 断连 / context_length)
│       │
│       ├── compression/            # 自动压缩
│       │   ├── __init__.py
│       │   ├── trigger.py          # 触发条件判断 (85% 阈值)
│       │   ├── summarizer.py       # LLM 摘要生成
│       │   └── consistency.py      # 压缩后一致性验证
│       │
│       ├── persistence/            # 会话持久化
│       │   ├── __init__.py
│       │   ├── storage.py          # JSONL 读写 (aiofiles)
│       │   ├── path.py             # 存储路径计算
│       │   ├── session.py          # 会话列表 / 恢复 / 删除
│       │   └── migrate.py          # 格式迁移
│       │
│       ├── logging/                # 结构化日志
│       │   ├── __init__.py
│       │   └── tracer.py           # trace_id 生成 + turn-level 日志记录
│       │
│       └── utils/                  # 内部工具
│           ├── __init__.py
│           ├── file_state.py       # read_file_state 缓存 (dict + mtime)
│           ├── encoding.py         # 文件编码检测
│           └── subprocess.py       # asyncio 子进程封装
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py                 # pytest fixtures (MockModel, tmp_workspace)
│   ├── unit/                       # 单元测试
│   │   ├── test_tools_read.py
│   │   ├── test_tools_edit.py
│   │   ├── test_tools_bash.py
│   │   ├── test_permissions.py
│   │   ├── test_hooks.py
│   │   └── test_normalize.py
│   ├── integration/                # mock model 集成测试 (无真实 API)
│   │   ├── mock_model.py           # MockModel 实现
│   │   ├── test_tool_pipeline.py   # 并发/串行工具管线
│   │   ├── test_subagent.py        # 子Agent 隔离 + sidechain
│   │   ├── test_fork.py            # Fork Agent 消息预处理
│   │   └── test_permission_inheritance.py  # 权限继承链
│   └── e2e/                        # vcrpy 录制回放 (需 API key 录制一次)
│       ├── cassettes/              # vcrpy 录制文件
│       ├── test_turn_flow.py       # 端到端 turn flow
│       └── test_compression.py     # 自动压缩触发
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
