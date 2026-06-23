# Item 7: Agent 定义系统 —— 实现计划

> 目标：`AgentDefinition` 数据结构、加载器、优先级链、内置 Agent 注册。
> 对应 TodoList 条目 #7，预估 2 天。

---

> **状态** (2026-06-23)：计划阶段。`permissions/inheritance.py` 已实现 3 个纯函数合约（`compute_child_permission_mode` / `compute_child_allowed_tools` / `compute_child_disallowed_tools`），等待 `AgentDefinition` 接入。`ToolRegistry` 已支持 query-scoped 实例隔离，为子 Agent 工具池过滤做好准备。`agents/` 目录已创建但为空。
>
> **修订 (2026-06-23, 审阅后)**：基于对 `PermissionEngine` 实际逻辑的核对，做了以下关键修正：
> 1. **只读免批准机制澄清** —— 由引擎判定链第 6 步「只读兜底」保证（与 `permission_mode` 无关），内置只读 Agent 的 `permission_mode` 一律改为 `None`，移除无效且有误导性的 `acceptEdits`。
> 2. **新增三层权限模型** —— 第 1 层默认只读零批准；第 2 层用户开启编辑工具（带 ask 弹窗）；第 3 层新增字段 **`auto_approve_tools`** 实现「开启编辑且免批准」，经编译为 `allow` 规则在引擎第 1–4 步命中，不改全局 mode、不违反非提权不变量（详见 §2.1 / §3.4 / §7.5）。
> 3. **修正若干内部矛盾** —— models.py 不再新增类（§1.4/§7.3）；内置 Agent 统一为「实例 + 直接调用 register_agent」（§3）；合并算法改用 `model_dump(exclude_unset=True)` 杜绝默认值误覆盖（§4.2）；加载器默认不扫裸 `.md`（§5.3）；补全测试与风险条目（§8/§10）。

---

## 1. 架构总览

### 1.1 AgentDefinition 在系统中的地位

```
Agent(config).run(prompt)
  │
  ▼
Agent.stream() ──► query()               ← 当前主 Agent 路径
                      │
                      ├─ 用户直接调用：使用 AgentConfig 的 tools/mode
                      │
                      └─ 子 Agent 调用（Item 8）：使用 AgentDefinition
                           │
                  ┌────────▼──────────┐
                  │  AgentDefinition  │  ← 本 Item 实现
                  │  .agent_type      │
                  │  .when_to_use     │
                  │  .tools           │
                  │  .system_prompt   │
                  │  .permission_mode │
                  │  .mcp_servers     │
                  │  ...              │
                  └────────┬──────────┘
                           │
              ┌────────────▼────────────┐
              │    agent_registry       │  ← 全局注册表
              │  built-in → 3 个        │
              │  project  → .md/.json   │
              │  user     → ~/.agent/   │
              │  plugin   → entry_point │
              └─────────────────────────┘
                           │
                           ▼
              run_agent(definition) ──► query()  ← Item 8 消费
```

**核心设计原则**：

- `AgentDefinition` 是**纯数据容器**（Pydantic BaseModel），不包含任何执行逻辑
- 注册表支持**多来源优先级链**合并，高优先级覆盖低优先级同名字段（`disallowed_tools` 取并集）
- 内置 Agent 在模块 import 时自动注册，用户/项目定义可覆盖同 `agent_type` 的内置定义
- 加载器职责单一：找到文件 → 解析 → 返回 `AgentDefinition`，不负责注册（由调用方决定）
- 与 `AgentConfig` 明确区分 —— `AgentConfig` 是**运行时配置**（API key、max_turns），`AgentDefinition` 是**能力模板**（tools、system_prompt、when_to_use）

### 1.2 AgentDefinition vs AgentConfig

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

### 1.3 新增文件与模块

```
src/agent_core/agents/
├── __init__.py              # 公开导出: AgentDefinition, AgentRegistry, get_agent, register_agent, list_agents
├── definition.py            # AgentDefinition Pydantic 模型
├── registry.py              # AgentRegistry 全局注册表 + 优先级链合并
├── loader.py                # markdown frontmatter / JSON / 代码 三种加载器
└── builtin/
    ├── __init__.py           # 触发内置 Agent 注册
    ├── general.py            # GeneralPurpose
    ├── explore.py            # Explore
    └── plan.py               # Plan
```

### 1.4 影响/修改的现有文件

| 文件 | 变更内容 |
|------|---------|
| `src/agent_core/models.py` | **不修改**。`AgentDefinition` / `AgentMemory` 定义在 `agents/definition.py`（见 §7.3 决策），仅 *import* 复用其中已有的 `PermissionMode` / `McpServerConfig` |
| `src/agent_core/__init__.py` | 导出 `AgentDefinition`, `AgentMemory`, `AgentRegistry`, `get_agent`, `register_agent`, `list_agents` |
| `src/agent_core/agents/__init__.py` | 新建 — 公开 API 聚合 |
| `src/agent_core/agents/definition.py` | 新建 — 数据模型 |
| `src/agent_core/agents/registry.py` | 新建 — 注册表 |
| `src/agent_core/agents/loader.py` | 新建 — 加载器 |
| `src/agent_core/agents/builtin/` | 新建 — 3 个内置 Agent |

**注意**：`query/loop.py`、`agent.py` 在 Item 7 阶段**不修改**——AgentDefinition 此时仅作为数据结构被定义和注册，Item 8 才接入 `run_agent()` 执行引擎。

---

## 2. 数据模型 (`agents/definition.py`)

### 2.1 AgentDefinition

```python
class AgentMemory(str, Enum):
    """子 Agent 记忆模式"""
    USER = "user"        # 跨会话持久化到用户记忆
    PROJECT = "project"  # 项目级记忆
    LOCAL = "local"      # 仅当前会话有效
    NONE = "none"        # 无记忆


class AgentDefinition(BaseModel):
    """Agent 能力模板 —— 纯数据，不包含执行逻辑。

    多个来源的同名 agent_type 定义通过优先级链合并：
    user > project > plugin > built-in。
    """

    agent_type: str
    """唯一标识符，如 ``"Explore"``、``"my-custom-agent"``。"""

    when_to_use: str | None = None
    """LLM 看到的使用场景描述。用于 AgentTool（Item 8）路由决策。
    例如 ``"When you need to search across multiple files..."``
    """

    tools: list[str] = Field(default_factory=lambda: ["*"])
    """工具白名单。``["*"]`` 表示父 Agent 所有可用工具。
    子 Agent 实际工具 = 此列表 ∩ 父 Agent 允许的工具。"""

    disallowed_tools: list[str] = Field(default_factory=list)
    """工具黑名单。子 Agent 实际禁止 = 此列表 ∪ 父 Agent 禁止。"""

    system_prompt: str = ""
    """注入子 Agent system prompt 的文本。
    在 Item 8 中与父 prompt + tool 列表拼接。"""

    model: str | None = None
    """模型标识符。``None`` 表示继承父 Agent 当前模型。"""

    permission_mode: PermissionMode | None = None
    """声明的权限模式。``None`` 表示继承父模式。
    运行时实际模式 = min（父模式, 此声明），**不可提升**（见 inheritance.py 不变量）。

    ⚠️ 注意 ``permission_mode`` **不是**让编辑类工具免批准的手段：
    本项目 PermissionEngine 中 ``acceptEdits`` 仅放行 *非 destructive* 工具，
    而 ``Edit`` / ``Write`` / ``Bash`` 均为 ``is_destructive=True``。
    要让编辑类工具免批准，请用下方 ``auto_approve_tools``。
    """

    auto_approve_tools: list[str] = Field(default_factory=list)
    """**预授权工具清单（第 3 层机制）** —— 用户可在 agent 定义中显式声明
    哪些工具调用免批准。Item 8 构建子 Agent 的 ``PermissionEngine`` 时，
    将此列表中的每个工具名编译为一条 ``PermissionRule(action="allow")``，
    在引擎判定链的第 1–4 步（规则匹配，优先级高于 mode）命中并放行。

    设计要点：
    - 这是**绕过 ask 弹窗**的唯一合法通道，且**不**修改全局 ``permission_mode``，
      因此**不违反**「子 Agent 不可自行提权超过父会话」的安全不变量 ——
      授权来源是用户亲手写的 agent 定义，由用户对其负责。
    - 仍受工具池约束：工具必须先通过 ``tools`` ∩ 父级允许集（见 §7.2），
      ``auto_approve_tools`` 只决定「调了弹不弹」，不决定「能不能调」。
    - 默认 ``[]`` —— 即默认情况下编辑类工具走 ask 回调（最安全语义）。
    """

    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    """Agent 专属 MCP 服务器。Item 8 中与父 Agent 合并（同名 server 子覆盖父）。"""

    hooks: list[Any] = Field(default_factory=list)
    """Agent 专属 Hook 实例（Item 9 接入后生效）。"""

    skills: list[str] = Field(default_factory=list)
    """预加载的 skill 名称列表。Item 9 接入后生效。"""

    memory: AgentMemory = AgentMemory.NONE
    """子 Agent 记忆持久化模式。Item 10 接入后生效。"""

    background: bool = False
    """是否默认以后台异步模式执行（Item 8）。"""

    @field_validator("agent_type")
    @classmethod
    def _validate_agent_type(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("agent_type must not be empty")
        if not re.match(r"^[a-zA-Z0-9_-]+$", v):
            raise ValueError(
                f"agent_type '{v}' contains invalid characters. "
                "Only [a-zA-Z0-9_-] allowed."
            )
        return v.strip()

    @field_validator("tools")
    @classmethod
    def _validate_tools(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("tools must not be empty")
        return v
```

### 2.2 字段来源对比（todo vs 实现）

| Todo 字段 | 实现 | 说明 |
|-----------|------|------|
| `agent_type: str` | ✅ 保留 | 唯一标识 |
| `when_to_use: str` | ✅ 保留 | LLM 路由描述 |
| `tools: list[str]` | ✅ 保留 | `["*"]` 默认 |
| `disallowed_tools: list[str]` | ✅ 保留 | 默认 `[]` |
| *(新增)* `auto_approve_tools: list[str]` | ➕ 新增 | 默认 `[]`，编译为 `allow` 规则（第 3 层免批准机制） |
| `system_prompt: str` | ✅ 保留 | 默认 `""` |
| `model: str \| None` | ✅ 保留 | `None` = 继承 |
| `permission_mode: ... \| None` | ✅ 保留 | `None` = 继承 |
| `mcp_servers: dict` | ✅ 保留 | 类型用已有 `McpServerConfig` |
| `hooks: list[Hook]` | ✅ 保留 | 类型 `list[Any]`（与 AgentConfig 一致） |
| `skills: list[str]` | ✅ 保留 | 默认 `[]` |
| `memory: "user" \| "project" \| "local" \| None` | ✅ 改为 `AgentMemory` 枚举 | 增加 `NONE` 值表意更明确 |
| `background: bool` | ✅ 保留 | 默认 `False` |

---

## 3. 内置 Agent (`agents/builtin/`)

三个内置 Agent 是模块级 `AgentDefinition` **实例**（不是类），在对应模块 import 时通过**直接调用** `register_agent(definition, source="builtin")` 注册到全局注册表。`builtin/__init__.py` 导入三个子模块即触发注册。

> **统一约定（修正先前矛盾）**：内置 Agent 一律采用「实例 + 直接调用 register_agent」模式，**不使用装饰器**。装饰器形态（§4.3）仅保留给「装饰一个 `AgentDefinition` 实例赋值表达式」的高级用法，**不**用于装饰 class。下方三个子模块的写法是规范样板。

**只读免批准的真正机制**（重要）：Explore/Plan 之所以「无需用户批准」，**不是**因为 `permission_mode`，而是因为它们只持有只读工具（`Read`/`Glob`/`Grep`，均 `is_read_only=True`），命中 `PermissionEngine` 判定链**第 6 步「只读兜底」**——该步**与 mode 无关**，在任何父模式（含默认 `ask`）下都直接放行。因此内置 Agent 的 `permission_mode` 一律设 `None`（继承父级），**不再设 `acceptEdits`**（先前计划的 `acceptEdits` 既多余、又因第 5 步只放行非 destructive 工具而对编辑类无效）。

### 3.1 GeneralPurpose

```python
# agents/builtin/general.py
from agent_core.agents.registry import register_agent

GENERAL_PURPOSE = AgentDefinition(
    agent_type="GeneralPurpose",
    when_to_use="General-purpose agent for any task that doesn't require a specialist.",
    tools=["*"],
    system_prompt="You are a general-purpose assistant.",
    permission_mode=None,   # 继承父级
    memory=AgentMemory.NONE,
)

# import 时直接注册（非装饰器）
register_agent(GENERAL_PURPOSE, source="builtin")
```

**说明**：默认 Agent。全部工具可用（与父级求交后），权限继承父级。`agent_type` 名风格采用 PascalCase（Python 惯例，见附录 A 兼容对照）。

### 3.2 Explore

```python
# agents/builtin/explore.py
from agent_core.agents.registry import register_agent

EXPLORE = AgentDefinition(
    agent_type="Explore",
    when_to_use=(
        "Subagent used whenever the task requires searching across multiple "
        "files, directories, or patterns, or when the scope of code exploration "
        "is too large for a single read_file or search_file call. "
        "This agent should be invoked any time the task requires understanding "
        "or analyzing code spread across more than 2-3 files.\n"
        "The agent returns findings as text — it should NOT write code."
    ),
    tools=["Read", "Glob", "Grep"],
    system_prompt=(
        "You are a code exploration specialist. Your job is to:\n"
        "1. Search the codebase efficiently using Glob and Grep\n"
        "2. Read and analyze relevant files thoroughly\n"
        "3. Report findings succinctly\n\n"
        "Important: Do NOT edit or write files. You are read-only."
    ),
    permission_mode=None,  # 继承父级；只读免批准由引擎第 6 步保证，与 mode 无关
    memory=AgentMemory.LOCAL,
)

register_agent(EXPLORE, source="builtin")
```

**说明**：只读工具集（`Read`/`Glob`/`Grep`）→ 命中 `PermissionEngine` 第 6 步「只读兜底」→ 在任何父模式下都无需用户批准。适合被频繁调用。**不设 `acceptEdits`**（理由见 §3 开头）。

### 3.3 Plan

```python
# agents/builtin/plan.py
from agent_core.agents.registry import register_agent

PLAN = AgentDefinition(
    agent_type="Plan",
    when_to_use=(
        "Subagent used before writing any code. This agent should be invoked "
        "when the user gives a complex task that requires planning or when "
        "the approach isn't immediately obvious. "
        "The agent reads relevant code, designs an architecture, and returns "
        "a written plan — it should NOT write implementation code."
    ),
    tools=["Read", "Glob", "Grep"],
    system_prompt=(
        "You are a planning specialist. Your job is to:\n"
        "1. Understand the user's requirements\n"
        "2. Explore the relevant codebase to understand existing code\n"
        "3. Design an implementation approach\n"
        "4. Produce a clear, detailed plan\n\n"
        "Important: Do NOT write implementation code. Produce only a plan."
    ),
    permission_mode=None,  # 继承父级；只读免批准由引擎第 6 步保证，与 mode 无关
    memory=AgentMemory.LOCAL,
)

register_agent(PLAN, source="builtin")
```

### 3.4 用户自定义 Agent 启用编辑工具（三层权限示例）

内置 Agent 只读；用户可在 `~/.agent/agents/` 或项目 `.agent/agents/` 下定义需要编辑能力的 Agent。三层语义：

```yaml
# 第 2 层：可用但弹窗确认（最安全）—— refactor.agent.md
---
agent_type: refactorer
tools: [Read, Glob, Grep, Edit, Write]
# 不写 auto_approve_tools → Edit/Write 走 ask 回调，每次弹窗
---
```

```yaml
# 第 3 层：可用且免批准 —— autofix.agent.md
---
agent_type: autofixer
tools: [Read, Glob, Grep, Edit, Write]
auto_approve_tools: [Edit, Write]   # 编译为 allow 规则，绕过 ask 弹窗
---
```

第 3 层免批准**不修改 `permission_mode`**，因此不触发 `min(父, 子)` 降级、也不违反非提权不变量；授权由用户显式书写并负责（详见 §2.1 `auto_approve_tools` 与 §7.5）。

---

## 4. 注册表 (`agents/registry.py`)

### 4.1 AgentRegistry 类

```python
class AgentRegistry:
    """全局 Agent 定义注册表。

    支持多来源分层存储和优先级链查询：
    - builtin: 内置 Agent（最低优先级）
    - plugin:  插件注册的 Agent
    - project: 项目级 .md/.json 定义
    - user:    用户级 ~/.agent/agents/ 定义（最高优先级）

    同 agent_type 的多来源定义通过优先级链合并：
    - 标量字段（system_prompt, when_to_use, model, ...）：高优先级**显式设置**覆盖低优先级
    - 列表字段（tools, skills, hooks）：高优先级**显式设置**覆盖低优先级（非合并）
    - disallowed_tools / auto_approve_tools：所有来源取**并集**
    - mcp_servers：浅合并（同名 server 高优先级覆盖）

    ⚠️ 「显式设置」判定：合并**不能**用「与默认值相等」来判断某层是否设过某字段
    （用户显式写 ``tools=["*"]`` 与不写、走 default_factory 得到的对象完全相同，无法区分）。
    必须改用 Pydantic 的 ``model_dump(exclude_unset=True)`` / ``model_fields_set``
    逐层取「该层真正显式赋过值的字段」，仅这些字段参与覆盖。
    """

    def __init__(self) -> None: ...

    # -- 注册 --
    def register(
        self,
        definition: AgentDefinition,
        source: Literal["builtin", "plugin", "project", "user"] = "project",
    ) -> None:
        """在指定层注册 Agent。同层同名覆盖。"""

    def unregister(self, agent_type: str, source: str | None = None) -> None:
        """注销。source=None 时清除所有层的该 agent_type。"""

    # -- 查询 --
    def get(self, agent_type: str) -> AgentDefinition:
        """优先级链合并后返回。未找到抛 KeyError。"""

    def list_all(self) -> dict[str, AgentDefinition]:
        """返回所有已知 agent_type 的合并结果。"""

    def list_by_source(self, source: str) -> dict[str, AgentDefinition]:
        """返回特定来源层的原始定义（不合并）。"""

    def contains(self, agent_type: str) -> bool: ...

    # -- 内部 --
    def _merge(self, layers: list[AgentDefinition]) -> AgentDefinition:
        """优先级链合并算法。"""
```

### 4.2 优先级链合并算法 (`_merge`)

```
输入: [builtin_def, project_def, user_def]  ← 低→高优先级
输出: 合并后的 AgentDefinition

预处理:
  对每一层调用 layer.model_dump(exclude_unset=True) → 得到「该层显式设置过的字段」dict。
  仅这些字段参与下面的覆盖/并集；未显式设置的字段不参与（避免默认值误覆盖）。

规则:
1. agent_type 不变（所有层必须一致）
2. 标量字段：从低到高顺序遍历，后出现的「显式设置值」覆盖前面的
   - when_to_use, system_prompt, model, permission_mode, memory, background
3. tools：取「最高优先级中显式设置过 tools 的那一层」的值（未显式设置则保持低层 / 默认 ["*"]）
4. disallowed_tools：所有层显式值取**并集**
5. auto_approve_tools：所有层显式值取**并集**（与 disallowed_tools 同语义，权限累加）
6. mcp_servers：浅合并（同名 server 高优先级覆盖）
7. hooks：最高优先级「显式设置层」覆盖
8. skills：最高优先级「显式设置层」覆盖
```

> **实现提示**：可先 `merged = layers[0].model_dump()`（含默认值作底），再按低→高
> 用各层 `model_dump(exclude_unset=True)` 逐层 `update`，对 disallowed_tools /
> auto_approve_tools 改为并集累加而非覆盖，最后 `AgentDefinition.model_validate(merged)`。

### 4.3 模块级默认实例 + 便捷函数

```python
# 全局单例
_default_registry = AgentRegistry()

def register_agent(
    definition: AgentDefinition | None = None,
    *,
    source: str = "project",
) -> Callable | AgentDefinition:
    """装饰器 / 直接调用 两用注册函数。

    用法 1 — 装饰器:
        @register_agent(source="builtin")
        class MyAgent:
            agent_type = "MyAgent"
            ...

    用法 2 — 直接调用:
        register_agent(my_definition, source="project")
    """
    ...

def get_agent(agent_type: str) -> AgentDefinition:
    """从全局注册表查询合并后的定义。"""
    return _default_registry.get(agent_type)

def list_agents() -> dict[str, AgentDefinition]:
    """列出全局注册表中所有 Agent。"""
    return _default_registry.list_all()
```

---

## 5. 加载器 (`agents/loader.py`)

三种加载方式，返回 `AgentDefinition` 或 `None`（解析失败时）。

### 5.1 Markdown Frontmatter 加载

```python
def load_markdown_definition(file_path: str) -> AgentDefinition | None:
    """从 .md 文件的 YAML frontmatter 解析 AgentDefinition。

    格式示例 (SKILL.md 风格):
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

    # Code Reviewer

    Body text is ignored by the loader (reserved for Skill system).
    ```

    依赖: python-frontmatter >= 1（已在 pyproject.toml 中）
    """
```

### 5.2 JSON 文件加载

```python
def load_json_definition(file_path: str) -> AgentDefinition | None:
    """从 .json 文件加载 AgentDefinition。

    格式:
    ```json
    {
      "agent_type": "code-reviewer",
      "when_to_use": "...",
      "tools": ["Read", "Glob"],
      "system_prompt": "You are a code reviewer..."
    }
    ```
    """
```

### 5.3 目录扫描（发现机制）

```python
def discover_agents(
    directory: str,
    source: str = "project",
    include_plain_md: bool = False,
) -> dict[str, AgentDefinition]:
    """递归扫描目录，解析 agent 定义文件为 AgentDefinition。

    文件命名约定（默认仅前两类）：
    - <name>.agent.md    →  markdown 定义
    - <name>.agent.json  →  JSON 定义
    - <name>.md          →  仅当 include_plain_md=True 时尝试（兼容 SKILL.md 风格）

    ⚠️ 默认 ``include_plain_md=False``：避免把 SKILL.md 等无 ``agent_type`` 的裸 .md
    误当 agent 解析而刷屏 warning。裸 .md 缺 ``agent_type`` 时**静默跳过**（debug 级日志）。

    同 agent_type 多文件时，.agent.md > .agent.json > .md（warn 冲突）。
    解析失败的文件打印 warning 跳过，不阻塞整体扫描。
    """
```

**扫描路径约定**：
| 来源 | 路径 | source 参数 |
|------|------|-------------|
| 项目级 | `<workspace_root>/.agent/agents/` | `"project"` |
| 用户级 | `~/.agent/agents/` | `"user"` |

---

## 6. 公开导出 (`agents/__init__.py`)

```python
from agent_core.agents import (
    AgentDefinition,
    AgentMemory,
    AgentRegistry,
    get_agent,
    list_agents,
    register_agent,
    # 加载器（高级用法）
    load_markdown_definition,
    load_json_definition,
    discover_agents,
)
```

`agent_core/__init__.py` 增量：
```python
from .agents import (
    AgentDefinition,
    AgentMemory,
    get_agent,
    list_agents,
    register_agent,
)
```

---

## 7. 与现有代码的集成点

### 7.1 `permissions/inheritance.py`（已实现，无需修改）

三个纯函数已就绪：
- `compute_child_permission_mode(parent_mode, child_definition_mode)` — `child_definition_mode` 直接取 `AgentDefinition.permission_mode`
- `compute_child_allowed_tools(parent_allowed, child_allowed_spec)` — `child_allowed_spec` 直接取 `AgentDefinition.tools`
- `compute_child_disallowed_tools(parent_disallowed, child_disallowed_spec)` — `child_disallowed_spec` 直接取 `AgentDefinition.disallowed_tools`

Item 7 无需修改这些函数，Item 8 的 `run_agent()` 直接调用。

> **关于只读免批准**：`compute_child_permission_mode` 的 `min(父, 子)` 不变量意味着子 Agent
> **无法提权**。但内置 Explore/Plan 仍能「零批准」运行——这与 `permission_mode` 无关，
> 而是因为 `PermissionEngine.check()` 第 6 步对 `is_read_only=True` 工具无条件放行（engine.py:98-103）。
> 因此内置只读 Agent 一律 `permission_mode=None`，不依赖也不应设 `acceptEdits`。

### 7.2 `ToolRegistry`（已实现，无需修改）

`ToolRegistry` 已支持 query-scoped 实例。子 Agent 工具池过滤流程（Item 8）：
1. 根据 `AgentDefinition.tools` + `compute_child_allowed_tools()` 计算实际允许的工具名 set
2. 创建新的 `ToolRegistry()` 实例
3. 从父 registry 按名拷贝被允许的工具到子 registry

### 7.3 `models.py` 变更

**结论：`models.py` 不新增任何类。** `AgentMemory` 枚举与 `AgentDefinition` 模型均定义在
`agents/definition.py`；`definition.py` 仅从 `models.py` *import* 已有的 `PermissionMode`、
`McpServerConfig` 复用。

**决策**：`AgentDefinition` 和 `AgentMemory` 定义在 `agents/definition.py` 而非 `models.py`。理由：
- `models.py` 已承载 10+ 个模型类，继续膨胀不利于维护
- `AgentDefinition` 是 agents 子系统的核心，放在 `agents/definition.py` 符合模块内聚原则
- `models.py` 中的模型均被多个子系统使用；`AgentDefinition` 几乎仅被 agents 子系统消费
- 对外导出仍通过 `agent_core/__init__.py` 统一入口，调用方不受影响

### 7.4 CLI 集成（Item 7 不实现）

`cli.py` 暂不修改。Item 8 实现 `AgentTool` 后，`/agent <type>` REPL 命令可后续追加。

### 7.5 `auto_approve_tools` → `PermissionEngine`（Item 8 消费契约）

Item 7 仅**定义** `auto_approve_tools` 字段；Item 8 的 `run_agent()` 在为子 Agent 构建
`PermissionEngine` 时按以下契约消费它，将「免批准」诉求落到引擎的规则链（第 1–4 步，
优先级高于 `permission_mode`）：

```python
from agent_core.permissions.engine import PermissionEngine
from agent_core.permissions.rules import PermissionRule, default_rules
from agent_core.permissions.inheritance import compute_child_permission_mode

def build_child_engine(parent_mode, definition):
    # 1) 每个预授权工具 → 一条 allow 规则（高 priority 以压过默认规则）
    extra_rules = [
        PermissionRule(
            tool_pattern=name,
            action="allow",
            priority=0,  # 数值越小越先匹配；确保压过 default_rules
            description=f"pre-authorized for agent '{definition.agent_type}'",
        )
        for name in definition.auto_approve_tools
    ]
    # 2) mode 仍走非提权钳制（auto_approve_tools 不参与提权，二者正交）
    child_mode = compute_child_permission_mode(parent_mode, definition.permission_mode)
    return PermissionEngine(rules=default_rules() + extra_rules, mode=child_mode)
```

**安全约束（Item 8 必须保证）**：
- `auto_approve_tools` 中的工具仍须先通过工具池过滤（§7.2）——若某工具不在
  `tools ∩ 父级允许集` 内，则即便在 `auto_approve_tools` 里也**调用不到**，allow 规则形同空转。
- 该机制**只放宽「弹不弹窗」，绝不放宽「能不能调」**，因此不构成提权绕过。
- 父级 `disallowed_tools`（§7.1 第三函数取并集）优先级语义上高于子级 `auto_approve_tools`：
  Item 8 应在生成 allow 规则前先剔除落入子级有效 `disallowed` 集的工具名，避免「父禁 + 子免批」冲突。

> 此契约写在 Item 7 计划中是为了固定字段语义；`engine.py` / `rules.py` 在 Item 7 阶段**不修改**。

---

## 8. 测试计划

### 8.1 单元测试 (`tests/unit/test_agent_definition.py`)

| # | 测试项 | 描述 |
|---|--------|------|
| 1 | `AgentDefinition` 默认值 | 最小字段构造，验证所有默认值正确 |
| 2 | `agent_type` 校验 | 空字符串、含特殊字符 → ValueError |
| 3 | `tools` 非空校验 | `[]` → ValueError |
| 4 | `AgentMemory` 枚举值 | 验证 4 个成员 |
| 5 | 序列化/反序列化 | `model_dump()` → `model_validate()` 往返一致 |
| 6 | `model` 字段 None vs 值 | None 正确保留，有效字符串通过 |
| 7 | `permission_mode` None vs 值 | None 和三种模式均通过校验 |
| 8 | `auto_approve_tools` 默认 `[]` | 不传时为空列表 |
| 9 | `model_dump(exclude_unset=True)` 区分显式/默认 | 显式 `tools=["*"]` 出现在 dump，未传则不出现（合并算法前提） |

### 8.2 注册表测试 (`tests/unit/test_agent_registry.py`)

| # | 测试项 | 描述 |
|---|--------|------|
| 1 | 单层注册 + 查询 | 注册 → get() 返回相同定义 |
| 2 | 同名覆盖（同层） | 同 source 同 agent_type 后注册覆盖 |
| 3 | 优先级链合并 — 标量 | user 层 system_prompt 覆盖 builtin 层 |
| 4 | 优先级链合并 — disallowed_tools 并集 | builtin 禁 A, project 禁 B → 结果禁 A+B |
| 5 | 优先级链合并 — mcp_servers 浅合并 | 不同名 server 合并，同名覆盖 |
| 6 | 优先级链合并 — tools 覆盖 | project 层 `["Read", "Glob"]` 覆盖 builtin `["*"]` |
| 7 | 查询不存在的 agent_type → KeyError | |
| 8 | `list_all()` 返回所有合并结果 | |
| 9 | `list_by_source()` 返回原始定义 | |
| 10 | `unregister()` 单层 | 删除 project 层后 fallback 到 builtin |
| 11 | `unregister()` 全层 | agent_type 完全消失 |
| 12 | `contains()` 查询 | 存在/不存在均正确 |
| 13 | `register_agent` 直接调用 | 传入 definition 实例直接注册（内置 Agent 规范用法） |
| 14 | `register_agent` 装饰器形态 | 装饰 `AgentDefinition` 实例表达式正确注册（高级用法） |
| 15 | 优先级链合并 — auto_approve_tools 并集 | project 免批 Edit, user 免批 Write → 结果免批 Edit+Write |
| 16 | 合并不被默认值误覆盖 | 高层未显式设 tools 时，低层显式 `["Read"]` 不被默认 `["*"]` 覆盖 |

### 8.3 加载器测试 (`tests/unit/test_agent_loader.py`)

| # | 测试项 | 描述 |
|---|--------|------|
| 1 | Markdown frontmatter 完整解析 | 所有字段正确提取 |
| 2 | Markdown 无 frontmatter | 返回 None（不抛异常） |
| 3 | Markdown frontmatter 缺必填字段 | 返回 None + warning |
| 4 | JSON 完整解析 | 所有字段正确提取 |
| 5 | JSON 格式错误 | 返回 None |
| 6 | `discover_agents()` 扫描目录 | 正确找到 .agent.md 和 .agent.json |
| 7 | `discover_agents()` 空目录 | 返回 {} |
| 8 | `discover_agents()` 同 agent_type 冲突 | warning + 按优先级选择 |

### 8.4 内置 Agent 测试 (`tests/unit/test_builtin_agents.py`)

| # | 测试项 | 描述 |
|---|--------|------|
| 1 | 3 个内置 Agent 均已注册 | import agents.builtin 后 registry 包含三者 |
| 2 | GeneralPurpose agents=["*"] | |
| 3 | Explore tools 仅只读工具 | Read, Glob, Grep |
| 4 | Plan tools 仅只读工具 | Read, Glob, Grep |
| 5 | Explore/Plan permission_mode 为 None | 继承父级，**不**为 acceptEdits |
| 6 | 内置 Agent auto_approve_tools 为空 | 内置 Agent 不预授权任何工具 |
| 7 | 内置 Agent agent_type 不重复 | |
| 8 | 只读工具免批准（引擎层） | 用 `ask` 模式的 `PermissionEngine` 校验 Read/Glob/Grep → allowed（命中第 6 步），验证不依赖 acceptEdits |

---

## 9. 实现步骤（按顺序）

### Step 1: `AgentMemory` 枚举 + `AgentDefinition` 模型
- **文件**: `src/agent_core/agents/definition.py`（新建）
- **内容**: `AgentMemory` 枚举 + `AgentDefinition` Pydantic BaseModel + field validators
- **依赖**: `models.py` 中的 `PermissionMode`, `McpServerConfig`

### Step 2: `AgentRegistry` 注册表
- **文件**: `src/agent_core/agents/registry.py`（新建）
- **内容**: `AgentRegistry` 类 + `_merge()` 优先级链合并 + 模块级 `_default_registry` + 便捷函数
- **依赖**: `definition.py`

### Step 3: 内置 Agent
- **文件**: `src/agent_core/agents/builtin/`（新建目录 + 4 个文件）
- **内容**: general.py / explore.py / plan.py + `__init__.py`（import 触发注册）
- **依赖**: `registry.py` 的 `@register_agent` 装饰器

### Step 4: 加载器
- **文件**: `src/agent_core/agents/loader.py`（新建）
- **内容**: `load_markdown_definition()` / `load_json_definition()` / `discover_agents()`
- **依赖**: `python-frontmatter`（已在 pyproject.toml 中）

### Step 5: 模块公开 API 聚合
- **文件**: `src/agent_core/agents/__init__.py`（新建）
- **内容**: 模块级导出聚合
- **依赖**: 以上 4 步全部完成

### Step 6: `agent_core/__init__.py` 增量
- **文件**: `src/agent_core/__init__.py`（修改）
- **内容**: 增加 `AgentDefinition`, `AgentMemory`, `get_agent`, `list_agents`, `register_agent` 导出
- **注意**: 内置 Agent 需在此时 import → `from .agents.builtin import *`

### Step 7: 单元测试
- **文件**: `tests/unit/test_agent_definition.py` / `test_agent_registry.py` / `test_agent_loader.py` / `test_builtin_agents.py`
- **内容**: 按 §8 测试计划实现

---

## 10. 风险与约束

| 风险 | 缓解措施 |
|------|---------|
| `AgentDefinition` 字段过多，与 `AgentConfig` 重叠引起混淆 | §1.2 明确区分；`AgentDefinition` 不包含 API key / max_turns 等运行时字段 |
| 优先级链合并逻辑复杂，边界情况易出错 | `_merge()` 基于 **`model_dump(exclude_unset=True)`**（而非与默认值比较）逐字段合并，单元测试覆盖 16 个场景（含「默认值不误覆盖」专项） |
| **误以为 `acceptEdits` 能让编辑类工具免批准** | 本引擎 `acceptEdits` 仅放行非 destructive 工具；`Edit`/`Write`/`Bash` 均 destructive。免批准必须走 `auto_approve_tools` → `allow` 规则（§2.1 / §7.5），**不可**靠 mode。只读免批准由引擎第 6 步保证，与 mode 无关 |
| **子 Agent 提权绕过权限边界** | `auto_approve_tools` 只放宽「弹不弹窗」不放宽「能不能调」，且仍受工具池 ∩ 与父级 `disallowed` 并集约束（§7.5 安全约束）；`permission_mode` 经 `min(父, 子)` 钳制无法提权 |
| **全局 `_default_registry` 单例在测试间泄漏**（内置 Agent 在 import 时注册） | 注册表测试用独立 `AgentRegistry()` 实例，或提供 `reset()`/pytest fixture 在每个测试前后清理；不依赖全局单例做断言 |
| `discover_agents` 把无 `agent_type` 的裸 `.md`（如 SKILL.md）当 agent 解析产生噪音 warning | 默认只扫 `.agent.md` / `.agent.json`；裸 `.md` 解析设为可选开关，缺 `agent_type` 时静默跳过（debug 级日志而非 warning） |
| `python-frontmatter` 对畸形 YAML 的容错性 | 加载器使用 try/except 包裹，解析失败返回 None 而非抛异常 |
| 与 Item 8 接口不匹配 | `permissions/inheritance.py` 的三个函数 + §7.5 `auto_approve_tools` 消费契约已定义好入参/出参，`AgentDefinition` 字段命名直接对齐 |

---

## 附录 A: 与 Claude Code Agent 系统的兼容对照

| Claude Code 内置 Agent | 本项目 agent_type | 说明 |
|------------------------|-------------------|------|
| `general-purpose` | `GeneralPurpose` | 命名风格从 kebab-case 改为 PascalCase（Python 惯例） |
| `Explore` | `Explore` | 同名 |
| `Plan` | `Plan` | 同名（Claude Code Plan agent 作为子 agent） |

**when_to_use 文本**：Explore 和 Plan 的 `when_to_use` 文本直接参考了 Claude Code 对应 agent 的描述措辞，以最大化 LLM 路由准确性。

## 附录 B: discover_agents 扫描目录示例

```
~/.agent/agents/
├── my-reviewer.agent.md       → agent_type="my-reviewer"
├── deployment.agent.json      → agent_type="deployment"
├── README.md                  → 跳过（无 frontmatter）
└── subdir/
    └── custom.agent.md        → 递归发现
```
