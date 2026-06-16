# Item 5: 工具执行管线 & 权限系统 — 实现计划

## Context

当前 `query/loop.py` 中的 `_execute_tools_sequential()` 是一个占位实现：所有工具串行执行，权限检查只有简单的 callback 调用。需要替换为完整的并发/串行编排管线、权限规则引擎、Bash AST 安全检查，以及子 Agent 权限继承协议定义。这是 Agent 的安全边界实现。

---

## 变更范围

### 新建文件

| 文件 | 职责 |
|------|------|
| `src/agent_core/permissions/__init__.py` | 公开 API 导出 |
| `src/agent_core/permissions/engine.py` | 权限规则引擎（匹配链 + PermissionMode 集成） |
| `src/agent_core/permissions/rules.py` | PermissionRule Pydantic 模型 + 序列化 |
| `src/agent_core/permissions/inheritance.py` | 子 Agent 权限继承协议（Protocol + test stubs） |
| `src/agent_core/permissions/bash_guard.py` | Bash AST 安全检查（tree-sitter-bash） |

### 修改文件

| 文件 | 变更 |
|------|------|
| `src/agent_core/models.py` | AgentConfig 新增 `max_concurrency` 字段（默认 5） |
| `src/agent_core/query/loop.py` | 替换 `_execute_tools_sequential()` 为 `run_tools()` + `run_tool_use()` |
| `tests/unit/test_permissions.py` | 从占位文件改为权限引擎单元测试 |
| `tests/unit/test_tools_bash.py` | 追加 Bash AST 安全检查测试 |

### 依赖项

所有依赖已就绪（`pyproject.toml`: `tree-sitter>=0.24`, `tree-sitter-bash>=0.23`, `pydantic>=2`, `tenacity>=9`）。

---

## 详细设计

### 1. PermissionRule 模型 (`permissions/rules.py`)

```python
class PermissionRule(BaseModel):
    tool_pattern: str          # 工具名精确匹配 / 前缀* / fnmatch 通配符
    action: Literal["allow", "deny", "ask"]
    priority: int = 0          # 越小优先级越高
    path_constraint: str | None = None  # 可选：路径 glob 约束
    description: str = ""      # 规则说明（面向用户）
```

设计要点：
- `tool_pattern` 支持三种匹配：精确（`"Bash"`）、前缀（`"File*"` 自动转 fnmatch）、通配符（`"mcp__*"`）
- 规则可序列化为 JSON，存储在 `~/.agent-core/permissions.json`（与现有 `agent-core.config.json` 同级）
- 内置默认规则集（`Read`/`Glob`/`Grep` 默认 allow，`Bash`/`Edit`/`Write` 默认 ask）

### 2. PermissionEngine (`permissions/engine.py`)

匹配链（按优先级）：
```
1. 精确 tool_pattern 匹配 → 2. 前缀匹配 → 3. fnmatch 通配符 → 
4. path_constraint 检查 → 5. PermissionMode 回退 → 6. read_only 兜底 → 7. 默认 ask
```

核心方法：
```python
class PermissionEngine:
    def __init__(self, rules: list[PermissionRule], mode: PermissionMode):
        ...
    
    async def check(
        self, tool_name: str, tool_input: dict, 
        callback: PermissionCallback | None,
        tool_is_read_only: bool = False
    ) -> PermissionDecision:
        """执行匹配链，返回决策。"""
```

`PermissionDecision`:
```python
class PermissionDecision(BaseModel):
    allowed: bool
    reason: str
    source: Literal["rule", "mode", "callback", "default"]
```

**PermissionMode 集成**：
- `bypassPermissions` → 所有工具自动 allowed（跳过 callback）
- `acceptEdits` → 非破坏性工具 allowed，破坏性工具 ask
- `ask` → 调用 callback；无 callback 时拒绝破坏性工具，允许只读工具

### 3. 子 Agent 权限继承协议 (`permissions/inheritance.py`)

定义 Protocol + 纯函数（不依赖 AgentDefinition，仅契约）：

```python
def compute_child_permission_mode(
    parent_mode: PermissionMode, child_definition_mode: PermissionMode | None
) -> PermissionMode:
    """子 = min(父, 子定义)，不可提升。bypass > acceptEdits > ask"""

def compute_child_allowed_tools(
    parent_allowed: frozenset[str], child_allowed_spec: list[str]
) -> frozenset[str]:
    """子 = 父 ∩ 子定义（交集）。["*"] 展开为父全部。"""

def compute_child_disallowed_tools(
    parent_disallowed: frozenset[str], child_disallowed_spec: list[str]
) -> frozenset[str]:
    """子 = 父 ∪ 子定义（并集）。"""
```

测试桩：`tests/unit/test_permissions.py` 中包含 `TestPermissionInheritance` 类，覆盖 6 个场景：
- 子模式 = 父（同等级）
- 子声明 bypass 但父是 ask → 子降级为 ask
- 父禁止 Bash → 子 tools=["*"] → Bash 被过滤
- 子显式禁止父允许的工具 → 并集生效
- 子声明的工具父不允许 → 交集过滤为空
- disallowed_tools 并集

### 4. Bash AST 安全检查 (`permissions/bash_guard.py`)

```python
class BashSafetyResult(BaseModel):
    safe: bool
    warnings: list[str]
    dangerous_patterns: list[dict[str, str]]  
    # [{"pattern": "curl_pipe_bash", "location": "command at position X", "severity": "critical"}]

def analyze_bash_command(command: str) -> BashSafetyResult:
    """使用 tree-sitter-bash 解析命令 AST，检查危险模式。"""
```

**危险模式检测**（按严重程度）：

| 严重度 | 模式 | 检测逻辑 |
|--------|------|---------|
| critical | `curl/wget ... \| bash/sh` | AST 中 pipe 节点的左子树包含 curl/wget 调用，右子树是 bash/sh |
| critical | `rm -rf /` 或 `rm -rf /*` | rm 命令的路径参数解析后为 `/` 或 `/*` |
| critical | `/dev/null` 重定向覆盖系统文件 | `>` 或 `>>` 重定向到 `/dev/null` 且源路径是 `/etc/*` 或 `~/*` |
| high | `chmod 777` 在系统路径 | chmod 777 且路径是 `/usr/*`, `/bin/*`, `/etc/*` |
| high | `sudo` 执行危险命令 | sudo + 以上任一危险模式 |
| medium | `eval` 使用变量 | eval 的参数包含 `$` 变量引用 |
| warn | 未加引号的变量扩展 | `$VAR` 出现在命令参数位置未加引号 |

**复合命令遍历**：使用 tree-sitter visitor 模式遍历 `&&` / `||` / `|` / `;` / `$()` / 反引号节点，对每个子命令独立检查。

### 5. 工具执行管线 (`query/loop.py`)

替换 `_execute_tools_sequential()` 为两个新函数：

```python
async def run_tools(
    tool_uses: list[ToolUse],
    context: ToolUseContext,
    permission_callback: PermissionCallback | None = None,
    permission_engine: PermissionEngine | None = None,
    max_concurrency: int = 5,
) -> list[ToolResult]:
```

**编排逻辑**：
1. 按 `Tool.is_concurrency_safe` 类属性分组（无需实例化→查询 registry 获取类）
2. 非并发工具 → 串行 `for ... await`
3. 并发工具 → `asyncio.Semaphore(max_concurrency)` + `asyncio.gather()`
4. 结果按原始 `tool_uses` 顺序输出（用 index 排序）

```python
async def run_tool_use(
    tool_use: ToolUse,
    context: ToolUseContext,
    permission_callback: PermissionCallback | None = None,
    permission_engine: PermissionEngine | None = None,
) -> ToolResult:
```

**单工具生命周期** (7 步)：

```
1. resolve   → get_tool(name) → 失败返回 error ToolResult
2. validate  → tool.validate_input(raw_dict) → 失败返回 error ToolResult
3. bash_guard → BashTool 时调用 analyze_bash_command() → 失败返回 error ToolResult
4. permission → permission_engine.check() → 拒绝返回 error ToolResult
5. pre-hooks → 遍历 PreToolUse hooks（当前 hooks 列表为空，保留 Hook 调用接口）
6. execute   → tool.call(validated_input, context)
7. persist   → 结果 > 50KB 时写入临时文件，截断展示
```

**步骤 5 (pre-hooks) 处理**：当前 `hooks/` 目录为空，`AgentConfig.hooks` 默认 `[]`。保留接口但暂时跳过 hook 执行（hooks 系统在 Item 9 实现后再接入）。

### 6. 结果持久化

当 `ToolCallResult.content` 为字符串且超过 50KB：
- 写入 `{tempfile.gettempdir()}/agent_core/results/{agent_id}/{timestamp}_{tool_name}.txt`
- 返回的 `ToolResult.content` 替换为截断文本 + 文件路径引用
- `ToolResult.is_error = False`, 但附注截断信息

### 7. AgentConfig 新增字段 (`models.py`)

```python
max_concurrency: int = Field(
    default=5,
    ge=1,
    le=20,
    description="Maximum number of concurrency-safe tools to execute in parallel",
)
```

---

## 文件修改详情

### `src/agent_core/permissions/__init__.py`

导出：`PermissionEngine`, `PermissionRule`, `PermissionDecision`, `compute_child_permission_mode`, `compute_child_allowed_tools`, `compute_child_disallowed_tools`, `analyze_bash_command`, `BashSafetyResult`

### `src/agent_core/permissions/rules.py` (~80 行)

`PermissionRule` Pydantic model + 内置默认规则集工厂函数 `default_rules()`

### `src/agent_core/permissions/engine.py` (~200 行)

`PermissionEngine` 类 + `PermissionDecision` model + 匹配链实现 + PermissionMode 集成

### `src/agent_core/permissions/inheritance.py` (~80 行)

3 个纯函数 + 详细 docstring 说明契约

### `src/agent_core/permissions/bash_guard.py` (~250 行)

tree-sitter-bash AST 解析 + 危险模式检测 + 复合命令递归遍历

### `src/agent_core/models.py` (增量 ~5 行)

AgentConfig 添加 `max_concurrency` 字段

### `src/agent_core/query/loop.py` (增量 ~120 行)

- 移除 `_execute_tools_sequential()`
- 新增 `run_tools()` 和 `run_tool_use()`
- `query()` 中构造 `PermissionEngine` 并传入 `run_tools()`
- `query()` 调用从 `_execute_tools_sequential(...)` 改为 `run_tools(...)`

### `tests/unit/test_permissions.py` (~200 行)

替换占位文件，覆盖：
- `TestPermissionEngine`: 匹配链优先级、PermissionMode 行为、callback 集成
- `TestPermissionInheritance`: 6 个场景（mode min / allowed 交集 / disallowed 并集）
- `TestBashGuard`: 危险命令检测、安全命令通过、复合命令解析

### `tests/unit/test_tools_bash.py` (增量 ~100 行)

追加 test class `TestBashGuardIntegration`：BashTool.call() 在管线中触发 bash_guard 检查

---

## 验证

1. **单元测试**: `pytest tests/unit/test_permissions.py -v` — 权限引擎 + 继承协议 + bash guard
2. **Bash 工具测试**: `pytest tests/unit/test_tools_bash.py -v` — 确保 bash_guard 集成不破坏现有行为
3. **模型验证**: `pytest tests/unit/test_models.py -v` — max_concurrency 字段校验
4. **集成测试**: `pytest tests/integration/test_query_loop.py -v` — 确认 query() 仍然正常运行
5. **全量回退**: `pytest -x` — 确保无回归
