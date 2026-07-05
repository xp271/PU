# QQ 机器人：动态工作目录切换功能计划书

> 目标：允许 QQBot 在同一对话中动态切换 `workspace_root`（工作目录），且该能力**仅限 QQBot channel 拥有**、**不修改安全规则引擎**。
> 预估 0.5 天。

---

> **状态**：📋 计划阶段，尚未实现。
>
> ⚠️ **已废弃 (2026-07-05)**：此计划被 `qqbot-shell-commands-plan.md` v2 取代。
> `/workspace` 指令已替换为 `/cd`、`/ls`、`/clear`、`/help`。实现细节参考新计划书。
>
> **审查记录**：
> - v1（初稿）→ v2（修复私有方法访问、`_written_count` 一致性、原子性、reset 缺失等 7 个问题）
> - v2 → v3（2026-07-05：第二轮审查 — 修复 `self._config.workspace_root` 可能为 None 而真实 Agent workspace 非 None 导致 `/workspace` 显示错误的问题；新增 `self._agent_config` 存储合并后的配置）

---

## 0. 背景与动机

当前 architecture 中，`workspace_root` 是一个**启动时固定、运行期不可变**的值：

```
AgentConfig.workspace_root  (启动时从配置文件/QQBotConfig 加载，之后不变)
  → Agent.stream()
    → query(workspace_root=...)
      → _run_turns():
          ① enrich_context(workspace_root)      → <environment> 块
          ② ToolUseContext.workspace_root       → 传给每个工具
          ③ check_workspace(path, context.workspace_root) → 安全边界
```

**问题**：QQBot 用户可能在群聊中说「看看 D 盘那个项目」→ 需要切换到 `D:\other-project`，但 `workspace_root` 改不了，只能重新配置重启。对于交互式 Bot 场景，这个限制很不自然。

**目标体验**：
```
用户: /workspace D:\other-project
Bot:  工作目录已切换至: D:\other-project\n对话历史已重置。
用户: 帮我看看这个项目的结构
Bot:  [在 D:\other-project 下执行 Glob/Read/Bash...]
```

---

## 1. 核心设计思路

### 1.1 一句话概括

**在 `SessionState` 上挂一个 `workspace_override` 字段，QQBot channel 在对话中修改它，Agent 创建时用它覆盖 `config.workspace_root`。安全引擎（`PermissionEngine` + `check_workspace`）一行不改。**

### 1.2 关键洞察：安全引擎与 workspace 是解耦的

| 组件 | 管什么 | 管不管 workspace |
|---|---|---|
| `PermissionEngine` | 工具能不能调用（allow/deny/ask），基于规则匹配链 | ❌ 不管 |
| `check_workspace()` | 工具操作的路径是否在 `context.workspace_root` 之内 | ✅ 只管这个 |

所以「动态切换工作目录」对 `PermissionEngine` 完全透明——只需改变塞进 `ToolUseContext.workspace_root` 的值，`check_workspace()` 自动跟随新边界。

### 1.3 数据流（改造后）

```
SessionState.workspace_override   ← QQBotChannel 可在对话中修改（走 AgentBridge 公开接口）
  ↓
AgentBridge.process():
  effective_workspace = session.workspace_override or config.workspace_root
  effective_config = config.model_copy(update={"workspace_root": effective_workspace})
  Agent(effective_config, session=session)
  ↓
Agent.stream():
  workspace_root = self._config.workspace_root    ← 已是覆盖后的值
  project_hash = compute_project_hash(workspace_root)  ← 自动跟随新目录
  ↓
query(workspace_root=effective_workspace)
  → enrich_context(effective_workspace)    → <environment> 块使用新目录
  → ToolUseContext.workspace_root = effective_workspace
  → check_workspace(path, effective_workspace)  → 安全边界自动跟随
```

> **联动验证点**（`agent.py:145`）：`project_hash = compute_project_hash(self._config.workspace_root)` — 因为传入 Agent 的 config 已经是 `model_copy` 覆盖后的，`project_hash` 自动使用新的 workspace 值，日志文件的归属目录正确，无需额外处理。

### 1.4 安全考量

| 场景 | 行为 |
|---|---|
| `workspace_override = None`（默认） | 走 `config.workspace_root`，行为完全不变 |
| `workspace_override = "D:\tmp"` | 后续所有工具操作被限制在 `D:\tmp` 内 |
| 切换到不存在的路径 | QQBot 端拒绝切换（`os.path.isdir` 检查），session 状态不变 |
| CLI / REPL | 不暴露切换入口，`workspace_override` 始终为 `None` |

**为什么安全**：`workspace_override` 只是换了 `check_workspace()` 的边界值，不是绕过它。切到新目录后，LLM 能访问的范围也随之改变——不能访问旧目录（已被新边界排除）。

**⚠️ 重要前提**：`check_workspace()` 在 `context.workspace_root is None` 时**完全跳过边界检查**（见 `_io.py:102-104`）。因此本功能的安全保证依赖 `config.workspace_root` 被配置为非 None 值。如果 QQBot 的 `workspace_root` 未设置，则无论 override 为何值，工具都没有路径限制。这不是本次改动引入的行为，但部署时必须确保 `workspace_root` 已配置。

---

## 2. 详细设计

### 2.1 `SessionState` 增加 `workspace_override` 字段 + 修复 `clear_history()` 水印

**文件**：`src/agent_core/session/state.py`

两处改动：

**(A) 新增字段** — 在现有字段末尾添加：

```python
@dataclass
class SessionState:
    # ... 现有字段 ...
    workspace_override: str | None = None  # 新增
    """QQBot 可动态设置的工作目录覆盖。

    当非 None 时，Agent 创建会使用此值替代 config.workspace_root。
    CLI/REPL 永远不设置此字段，因此不影响现有行为。
    不持久化 — QQBot 重启后恢复为 config.workspace_root。
    """
```

**(B) 修复 `clear_history()` 的水印不一致**：

```python
def clear_history(self) -> None:
    """Drop conversation history (e.g. a ``/clear`` command). Permission
    memory is left intact."""
    self.messages = []
    self._written_count = 0  # ← 新增：重置持久化水印
```

> **原因**：`clear_history()` 将 `messages` 置空，但之前未重置 `_written_count`。如果 `_written_count > 0`（历史消息已通过 Phase B 持久化），后续 `flush_new_messages` 中的 `_is_compaction` 会因 `len(final_messages) < self._written_count` 而误判为 compaction，写入错误的 checkpoint。虽然 QQBot 场景中 `_writer` 始终为 `None`（`begin_persistence` 只在 CLI REPL 调用），该 bug 当前不触发，但作为数据完整性隐患必须修复。

### 2.2 `AgentBridge` 新增公开 session 访问接口 + `process()` 使用覆盖值

**文件**：`src/agent_core/bridge/agent_bridge.py`

两处改动：

**(A) 新增公开方法 `get_session()`** — 替代 channel 层直接调用私有 `_get_or_create_session`：

```python
def get_session(self, user_id: str, open_kfid: str) -> SessionState:
    """获取或创建指定用户的 SessionState（公开接口）。

    Channel 层可通过此方法访问 session，例如设置 workspace_override。
    """
    sid = self.session_id(user_id, open_kfid)
    return self._get_or_create_session(sid)
```

**(B) `process()` 中读取 workspace 覆盖** — 修改 Agent 创建逻辑：

```python
async def process(
    self, user_id: str, open_kfid: str, content: str, msg_type: str
) -> list[str]:
    sid = self.session_id(user_id, open_kfid)
    session = self._get_or_create_session(sid)
    lock = self._session_locks[sid]

    async with lock:
        prompt = self._build_prompt(content, msg_type)

        # --- 动态 workspace 覆盖 ---
        effective_config = self._base
        if session.workspace_override is not None:
            effective_config = self._base.model_copy(
                update={"workspace_root": session.workspace_override}
            )
        # ---

        agent = Agent(effective_config, session=session)
        # ... 原有逻辑 ...
```

> **说明**：`model_copy(update=...)` 是 Pydantic v2 的浅拷贝方法，创建新 `AgentConfig` 实例，只替换 `workspace_root`，其余字段保持不变。此逻辑对微信客服 channel 透明 — 其 session 的 `workspace_override` 始终为 `None`，走 `self._base` 原值。

### 2.3 `QQBotChannel` — 存储合并后的 AgentConfig + 处理切换指令

**文件**：`src/agent_core/channels/qq_bot/channel.py`

**关键背景**：`workspace_root` 有两个来源——

```
QQBotConfig.workspace_root   ──optional overlay──→ 全局 AgentConfig.workspace_root
```

`QQBotConfig.to_agent_config()` 合并两者：若 `QQBotConfig.workspace_root` 非 None 则覆盖全局值，否则保留全局值。这意味着 **`self._config.workspace_root`（QQBotConfig 字段）可能为 `None`，而实际 Agent 使用的 `workspace_root` 来自全局配置**。

因此必须在 `__init__` 中**显式存储合并后的 `AgentConfig`**，`_handle_workspace_command` 读取真实 workspace 时使用它。

改动分两处：

**(A) `__init__` 存储合并后的 AgentConfig**：

```python
class QQBotChannel:
    def __init__(self, config: QQBotConfig) -> None:
        self._config = config
        self._client: QQBotClient | None = None

        base_agent_config = _load_global_agent_config()
        self._agent_config = config.to_agent_config(base_agent_config)  # ← 新增：存储合并后配置
        from agent_core.bridge.agent_bridge import AgentBridge
        self._bridge = AgentBridge(
            self._agent_config,  # ← 改：用存储的合并配置（原先为内联表达式）
            system_prompt=config.system_prompt,
        )
```

**(B) `handle_message()` 加 `/workspace` 前缀检测 + 新增 `_handle_workspace_command`**：

```python
async def handle_message(
    self, user_id: str, channel_id: str, content: str, msg_type: str,
) -> None:
    # --- workspace 切换指令（在 Agent 处理之前） ---
    if content.startswith("/workspace"):
        await self._handle_workspace_command(user_id, channel_id, content)
        return
    # ---

    # ... 原有消息处理逻辑 ...

async def _handle_workspace_command(
    self, user_id: str, channel_id: str, content: str,
) -> None:
    """处理 /workspace 指令：查看、切换或重置工作目录。"""
    import os

    # 功能开关检查
    if not self._config.allow_workspace_switch:
        await self._send_message(
            user_id, channel_id, "工作目录切换功能未启用。"
        )
        return

    # 通过 AgentBridge 公开接口获取 session
    session = self._bridge.get_session(user_id, channel_id)

    arg = content[len("/workspace"):].strip()

    if not arg:
        # 无参数 → 显示当前工作目录
        current = (
            session.workspace_override
            or self._agent_config.workspace_root    # ← 用合并后的 AgentConfig
            or "未设置（无限制）"
        )
        await self._send_message(
            user_id, channel_id, f"当前工作目录: {current}"
        )
        return

    if arg == "--reset":
        # 重置为配置文件默认值
        session.workspace_override = None
        session.clear_history()
        default = self._agent_config.workspace_root or "未设置"  # ← 同上
        await self._send_message(
            user_id, channel_id,
            f"工作目录已重置为默认: {default}\n对话历史已重置。"
        )
        return

    # 切换目录
    new_path = os.path.abspath(arg)
    if not os.path.isdir(new_path):
        await self._send_message(
            user_id, channel_id, f"目录不存在: {new_path}"
        )
        return

    session.workspace_override = new_path
    session.clear_history()
    await self._send_message(
        user_id, channel_id,
        f"工作目录已切换至: {new_path}\n对话历史已重置。"
    )
```

> **为什么 `self._agent_config.workspace_root` 而不是 `self._config.workspace_root`**：
>
> `self._config` 是 `QQBotConfig` 实例。当用户只在全局 `agent-core.config.json` 中配置了 `workspace_root` 而未在 `qq_bot` 嵌套块中覆盖时：
> - `self._config.workspace_root` → `None`（QQBotConfig 字段默认值）
> - `self._agent_config.workspace_root` → `"C:\PU"`（来自全局配置，经 `to_agent_config` 合并）
>
> Agent 实际使用的是后者。若用前者显示「当前工作目录: 未设置（无限制）」，用户会被误导——明明 Agent 在 `C:\PU` 下运行却说无限制。

#### 并发安全性说明

`_handle_workspace_command` 设置 `workspace_override` 和 `clear_history()` 时**没有拿 `AgentBridge` 内部的 per-session Lock**（该 lock 在 `process()` 的 `async with lock:` 内）。这依赖一个前提：

> **botpy QQ WebSocket 对同一 session 的消息是串行投递的** — 即同一用户的第二条消息只会在第一条的 `handle_message` 返回后才到达。

在此前提下，`/workspace` 指令设置 override 时不可能有并发 `process()` 在读取它。若未来 botpy 或 QQ 协议改为并发投递，需让 `_handle_workspace_command` 也获取 `AgentBridge` 的 per-session lock（需要 bridge 暴露 lock 获取接口）。

#### 关于 `session.clear_history()`

切换工作目录后**强制重置对话历史**，原因：
- 旧对话可能包含对旧目录文件的引用和结论
- LLM 可能基于过期的上下文做出错误判断
- 新目录 = 新的工作上下文，保留旧对话会造成混淆

`clear_history()` 同步重置 `_written_count = 0`（见 §2.1 B 修复），保证 Phase B 持久化水印一致。若未来需要「保留历史切换」，可扩展为 `/workspace D:\new --keep`。

### 2.4 `QQBotConfig` 增加功能开关

**文件**：`src/agent_core/channels/qq_bot/types.py`

```python
class QQBotConfig(BaseModel):
    # ... 现有字段 ...
    allow_workspace_switch: bool = Field(
        default=True,
        description="允许 QQBot 用户在对话中通过 /workspace 切换工作目录",
    )
```

> 该开关确保即使 QQBot channel 代码中存在切换逻辑，也可以通过配置完全关闭，不影响安全。

---

## 3. 影响/修改的文件清单

| 文件 | 变更 | 影响范围 |
|---|---|---|
| `src/agent_core/session/state.py` | (A) `SessionState` 加 `workspace_override: str \| None = None`；(B) `clear_history()` 加 `_written_count = 0` | 所有 session 使用者 |
| `src/agent_core/bridge/agent_bridge.py` | (A) 新增公开方法 `get_session()`；(B) `process()` 中读取 `session.workspace_override` 并覆盖 config | QQBot + 微信客服（微信客服不受影响） |
| `src/agent_core/channels/qq_bot/channel.py` | (A) `__init__` 存储 `self._agent_config`；(B) 新增 `_handle_workspace_command()`；(C) `handle_message()` 加 `/workspace` 前缀检测 | 仅 QQBot |
| `src/agent_core/channels/qq_bot/types.py` | `QQBotConfig` 新增 `allow_workspace_switch: bool = True` | 仅 QQBot 配置 |

| **不需要改的文件** | 原因 |
|---|---|
| `src/agent_core/permissions/engine.py` | 安全引擎与 workspace 无关 |
| `src/agent_core/permissions/rules.py` | 同上 |
| `src/agent_core/tools/_io.py` | `check_workspace()` 只消费 `context.workspace_root`，该值已在 ToolUseContext 创建时被覆盖 |
| `src/agent_core/tools/bash.py` 等工具文件 | 同上 |
| `src/agent_core/agent.py` | 透传 `config.workspace_root`（已被覆盖），`project_hash` 自动跟随新值（已验证 `agent.py:145` 路径） |
| `src/agent_core/query/loop.py` | `workspace_root` 来自函数参数，调用者已传覆盖后的值 |
| `src/agent_core/query/context.py` | `enrich_context(workspace_root)` — 传入值已被覆盖 |
| `src/agent_core/cli/_main.py` | CLI 不涉及，`workspace_override` 始终为 None |

**总计**：4 个文件，约 55 行实际代码变更（含公开接口、辅助方法和存储合并配置）。

---

## 4. 安全分析

### 4.1 威胁模型

| 威胁 | 分析 |
|---|---|
| QQBot 用户切换到敏感目录（如 `C:\Windows`） | ✅ 允许。LLM 仍需通过 `check_workspace()` 边界检查，只是边界变了。若配置允许切换，这是预期行为。可通过 `allow_workspace_switch=False` 关闭。 |
| 非 QQBot channel 意外获得切换能力 | ✅ 不会。切换入口仅在 `QQBotChannel._handle_workspace_command()` 中，CLI/微信客服不包含此逻辑。`SessionState.workspace_override` 字段存在但无人写入，始终为 None。 |
| 切换后访问旧 workspace 的文件 | ✅ 被阻止。`check_workspace()` 检查新路径是否在新 workspace 内，旧路径不在范围内。 |
| 多个 QQBot 用户共享 session 导致互相干扰 | ✅ 不会。每个用户有独立 session（`session_id = f"{open_kfid}:{touser}"`），`workspace_override` 是 per-session 的。 |
| 恶意用户通过构造消息绕过 `/workspace` 前缀检测 | ⚠️ 仅精确匹配 `/workspace` 前缀。LLM 自然语言「切换到某某目录」不会被识别为切换指令（但 LLM 可正常执行 `cd` 命令来改变 Bash 的 cwd，不影响 workspace 边界）。 |
| 并发投递导致竞态 | ✅ 依赖 botpy WebSocket 对同一用户消息串行投递的前提（见 §2.3 说明）。若未来协议变更，需让 workspace 操作也获取 per-session lock。 |

### 4.2 不变性保证

- `PermissionEngine` 的规则匹配链：**零变更**
- `check_workspace()` 函数签名和逻辑：**零变更**
- 默认行为（`workspace_override = None`）：**零变更**（`model_copy` 创建新 config 但所有值相同）
- CLI / REPL 行为：**零变更**
- 微信客服 channel 行为：**零变更**

---

## 5. 边界情况处理

| 场景 | 行为 |
|---|---|
| `/workspace`（无参数） | 显示当前工作目录路径 |
| `/workspace --reset` | 清除 override，恢复为 `config.workspace_root` |
| `/workspace D:\not-exist` | 返回 "目录不存在" 错误，session 状态不变 |
| `/workspace` 输入相对路径 | `os.path.abspath()` 转为绝对路径 |
| 切换后对话历史 | 自动 `session.clear_history()`（同时重置 `_written_count`） |
| `allow_workspace_switch=False` | 返回 "功能未启用"，session 状态不变 |
| 切换后第一条消息 | `enrich_context()` 使用新 workspace 生成 `<environment>` 块 |
| QQBot 重启后 | `workspace_override` 不持久化（SessionState 是内存对象），恢复为 `config.workspace_root` |
| `config.workspace_root = None` 时切换 | 切换本身正常，但 `check_workspace()` 在 `None` 下跳过所有边界检查（这是 `_io.py` 的既有行为，非本次引入） |

---

## 6. 测试计划

| 测试 | 验证点 |
|---|---|
| 单元测试：`SessionState.workspace_override` 默认值 | `None` |
| 单元测试：`clear_history()` 重置 `_written_count` | `_written_count == 0` after clear |
| 单元测试：`AgentBridge.get_session()` 公开接口 | 创建/获取 session，ID 正确 |
| 单元测试：`AgentBridge.process()` 使用 `workspace_override` | 当 `session.workspace_override = "D:\\test"` 时，传入 Agent 的 config 的 `workspace_root == "D:\\test"` |
| 单元测试：`AgentBridge.process()` 无 override 时不修改 config | 当 `session.workspace_override = None` 时，传入 Agent 的 config 与 `self._base` 相同（is 或 eq） |
| 单元测试：`QQBotChannel._handle_workspace_command()` 切换 | session.workspace_override 被设置，history 被清空 |
| 单元测试：`QQBotChannel._handle_workspace_command()` 目录不存在 | session 状态不变 |
| 单元测试：`QQBotChannel._handle_workspace_command()` --reset | override 重置为 None |
| 单元测试：`QQBotChannel._handle_workspace_command()` 开关关闭 | 返回错误消息，session 不变 |
| 单元测试：`/workspace` 无参数显示合并后 workspace | QQBotConfig.workspace_root = None，全局 AgentConfig.workspace_root = "C:\\PU" → 回复消息包含 "C:\\PU"（而非 "未设置"） |
| 集成测试：完整 `/workspace` → Agent 调用 | Agent 在切换后的目录下执行工具操作 |
| 回归测试：CLI / REPL | 正常运行，`workspace_override` 始终为 None |
| 回归测试：微信客服 channel | 正常运行，`workspace_override` 始终为 None |

---

## 7. 实现步骤（按顺序）

| # | 文件 | 变更 |
|---|---|---|
| 1 | `session/state.py` | (A) `SessionState` 加 `workspace_override` 字段；(B) `clear_history()` 加 `_written_count = 0` |
| 2 | `bridge/agent_bridge.py` | (A) 新增 `get_session()` 公开方法；(B) `process()` 中读取 `workspace_override` 并覆盖 config |
| 3 | `channels/qq_bot/types.py` | `QQBotConfig` 加 `allow_workspace_switch` 开关 |
| 4 | `channels/qq_bot/channel.py` | (A) `__init__` 存储 `self._agent_config = config.to_agent_config(...)`；(B) 新增 `_handle_workspace_command()`；(C) `handle_message()` 加前缀检测 |
| 5 | 测试 | 补单元测试（含合并配置场景）+ 手动集成测试 |
| 6 | 文档 | 更新 QQBot 用户文档（如有） |

---

## 8. 与现有功能的正交性

| 现有功能 | 与 workspace 切换的关系 |
|---|---|
| 权限引擎（`PermissionEngine`） | 正交 — 引擎不管路径 |
| 会话权限记忆（`SessionPermissionStore`） | 正交 — 切换 workspace 不清除权限记忆 |
| 对话历史持久化（Phase B） | 正交 — `workspace_override` 不持久化（重启丢失）；`clear_history()` 已修复 `_written_count` 水印，compaction 检测不再误触发 |
| 上下文压缩（compression） | 正交 — 压缩逻辑不涉及 workspace |
| `project_hash`（`agent.py:145`） | 自动跟随 — `self._config.workspace_root` 已被覆盖，`compute_project_hash` 使用新值 |
| `<environment>` 块（`context.py`） | 自动跟随 — `enrich_context(workspace_root)` 传入覆盖后的值 |
| MCP 工具 | 正交 — MCP 工具不走 `check_workspace()` |
| 子 Agent（AgentTool） | 子 Agent 使用父 config，自动继承覆盖后的 workspace |
| 微信客服 channel | 不受影响 — `session.workspace_override` 始终为 None |
| CLI REPL | 不受影响 — `session.workspace_override` 始终为 None |

---

## 9. 演进路线（本次不实现）

1. **自然语言切换**：不依赖 `/workspace` 指令，LLM 可以在对话中理解「切到某某目录」并自动调用一个 `switch_workspace` 工具。需要新增一个内部 tool，且需考虑安全审批。
2. **workspace 白名单**：`QQBotConfig` 中配置允许切换的目录列表，拒绝切换到白名单之外的路径。
3. **持久化 workspace_override**：跨 Bot 重启记住上次切换的目录（需配合 Phase B session 持久化）。
4. **多 workspace 并行**：一个 session 内维护多个 workspace，LLM 可同时操作多个项目（复杂度高，暂不需要）。
5. **保留历史切换**：`/workspace D:\new --keep` 选项，切换目录但不清除对话历史（需要 LLM 能正确处理跨目录上下文）。
