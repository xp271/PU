# Item 9: Hooks + Plugins + Skills — API 参考

> **状态** (2026-06-25)：**已实现并接线，测试全绿**(全套 824 passed)。
> 本文档按当前代码(`src/agent_core/{hooks,plugins,skills}/` + 集成点)编写,
> 作为 as-built API 参考。三系统:Hook 管线(阻断/改写/终止)、Plugin 热插拔
> 注册表、Skill 定义与执行(inline / fork)。

---

## 0. 模块总览

| 包 | 文件 | 职责 |
|----|------|------|
| `agent_core.hooks` | `types.py` | `HookKind` / `HookBase` / 结果模型 / 4 个 Protocol |
| | `registry.py` | `HookRegistry` + 只读模板 `_default_hook_registry` |
| | `pipeline.py` | 4 个管线执行器 |
| `agent_core.plugins` | `base.py` | `Plugin` 抽象基类 |
| | `registry.py` | `PluginRegistry`(entry_points 发现 + 手动注册) |
| | `lifecycle.py` | `safe_enable_plugins` / `safe_disable_all` |
| `agent_core.skills` | `definition.py` | `SkillParameter` / `SkillDefinition` |
| | `loader.py` | `load_skill_from_markdown` / `discover_skills` |
| | `registry.py` | `SkillRegistry` |
| | `executor.py` | `render_template` / `execute_skill_inline` / `execute_skill_fork` |
| | `tool.py` | `SkillTool`(name=`"Skill"`)+ 注册表 plumbing |
| | `permissions.py` | `SkillPermissionRule` / `SkillPermissionEngine` |

集成点:`query/loop.py`、`agent.py`、`agents/run.py`、`agents/tool.py`、
`tools/base.py`、`models.py`、`__init__.py`、`cli.py`。

### 生命周期总图(as-built)

```
Agent.stream() / query()
  │  hook_registry = _default_hook_registry.copy_with_overrides(config.hooks)  (None 时)
  ▼
query() → _run_turns()  ── bootstrap ──┐
  │   skill_registry = discover(config.skills_dir > project > user)  [asyncio.to_thread, first-wins]
  │   if config.auto_enable_plugins: PluginRegistry().discover() + safe_enable_plugins(...)
  │       └─ 注入 → tool_registry / hook_registry / skill_registry / agent_registry
  ▼
turn 循环
  ├─ 2a0   notification drain
  ├─ 2a0.5 StopHook  (turn 顶部，消息合法；should_stop=True → AssistantText? + DoneMessage)
  ├─ 2b    LLM call
  └─ 2e    run_tools → run_tool_use():
              1 resolve · 2 validate · 3 bash_guard · 4 permission
              5 PreToolUse  (block / modify→重跑 3+4)
              6 execute
              7 PostToolUse (改写完整 ToolCallResult，在 persist 之前)
              8 persist
AgentTool.call() → SubagentStartHook(可 block) → run_agent()(继承 hook_registry)
```

---

## 1. Hooks API

### 1.1 `hooks/types.py`

#### `HookKind`
```python
HookKind = Literal["pre_tool_use", "post_tool_use", "stop", "subagent_start"]
```
Hook 的**运行时分类靠显式 `hook_kind` 属性**,不靠 `isinstance` —— 四个 Protocol
都只有 `__call__`,`@runtime_checkable` 无法区分。

#### `HookBase`
```python
class HookBase:
    hook_kind: ClassVar[HookKind]
```
可选基类。Hook 实现也可不继承,只要对象上有 `hook_kind` 属性(duck typing)。

#### 结果模型
```python
class HookContinue(BaseModel):  action: Literal["continue"] = "continue"
class HookBlock(BaseModel):     action = "block"; reason: str = ""; message: str | None = None
class HookModify(BaseModel):    action = "modify"; new_result: Any
HookResult = HookContinue | HookBlock | HookModify

class StopResult(BaseModel):    should_stop: bool = False; reason: str = ""; message: str | None = None
```

#### Protocol(签名 + `hook_kind` 取值)

| Protocol | `hook_kind` | `__call__` 参数 | 返回 |
|----------|-------------|-----------------|------|
| `PreToolUseHook` | `"pre_tool_use"` | `(tool_name, tool_input, context)` | `HookResult` |
| `PostToolUseHook` | `"post_tool_use"` | `(tool_name, tool_input, tool_result, context)` | `HookResult` |
| `StopHook` | `"stop"` | `(messages, context)` | `StopResult` |
| `SubagentStartHook` | `"subagent_start"` | `(agent_type, prompt, definition, context)` | `HookResult` |

语义:
- **PreToolUse**:`HookBlock`→拒绝;`HookModify(new_result=<dict>)`→替换 `tool_input`,
  管线随后**重跑 bash_guard + 权限**;`HookContinue`→放行。
- **PostToolUse**:收到 `tool.call()` 的**完整** `ToolCallResult`(persist 之前);
  `HookModify(new_result=...)`→替换 `.content`;`HookBlock`→标记 error。
- **StopHook**:`should_stop=True`→ 终止循环;`message` 作为 `AssistantText` 在
  `DoneMessage` 之前发出。
- **SubagentStartHook**:`HookBlock`→阻止子 Agent 启动;`HookModify` 被忽略(当 continue)。

### 1.2 `hooks/registry.py` — `HookRegistry`

每-query 一个实例,挂在 `ToolUseContext.hook_registry`。

| 方法 | 签名 | 说明 |
|------|------|------|
| `register` | `(hook, priority=100) -> None` | 按 `hook.hook_kind` 路由到桶;缺失 `hook_kind` 抛 `ValueError` |
| `register_all` | `(hooks, priority=100) -> None` | 批量 |
| `pre_tool_use_hooks` | `() -> list` | 按 priority 升序(同序按注册顺序,靠单调 `_seq` 稳定) |
| `post_tool_use_hooks` / `stop_hooks` / `subagent_start_hooks` | `() -> list` | 同上 |
| `is_empty` | `() -> bool` | 管线零开销短路 |
| `copy_with_overrides` | `(extra_hooks) -> HookRegistry` | 复制自身全部 hook,再追加 `extra_hooks`(priority=100);用于子 Agent 隔离 |

**`_default_hook_registry`**:模块级只读模板(导入期注册内置 hook)。`query()` 经
`copy_with_overrides(config.hooks)` 派生 per-query 实例,**绝不 mutate 模板**。
priority 越小越先;内置 hook 用 `0`,用户默认 `100`。

### 1.3 `hooks/pipeline.py` — 执行器

```python
async def run_pre_tool_use_hooks(tool_name, tool_input, context, registry=None)
    -> tuple[dict | None, str | None]            # (modified_input, block_reason)；任一 block 短路；modify 最后一个生效

async def run_post_tool_use_hooks(tool_name, tool_input, tool_result, context, registry=None)
    -> ToolCallResult                            # 链式改写后的结果；block→error 短路

async def run_stop_hooks(messages, context, registry=None)
    -> StopResult                                # 任一 should_stop=True 短路返回

async def run_subagent_start_hooks(agent_type, prompt, definition, context, registry=None)
    -> tuple[bool, str | None]                   # (blocked, reason)
```
容错:pre/post/subagent-start 的 hook 抛异常 → 视为 block;stop hook 抛异常 → 视为
"无意见"继续。`registry is None or is_empty()` → 直接放行短路。

### 1.4 集成点

| 文件 | 位置 | 行为 |
|------|------|------|
| `query/loop.py` | `query()` / `_run_turns()` 新增 `hook_registry` 参数 | `None` 时自建 `_default_hook_registry.copy_with_overrides(config.hooks)`,赋给 `ToolUseContext.hook_registry` |
| | `_run_turns` turn 顶部(`2a0.5`) | `run_stop_hooks`;`should_stop` → 先 `AssistantText(message)`、drain 后台 agent、再 `DoneMessage("end_turn")` |
| | `run_tool_use()` 步骤 5 | `run_pre_tool_use_hooks`;block→error;modify→重 `validate_input` + `_guard_and_permit`(重跑 bash_guard+权限) |
| | `run_tool_use()` 步骤 7(persist 前) | `run_post_tool_use_hooks` 对完整 `ToolCallResult` |
| `agent.py` | `Agent.stream()` | `hook_registry = _default_hook_registry.copy_with_overrides(config.hooks)` 传入 `query()` |
| `agents/run.py` | `run_agent()` | 子 hook_registry = `parent.hook_registry.copy_with_overrides(definition.hooks)`(无父则新建+`register_all`);赋给 `child_context` 并传给子 `query()` |
| `agents/tool.py` | `AgentTool.call()` | resolve 之后、route 之前调 `run_subagent_start_hooks`,`blocked` → error |

---

## 2. Skills API

### 2.1 `skills/definition.py`

```python
class SkillParameter(BaseModel):
    type: str = "string"          # 仅文档用途
    description: str = ""
    required: bool = False

class SkillDefinition(BaseModel):
    name: str
    description: str = ""
    tools: list[str] = ["*"]
    model: str | None = None
    permission_mode: str | None = None
    system_prompt: str = ""
    prompt: str = ""              # SKILL.md 正文(模板)
    parameters: dict[str, SkillParameter] = {}
    execution_mode: Literal["inline", "fork"] = "inline"
    background: bool = False      # fork + background → 异步后台子 Agent
```

### 2.2 `SKILL.md` 格式 → 字段映射

约定:**一个 skill = 一个目录 + 其中的 `SKILL.md`**。
YAML frontmatter(由 `python-frontmatter` 解析)+ Markdown 正文(→ `prompt`)。

```markdown
---
name: review-pr
description: Review a GitHub pull request
tools: [Read, Grep, Glob, Bash]
execution_mode: fork
parameters:
  pr_number: { type: string, description: PR number, required: true }
---
Review pull request #{{ pr_number }} ...
```

| frontmatter | → 字段 | 默认 |
|-------------|--------|------|
| `name`(必填) | `name` | 无→`load_*` 返回 `None` |
| `description` | `description` | `""` |
| `tools` | `tools` | `["*"]` |
| `model` / `permission_mode` / `system_prompt` | 同名 | `None`/`None`/`""` |
| `execution_mode` | `execution_mode` | `"inline"` |
| `background` | `background` | `False` |
| `parameters` | `parameters` | `{}` |
| 正文 | `prompt` | `""` |

### 2.3 `skills/loader.py`

```python
def load_skill_from_markdown(file_path: str | Path) -> SkillDefinition | None
    # 解析单个 SKILL.md;文件不存在 / frontmatter 缺 name / 解析异常 → None(记日志)

def discover_skills(directory: str | Path) -> dict[str, SkillDefinition]
    # 扫描 <directory>/<skill>/SKILL.md;同名 first-wins(记 warning);目录不存在→{}
    # 同步函数;query() bootstrap 经 asyncio.to_thread 调用以免阻塞事件循环
```

### 2.4 `skills/registry.py` — `SkillRegistry`

| 方法 | 签名 | 说明 |
|------|------|------|
| `register` | `(skill, source="project") -> None` | 记录 skill + source(`config`/`project`/`user`/`plugin`/`builtin`) |
| `unregister` | `(name) -> None` | `KeyError` if unknown;同步清 source |
| `get` | `(name) -> SkillDefinition` | `KeyError` if not found |
| `source_of` | `(name) -> str | None` | 来源标签 |
| `contains` | `(name) -> bool` | |
| `list_all` | `() -> dict` | |
| `list_by_source` | `(source) -> dict` | 按来源过滤 |

### 2.5 `skills/executor.py`

```python
def render_template(template, arguments, declared) -> str
    # 正则 {{ name }} / {{name}}(容忍空格);required 缺失 → ValueError;未声明占位符原样保留

async def execute_skill_inline(skill, arguments, context) -> ToolCallResult
    # run_agent(parent_messages=context.messages) —— 继承父对话历史

async def execute_skill_fork(skill, arguments, context) -> ToolCallResult
    # run_agent(parent_messages=None) —— 全新历史，完全隔离
```
两者都构造临时 `AgentDefinition(agent_type=f"skill-{name}", tools, system_prompt, model,
permission_mode)`,经 `run_agent()` 同步执行并聚合 `assistant_text` 为 `ToolCallResult`。
**inline 不重入当前 turn 循环**(工具是叶子操作)。深度保护复用 `run_agent` 的
`MAX_AGENT_DEPTH`。

### 2.6 `skills/tool.py` — `SkillTool`

```python
@register_tool
class SkillTool(Tool):
    name = "Skill"
    is_concurrency_safe = False
    is_read_only = False
    is_destructive = True        # 子 Agent 可能跑 Write/Edit/Bash → 不被非破坏性 fallback 自动放行

class SkillInput(BaseModel):
    skill: str
    arguments: dict[str, str] = {}
    mode: Literal["inline", "fork"] | None = None   # 覆盖 skill.execution_mode
```
**`call()` 流程**:① 从 `context` 取 per-query `SkillRegistry`(无 → error);
② `registry.get(skill)`(未知 → error);③ `default_skill_permissions().check(name)`:
`deny`→error,`ask`/`allow`→放行;④ `render_template`(缺必填 → error);
⑤ `mode = input.mode or skill.execution_mode` 派发 inline/fork;执行异常 → error。

注册表 plumbing(挂在 `context` 属性 `"__skill_registry__"`):
```python
_set_skill_registry(context, registry) -> None
_get_skill_registry(context) -> SkillRegistry | None
```

### 2.7 `skills/permissions.py`

```python
class SkillPermissionRule(BaseModel):
    pattern: str                 # fnmatch glob: "review:*" / "dangerous-*" / "*"
    action: str = "ask"          # allow | deny | ask
    priority: int = 100
    description: str = ""

class SkillPermissionEngine:
    def __init__(self, rules=None)          # 按 priority 升序;空则补 "*"→ask
    def check(self, skill_name) -> str      # "allow" | "deny" | "ask";首个匹配胜;无匹配→"ask"

def default_skill_permissions() -> SkillPermissionEngine   # 单条 "*"→ask
```

### 2.8 发现与优先级(`query()` bootstrap)

`_run_turns` 启动时建 per-query `SkillRegistry`,经 `asyncio.to_thread(discover_skills, d)`
按下列 **first-wins** 优先级扫描,然后 `_set_skill_registry(tool_context, ...)`:

| 顺序 | 目录 | source |
|------|------|--------|
| 1 | `config.skills_dir` | `config` |
| 2 | `<workspace_root>/.agent/skills/` | `project` |
| 3 | `~/.agent/skills/` | `user` |

### 2.9 CLI

`/skills`(`cli.py:_list_skills`)列出可发现的 skill(name、mode、background、description)。

---

## 3. Plugins API

### 3.1 `plugins/base.py` — `Plugin`(ABC)

```python
class Plugin(ABC):
    name: ClassVar[str]              # 必填，唯一标识
    version: ClassVar[str] = "0.1.0"

    async def on_enable(self) -> None        # 贡献注入之后
    async def on_disable(self) -> None       # 贡献移除之前

    def get_tools(self) -> list[type[Tool]]: return []
    def get_hooks(self) -> list: return []                       # hook 实例(需带 hook_kind)
    def get_agent_definitions(self) -> list[AgentDefinition]: return []
    def get_skills(self) -> list: return []                      # SkillDefinition 实例
```

### 3.2 `plugins/registry.py` — `PluginRegistry`

`GROUP_NAME = "agent_core.plugins"`。**不持有**任何注册表引用 —— `enable()` 由调用方
显式传入,以保证贡献进入 per-query(hook/skill)实例。

| 方法 | 签名 | 说明 |
|------|------|------|
| `discover` | `() -> list[str]` | 扫描 `entry_points(group=...)`;返回**新发现**(未启用)插件名;加载/校验失败记 warning 跳过 |
| `register` | `(plugin_cls) -> None` | 手动注册插件类(需有 `name`) |
| `unregister` | `(name) -> None` | 已启用则 `RuntimeError` |
| `enable` | `async (name, *, tool_registry=None, hook_registry=None, skill_registry=None, agent_registry=None) -> None` | 实例化 + 注入贡献(tool→`tool_registry`,hook→`hook_registry`,agent→全局 `register_agent(source="plugin")`,skill→`skill_registry`)+ `on_enable()`;幂等 |
| `disable` | `async (name, *, ...) -> None` | `on_disable()` + 移除(skill 可移除;tool/agent 注册为追加式,best-effort) |
| `is_enabled` / `enabled_plugins` / `available_plugins` / `get` | — | 查询 |

### 3.3 `plugins/lifecycle.py`

```python
async def safe_enable_plugins(registry, names, **kwargs) -> None   # 逐个 enable，单个失败记 warning 跳过
async def safe_disable_all(registry, **kwargs) -> None
```

### 3.4 集成点(`query/loop.py` bootstrap)

```python
if config.auto_enable_plugins:
    plugin_registry = PluginRegistry()
    plugin_registry.discover()
    await safe_enable_plugins(
        plugin_registry, config.auto_enable_plugins,
        tool_registry=registry, hook_registry=hook_registry,
        skill_registry=skill_registry, agent_registry=_default_agent_registry,
    )
```
位置在 skill bootstrap 之后、`tools_schema` 构建之前(确保 plugin tool 进入 schema)。
hook/skill 注入 **per-query 实例**;tool/agent 沿用全局默认(tool 类无状态)。

---

## 4. 配置与上下文表面

### `AgentConfig`(`models.py`)
| 字段 | 类型 | 默认 | 消费方 |
|------|------|------|--------|
| `hooks` | `list[Any]` | `[]` | `Agent.stream` / `query()` → `copy_with_overrides` |
| `skills_dir` | `str | None` | `None` | skill 发现优先级 1 |
| `auto_enable_plugins` | `list[str]` | `[]` | plugin bootstrap |

### `AgentDefinition`(`agents/definition.py`)
| 字段 | 类型 | 默认 | 消费方 |
|------|------|------|--------|
| `hooks` | `list[Any]` | `[]` | 子 Agent `copy_with_overrides(definition.hooks)` |
| `skills` | `list[str]` | `[]` | 预留(skill 主要经 `SkillTool` 触发) |

### `ToolUseContext`(`tools/base.py`)
新增 `hook_registry: Any = None`;skill 注册表挂在动态属性 `"__skill_registry__"`。

### 公开导出(`agent_core/__init__.py`)
Hooks:`HookRegistry, HookResult, HookContinue, HookBlock, HookModify, StopResult,
PreToolUseHook, PostToolUseHook, StopHook, SubagentStartHook`。
Skills:`SkillDefinition, SkillRegistry, SkillTool`。
Plugins:`Plugin, PluginRegistry`。

---

## 5. 测试

| 文件 | 数量 | 覆盖 |
|------|------|------|
| `tests/unit/test_hooks.py` | ✓ | 结果模型 / `HookRegistry`(缺 `hook_kind` 报错、priority、`copy_with_overrides`、`is_empty`)/ pipeline |
| `tests/unit/test_skills.py` | ✓ | `SkillDefinition` / `SkillRegistry` / loader(SKILL.md、参数)/ `render_template` / `SkillPermissionEngine` |
| `tests/unit/test_skill_tool.py` | 12 | 契约(`is_destructive=True`)/ 无注册表 / 未知 skill / 缺必填参 / inline·fork·mode 派发 / 异常转 error |
| `tests/unit/test_plugins.py` | ✓ | Plugin 基类 / 注册注销 / enable·disable 注入 / entry_points |
| `tests/integration/test_hooks.py` | 6 | PreToolUse block / **modify 重跑 bash_guard** / PostToolUse modify·continue / Stop 终止 + 顺序 |
| `tests/integration/test_skills.py` | 2 | SKILL.md 发现→`Skill` 执行→子 Agent 聚合 / 未知 skill 报错 |
| `tests/integration/test_plugins.py` | 2 | plugin hook 经 query 生效 / 无 `auto_enable_plugins` 不触发 discover |

全套:**824 passed, 10 skipped**(跳过项均为其它 item 的占位与 Windows/ripgrep 环境限制)。

---

## 6. as-built 文件清单

**新建**:`hooks/{__init__,types,registry,pipeline}.py`、
`plugins/{__init__,base,registry,lifecycle}.py`、
`skills/{__init__,definition,loader,registry,executor,tool,permissions}.py`、
`tests/unit/test_{hooks,skills,skill_tool,plugins}.py`、
`tests/integration/test_{hooks,skills,plugins}.py`、
`docs/guides/skills.md`、`docs/api/hooks.md`、`examples/skill_example.py`。

**修改**:`query/loop.py`(hook 管线 + stop + skill/plugin bootstrap)、
`tools/base.py`(`hook_registry` 字段)、`agent.py`(注入 hook_registry)、
`agents/run.py`(子 hook 继承)、`agents/tool.py`(SubagentStart)、
`models.py`(`auto_enable_plugins`)、`skills/registry.py`(source 追踪)、
`__init__.py`(导出)、`cli.py`(`/skills`)。
