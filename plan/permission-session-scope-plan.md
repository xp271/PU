# 权限系统增强：会话级记住批准（v2，参考 cc-haha 架构重写）

> 目标：`PermissionCallback` 从 `Awaitable[bool]` 扩展为支持 `y(一次)` / `n(一次)` / `s(本会话允许)` / `r(本会话拒绝)`。
> **核心思路（借鉴 cc-haha）**：会话记忆不是一个独立的数据结构，而是"**带 `source="session"` 来源标签的 `PermissionRule`**"，复用现有规则匹配链；这些规则存放在一个**比 `PermissionEngine` 长寿的容器**里，由 REPL 层创建并向下注入。
> 预估 1~1.5 天。

---

> **状态** (2026-06-25)：✅ **已实现**。`PermissionOutcome` / `SessionPermissionStore` / 引擎注入 / CLI `[y/s/n/r]` 全部落地，挂载于 `SessionState.permission_store`（见 [session-state-and-history-plan.md](./session-state-and-history-plan.md) Phase A）。测试：`tests/unit/test_permission_store.py`。
> v1 计划已废弃，原因见 §0。

---

## 0. 为什么重写 v1

v1 计划（新建 `SessionPermissionMemory` 类、引擎内部 `allow_set/deny_set`、`check()` 加 step 0）存在 4 个问题：

1. **致命 — 生命周期错配**：`PermissionEngine` 在 `loop.py:212` `run_query()` 内部每个 prompt 重建一次。把记忆塞进引擎实例 → 下一个 REPL prompt 记忆即丢失，**"本会话记住"的目标根本无法达成**。
2. **写回点拿不到状态**：`_ask_callback_or_default()` 是模块级自由函数（`engine.py:228`），无 `self`，无法写入引擎内部集合。
3. **step 0「覆盖规则」语义不当**：会话允许会盖过显式 deny，安全隐患。
4. **粒度太粗**：只按 `tool_name`，content 粒度推迟到"后续扩展"。

cc-haha 的做法解决了全部 4 点（见 §1），故据此重写。

---

## 1. cc-haha 架构参考（关键结论）

| cc-haha 机制 | 文件 | 借鉴点 |
|---|---|---|
| `ToolPermissionContext` 持有 `alwaysAllowRules/alwaysDenyRules/alwaysAskRules`，**每个按 `source` 分桶**（`session` 只是来源之一） | `types/permissions.ts:427` | 会话记忆 = 规则的一个来源，不是独立类型 |
| `PermissionUpdate{type:'addRules', destination:'session', behavior, rules}` 是一等数据对象 | `types/permissions.ts:98` | "记住"是数据，不是让引擎偷看枚举改内部集合 |
| `applyPermissionUpdate` **不可变**返回新 context | `PermissionUpdate.ts:55` | 写回逻辑集中、可测 |
| `supportsPersistence()` 中 `session` 返回 `false` → 纯内存 | `PermissionUpdate.ts:208` | **session 内存 vs 磁盘持久化正交**，本功能完全不碰盘 |
| context 存活在长生命周期 AppState，跨整个 REPL | `state/AppStateStore.ts` | 记忆容器必须比 per-query 引擎长寿 → 由 REPL 注入 |

---

## 2. 设计

### 2.1 架构总览

```
REPL 启动（cli._repl，循环外）
  → 创建 1 个 SessionPermissionStore（长寿）
  → 循环内每个 prompt:
       Agent(config, session_store=store)         # 注入
         → query(... session_store=store)
           → run_query(... session_store=store)
             → PermissionEngine.from_config(..., session_store=store)   # 引擎只“借用”，不拥有
               → check():
                   规则匹配链 = sorted(builtin_rules + store.rules())   # session 规则就是规则
                   ...匹配命中 → 直通/直拒
                   ...走到 ask → callback → PermissionOutcome
                       outcome 为 ALLOW_SESSION/DENY_SESSION
                         → store.add_allow/add_deny(...)   # 写回长寿容器
```

引擎每个 prompt 仍可自由重建——但它持有的 `store` 是同一个，所以记忆跨 prompt 存活。

### 2.2 `PermissionRule` 增加 `source` 字段（rules.py）

```python
PermissionSource = Literal["builtin", "config", "session"]

class PermissionRule(BaseModel):
    tool_pattern: str
    action: PermissionAction           # allow | deny | ask
    priority: int = 0
    path_constraint: str | None = None
    rule_content: str | None = None    # 新增：预留 content 粒度（如 Bash 的 git*）
    source: PermissionSource = "builtin"  # 新增
    description: str = ""
```

> `rule_content` 字段先建好但 CLI v1 暂不填（见 §6 演进）。`default_rules()` 全部显式标 `source="builtin"`。

### 2.3 `SessionPermissionStore`（新增到 permissions/，长寿容器）

替代 v1 那个错位的 `SessionPermissionMemory`。它**只装 session 来源的规则**：

```python
class SessionPermissionStore:
    """会话级权限规则容器。由 REPL 创建并向下注入，生命周期 = 整个交互会话。
    仅驻留内存，不落盘（落盘是独立的 Item 10）。"""

    def __init__(self) -> None:
        self._rules: list[PermissionRule] = []

    def rules(self) -> list[PermissionRule]:
        return list(self._rules)

    def add_allow(self, tool_name: str, rule_content: str | None = None) -> None:
        self._add("allow", tool_name, rule_content)

    def add_deny(self, tool_name: str, rule_content: str | None = None) -> None:
        self._add("deny", tool_name, rule_content)

    def _add(self, action, tool_name, rule_content) -> None:
        # 去重：同 (tool, content, action) 不重复加
        # session-deny priority=10，session-allow priority=20（见 §2.5 优先级）
        priority = 10 if action == "deny" else 20
        rule = PermissionRule(
            tool_pattern=tool_name, action=action, rule_content=rule_content,
            priority=priority, source="session",
            description=f"Session {action} for {tool_name}",
        )
        if rule.model_dump() not in [r.model_dump() for r in self._rules]:
            self._rules.append(rule)

    def clear(self) -> None:
        self._rules.clear()
```

### 2.4 `PermissionOutcome` 枚举（新增到 engine.py）

```python
class PermissionOutcome(str, Enum):
    ALLOW_ONCE = "allow_once"        # y
    DENY_ONCE = "deny_once"          # n
    ALLOW_SESSION = "allow_session"  # s
    DENY_SESSION = "deny_session"    # r
```

`PermissionCallback` 新签名：`Callable[[str, dict], Awaitable[PermissionOutcome | bool]]`
（保留 `bool` 以向后兼容，见 §4）。

### 2.5 规则优先级（解决 v1 的「覆盖规则」隐患）

`_rule_sort_key` 当前为 `(priority, fmt)`，lower = 先匹配。约定：

| 规则 | priority | 说明 |
|---|---|---|
| session-deny | 10 | 用户显式拒绝，强 |
| session-allow | 20 | 用户显式允许，强于内置 ask/mode |
| builtin/config | 100 | 现状不变 |

> **不放在 step 0**：session 规则作为普通规则参与 §2.6 的规则匹配（step 1–4），靠 priority 数字定优先级，而非凌驾于所有规则之上。
> **前向兼容注记**：若将来新增「硬性 deny 内置规则」（当前没有），应给它 `priority < 10`，以免被 session-allow 盖过。已在 `rules.py` 注释标注。

### 2.6 `PermissionEngine` 变更（engine.py）

```python
def __init__(self, rules=None, mode="ask", session_store: SessionPermissionStore | None = None):
    ...
    self._session_store = session_store   # 借用，不拥有

@classmethod
def from_config(cls, permission_mode="ask", tools=None, session_store=None):
    return cls(mode=permission_mode, session_store=session_store)

async def check(self, tool_name, tool_input, callback, tool_is_read_only=False, tool_is_destructive=False):
    # 规则匹配链：内置规则 + session 规则一起排序匹配
    effective_rules = self._rules
    if self._session_store is not None:
        effective_rules = sorted(self._rules + self._session_store.rules(), key=_rule_sort_key)
    decision = _match_rules(tool_name, tool_input, effective_rules)
    if decision is not None:
        return decision

    # step 5 mode / step 6 read-only fallback ... 不变

    # step 7: ask → callback，并把 outcome 写回 store（在 check() 内，能访问 self）
    return await self._ask(tool_name, tool_input, callback, tool_is_destructive)

async def _ask(self, tool_name, tool_input, callback, tool_is_destructive):
    if callback is not None:
        try:
            raw = await callback(tool_name, tool_input)
        except BaseException:          # 含 KeyboardInterrupt(Ctrl+C) → deny once
            return PermissionDecision(allowed=False, reason="Cancelled / callback error.", source="callback")
        outcome = _coerce_outcome(raw)  # bool → ALLOW_ONCE/DENY_ONCE
        if outcome in (PermissionOutcome.ALLOW_SESSION, PermissionOutcome.DENY_SESSION) and self._session_store is not None:
            (self._session_store.add_allow if outcome == PermissionOutcome.ALLOW_SESSION
             else self._session_store.add_deny)(tool_name)
        allowed = outcome in (PermissionOutcome.ALLOW_ONCE, PermissionOutcome.ALLOW_SESSION)
        return PermissionDecision(allowed=allowed,
                                  reason="User approved." if allowed else "User denied.",
                                  source="callback")
    # 无 callback：destructive→deny，否则 allow（不变）
    ...
```

> 把 `_ask_callback_or_default` 由模块级自由函数改为方法 `self._ask`，**修掉 v1 拿不到 `self._session_store` 的 bug**。`except BaseException` **修掉 v1 `except Exception` 漏接 KeyboardInterrupt 的 bug**。

### 2.7 CLI 适配（cli.py:251）

```python
async def _permission_callback(tool_name, tool_input) -> PermissionOutcome:
    summary = _summarise(tool_input)
    prompt = (f"\n[permission] Allow {tool_name}? {summary}\n"
              f"  [y]=allow once  [s]=allow this session  [n]=deny once  [r]=deny this session: ")
    ch = (await asyncio.to_thread(input, prompt)).strip().lower()
    if ch.startswith("s"): return PermissionOutcome.ALLOW_SESSION
    if ch.startswith("r"): return PermissionOutcome.DENY_SESSION
    if ch.startswith("y"): return PermissionOutcome.ALLOW_ONCE
    return PermissionOutcome.DENY_ONCE
```

`_repl()` 中**循环外**创建 store，注入每个 Agent：

```python
async def _repl(config):
    session_store = SessionPermissionStore()      # 一个会话一个
    while True:
        ...
        agent = Agent(config, session_store=session_store)
        agent.on_permission_check(_permission_callback)
        ...
```

---

## 3. 影响/修改的文件

| 文件 | 变更 |
|---|---|
| `src/agent_core/permissions/rules.py` | `PermissionRule` 增 `source`、`rule_content`；`default_rules()` 标 `source="builtin"`；优先级注释 |
| `src/agent_core/permissions/store.py`（新建） | `SessionPermissionStore` |
| `src/agent_core/permissions/engine.py` | `PermissionOutcome` 枚举；`__init__`/`from_config` 接 `session_store`；`check()` 合并 session 规则匹配；`_ask_callback_or_default` 改为方法 `_ask`（写回 store + `BaseException` + bool 兼容）；`PermissionCallback` 签名更新 |
| `src/agent_core/permissions/__init__.py` | 导出 `PermissionOutcome`、`SessionPermissionStore` |
| `src/agent_core/agent.py` | `Agent.__init__(config, session_store=None)`；`stream()` 透传给 `query`；`PermissionCallback` 类型对齐 |
| `src/agent_core/query/loop.py` | `query`/`run_query` 增 `session_store` 参数；`PermissionEngine.from_config(..., session_store=...)`（loop.py:212）；`PermissionCallback`（当前 `=Any`）类型注释更新 |
| `src/agent_core/cli.py` | `_repl` 循环外建 store 并注入；`_permission_callback` 返回 `PermissionOutcome` |
| `src/agent_core/agents/run.py` | 子 Agent 默认 `session_store=None`（见 §5）；`_child_perm_cb` 注释说明 outcome 在子 Agent 边界的行为 |

> 注：`PermissionCallback` 当前有 3 处定义（`agent.py:14`、`engine.py:23`、`loop.py:53` 为 `=Any`）。本次顺手统一为从 `engine.py` 单一来源 re-export，消除漂移。

---

## 4. 向后兼容

`_coerce_outcome(raw)`：

```python
def _coerce_outcome(raw) -> PermissionOutcome:
    if isinstance(raw, PermissionOutcome): return raw
    if isinstance(raw, bool):
        return PermissionOutcome.ALLOW_ONCE if raw else PermissionOutcome.DENY_ONCE
    # 兜底：未知类型按真值
    return PermissionOutcome.ALLOW_ONCE if raw else PermissionOutcome.DENY_ONCE
```

现有所有回调（`cli._permission_callback`、`run._child_perm_cb`）今天都返回 `bool`，改造后仍可用，只是不触发 session 记忆。

---

## 5. 子 Agent 行为（明确化，不再含糊）

- `run.py` 创建子 Agent 时**默认 `session_store=None`** → 子 Agent **不继承也不共享**父会话记忆（与 v1 结论一致，但这次有明确实现点）。
- 注意现有**双层引擎**结构：`run.py:174` `_child_perm_cb` 内部跑 `child_engine.check()` 返回 `decision.allowed`（bool），再作为 callback 喂给 `run_query` 内层引擎（`loop.py:648`）。因 `_child_perm_cb` 把 outcome 压扁成 bool，**即使将来给子 Agent 注入 store，session outcome 也会在子 Agent 边界丢失**——若要支持子 Agent 会话记忆，需让 `_child_perm_cb` 透传 outcome 而非 bool。本次不做，仅在代码注释标注。
- 前向扩展：`run_query(... session_store=parent_store)` 即可让子 Agent 继承父记忆。

---

## 6. 演进路线（本次不实现，但数据模型已预留）

1. **content 粒度**：`rule_content` 字段已在 `PermissionRule`/`store.add_*` 就位。后续 CLI 可提供"允许 `Bash(git*)`"选项，`_match_rules` 增加对 `rule_content` 的匹配（如 Bash command 前缀）。
2. **跨会话落盘（Item 10）**：与本功能正交。届时新增 `source="localSettings"` 等规则来源 + 一个 `supports_persistence(source)` 判断，把 store 的规则序列化到 `~/.agent/permissions.json`。**匹配逻辑零改动**——这正是 cc-haha「session 只是一个来源标签」设计的回报。
3. **`/permissions` 命令**：查看/清空当前会话已记住的规则（`store.rules()` / `store.clear()`）。

---

## 7. 测试计划

| 文件 | 测试项 |
|---|---|
| `tests/unit/test_permission_store.py`（新建） | `add_allow/add_deny/rules/clear`；重复 add 幂等；session-deny priority < session-allow |
| `tests/unit/test_permissions.py`（修改/新增） | `PermissionOutcome` 枚举完整；`_coerce_outcome` bool→outcome |
| 同上 | engine：注入含 session-allow 规则的 store → 命中 `allowed=True, source="rule"` |
| 同上 | engine：session-deny 优先于 builtin allow（如对 Read 加 session-deny → 拒绝） |
| 同上 | engine：callback 返回 `ALLOW_SESSION` → store 被写入 → **同一 store、新建第二个 engine 实例**再 check 同工具 → 直通（**这条专门回归 v1 的生命周期 bug**） |
| 同上 | engine：callback 抛 `KeyboardInterrupt` → `allowed=False`（回归 v1 `except Exception` 漏接） |
| `tests/unit/test_permissions.py` | 向后兼容：callback 返回 `True`/`False` 仍工作，不写 store |

---

## 8. 实现步骤（按顺序）

1. **rules.py**：`PermissionRule` 加 `source`/`rule_content`；`default_rules()` 标来源；优先级注释。
2. **store.py**：新建 `SessionPermissionStore`。
3. **engine.py**：`PermissionOutcome`；`__init__`/`from_config` 接 `session_store`；`check()` 合并 session 规则；`_ask`（方法化 + 写回 + `BaseException` + `_coerce_outcome`）。
4. **注入链**：`agent.py` → `query/loop.py` 透传 `session_store`；`from_config` 传入（loop.py:212）。
5. **cli.py**：`_repl` 循环外建 store；`_permission_callback` 返回 outcome。
6. **导出统一**：`__init__.py` 导出新符号；`PermissionCallback` 单一来源 re-export。
7. **run.py**：子 Agent `session_store=None` + 注释。
8. **测试**：新建 + 修改，全局回归。

---

## 9. 与 v1 的差异速查

| 维度 | v1（废弃） | v2（本计划） |
|---|---|---|
| 记忆载体 | 引擎内部 `allow_set/deny_set`（随引擎每 prompt 销毁） | `SessionPermissionStore`，REPL 注入，跨 prompt 存活 |
| 记忆形态 | 独立数据结构 + step 0 旁路 | `source="session"` 的普通 `PermissionRule`，走规则匹配 |
| 写回点 | 模块级函数（拿不到状态，bug） | `check()` 内方法 `_ask`（能访问 `self._session_store`） |
| Ctrl+C | 声称 deny 但 `except Exception` 漏接 | `except BaseException` 真正 deny once |
| 优先级 | step 0 覆盖一切规则（隐患） | priority 数字定序，不凌驾显式规则 |
| 粒度 | 仅 tool_name | `rule_content` 字段已预留 |
| 落盘耦合 | 与持久化纠缠不清 | 完全正交，按来源标签，Item 10 零改匹配逻辑 |
