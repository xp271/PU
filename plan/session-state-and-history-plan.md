# 会话运行时层 & 对话历史（SessionState）实现计划

> 目标：补齐"**同一交互会话内、跨 prompt 的对话历史延续**"这一缺失层，并把它与会话权限记忆收拢到**同一个长寿运行时容器 `SessionState`** 中；为 Item 10 的落盘/恢复预留对接面。
> 参考实现：cc-haha（`src/state/AppStateStore.ts`、`src/utils/conversationRecovery.ts`、`src/utils/sessionStorage.ts`）。
> 预估：Phase A 1~1.5 天；Phase B（落盘/恢复 = Item 10）单独 2 天。

---

> **状态** (2026-06-25)：**Phase A ✅ 已实现**（`SessionState` + 跨 prompt 历史 + `DoneMessage.final_messages` 回吐 + Agent/CLI 注入）；**Phase B ⬜ 待实现**（= Item 10 落盘/恢复）。测试：`tests/unit/test_session_state.py`、`tests/integration/test_history_continuity.py`。
> 关联：[permission-session-scope-plan.md](./permission-session-scope-plan.md)（会话权限记忆，已挂载到本计划的 `SessionState.permission_store`）。

---

## 0. 问题定位：对话历史有三层，缺的是中间层

| 层 | 含义 | 现状 | 归属 |
|---|---|---|---|
| **层 1** 单 prompt 内多轮 | 一次 `run_query` 内 turn loop 累积 `messages` | ✅ 已实现（`loop.py:172–435`） | 现有 |
| **层 2** 同会话跨 prompt（内存） | REPL 里第 2 句能看到第 1 句 | ❌ **缺失** | 本计划 Phase A |
| **层 3** 跨会话落盘 + 恢复 | 关机重开继续聊 | ⬜ 未实现 | Item 10 / 本计划 Phase B |

**层 2 缺失的根因（两处）**：

1. **历史进不去**：`cli._repl`（`cli.py:259`）每个 prompt `agent = Agent(config)` 新建，`Agent.stream → query()` 调用时**不传 `initial_messages`**（`agent.py:62`）。
2. **历史出不来**：`run_query`（`loop.py:157`）内部累积 `messages`，但只 yield `AgentMessage`（assistant_text / tool_use / done…），`DoneMessage`（`loop.py:444`）**不携带最终 `messages` 列表** → 调用方无从拿到本轮产生的历史去喂给下一轮。

> 注：管道其实备好了一半 —— `run_query` 有 `initial_messages` 入参（`loop.py:69`），fork/子 Agent 已在用（`run.py` 传 `parent_messages`）。只差一个"长寿持有者"把历史在 prompt 之间接力。

这与 [会话权限记忆] 是**同一个病**：都缺一个比 per-prompt 引擎/Agent 更长寿的"会话运行时层"。本计划一次性补上，两者共用。

---

## 1. cc-haha 架构参考（提炼，非照搬）

cc-haha 是 React TUI，重量级，但其会话历史的**架构骨架**值得借鉴，复杂度按需裁剪：

| cc-haha 机制 | 文件 | 提炼出的原则 | 我们怎么裁剪 |
|---|---|---|---|
| `messages: Message[]` 存在长寿的 AppState（React store），整个 REPL 生命周期持有，每轮 append | `AppStateStore.ts:53` | 历史的宿主是**长寿会话容器**，不是 per-query 引擎 | → `SessionState.messages`，REPL 持有 |
| 落盘为 **JSONL**，每条 `{uuid, parentUuid, sessionId, timestamp, isSidechain, ...message}`，**append-only** | `sessionStorage.ts:1024+` | 增量追加，不整文件重写；`parentUuid` 串成链支持分叉/fork | Phase B 采用同构 JSONL；`parentUuid` 链可先简化为线性 `parentUuid` |
| 子 Agent transcript 进 `{sessionId}/subagents/` 子目录（sidechain） | `sessionStorage.ts:252` | 主会话与子 Agent 历史**物理分流** | 复用现有 `run.py` 的 sidechain 目录（已存在） |
| 恢复 = 读 JSONL → 按 `parentUuid` chain-walk 到 leaf → 过滤未闭合 tool_use / 孤儿 thinking → 反序列化 → 检测 mid-turn 中断 | `conversationRecovery.ts:154/272/456` | 恢复必须**清洗**：丢弃未配对 tool_use、补未完成轮次 | Phase B 实现 `filter_unresolved_tool_uses` 等价清洗 |

**关键提炼**：cc-haha 的历史和我们的"权限记忆"走的是**同一个设计模式**——长寿容器持有可变状态 + 向下注入 + (可选)落盘按需序列化。所以两者应共用一个 `SessionState`。

---

## 2. 设计

### 2.1 `SessionState`：统一的会话运行时容器（新增）

```python
# src/agent_core/session/state.py（新建包 session/）
@dataclass
class SessionState:
    """单次交互会话的运行时状态。生命周期 = 整个 REPL（或一次 SDK 会话）。
    由顶层创建并向下注入；引擎/Agent 每个 prompt 可重建，但都借用同一个 SessionState。"""

    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    messages: list[dict[str, Any]] = field(default_factory=list)   # 层 2：跨 prompt 历史
    permission_store: SessionPermissionStore = field(default_factory=SessionPermissionStore)  # 会话权限记忆

    # 层 3 预留（Phase B 填充）
    transcript_path: str | None = None

    def extend_history(self, new_messages: list[dict[str, Any]]) -> None:
        """一轮结束后，把本轮新产生的消息接到历史尾部。"""
        self.messages.extend(new_messages)

    def snapshot_for_next_turn(self) -> list[dict[str, Any]]:
        """作为下一个 prompt 的 initial_messages。"""
        return list(self.messages)
```

> `SessionState` 把 [permission-session-scope-plan] 里的 `SessionPermissionStore` 作为一个字段持有，二者注入链合并为一条，避免重复打补丁。

### 2.2 让历史"出得来"：`run_query` 回吐最终消息

`DoneMessage` 增加可选字段，把本轮累计的 `messages` 交还调用方：

```python
# models 中 DoneMessage
class DoneMessage(...):
    stop_reason: StopReason
    total_tokens: int
    final_messages: list[dict[str, Any]] | None = None   # 新增

# loop.py 各 yield DoneMessage 处（如 loop.py:444 及正常结束分支）：
yield DoneMessage(stop_reason=..., total_tokens=total_tokens, final_messages=messages)
```

> 选 `DoneMessage` 携带而非新增返回值，是因为 `query()` 是 async generator，没有干净的 return 通道；`done` 是天然的终结锚点。

### 2.3 让历史"进得去"：注入链贯通

```
cli._repl（循环外）
  session = SessionState()                         # 一个会话一个
  while prompt:
    agent = Agent(config, session=session)         # 注入
      Agent.stream(prompt):
        query(... initial_messages=session.snapshot_for_next_turn(),
                  session_store=session.permission_store)
      # 消费流：遇到 DoneMessage 时
      session.messages = done.final_messages        # 用本轮完整历史覆盖（含刚追加的 user+assistant+tool_result）
```

需改的签名：

| 符号 | 变更 |
|---|---|
| `Agent.__init__` | `(config, session: SessionState | None = None)` |
| `Agent.stream` | 调 `query()` 时传 `initial_messages=session.snapshot...`、`session_store=session.permission_store`；流结束时把 `DoneMessage.final_messages` 写回 `session.messages` |
| `query` / `run_query` | 已有 `initial_messages`（`loop.py:69`）；新增 `session_store` 透传（见权限计划）；`DoneMessage` 带 `final_messages` |
| `cli._repl` | 循环外建 `SessionState`，每个 Agent 注入同一个 |

> **历史合并语义**：`run_query` 用 `initial_messages` 起步（`loop.py:172–179`），轮转中 append 新消息。结束时 `final_messages` = 旧历史 + 本轮新增。REPL 直接 `session.messages = final_messages`（覆盖式，因为它本就包含旧历史），避免重复拼接。

### 2.4 子 Agent 的历史（沿用现有，不改语义）

`run.py` 已把 `parent_messages` 作为子 Agent 的 `initial_messages`，并把子 Agent transcript 写到 sidechain 文件（`run.py:185`）。**本计划不改子 Agent 历史语义**，只是父会话的 `messages` 现在由 `SessionState` 持有后，`run.py` 取父历史的来源从"临时变量"变为"`session.messages`"。

### 2.5 上下文压缩的交互

已有压缩逻辑（`loop.py:296–322` `hard_truncate`）作用于单轮内的 `messages`。跨 prompt 后，`session.messages` 会持续增长 → 下一轮 `initial_messages` 变长 → 触发同样的压缩路径。**无需新增逻辑**，但需注意：`final_messages` 应回吐**压缩后**的 messages（即轮末的 `messages` 变量），这样历史不会无界膨胀。已在 §2.2 自然满足（回吐的是轮末 `messages`）。

---

## 3. Phase B：落盘 & 恢复（= Item 10，单独里程碑）

> 与 Phase A 解耦。Phase A 完成后，`SessionState.messages` 已是单一事实源，落盘只是把它增量序列化。

### 3.1 存储格式（同构 cc-haha JSONL）

- 路径：`~/.agent/projects/{project_hash}/{session_id}.jsonl`（Item 10 已定，`todolist:2495`）。
- 子 Agent：`~/.agent/projects/{project_hash}/{session_id}/{agent_id}.jsonl`（复用现有 sidechain，`run.py:185`）。
- 每行一条 entry：

```json
{"uuid": "...", "parentUuid": "... | null", "sessionId": "...",
 "timestamp": "ISO8601", "isSidechain": false, "message": { ...Anthropic content-block... }}
```

- **append-only**：每条消息产生时 `aiofiles` 追加一行；不整文件重写。
- `parentUuid` 链：v1 可线性（上一条的 uuid），为将来 fork/branch 预留。

### 3.2 写入时机

在 `SessionState.extend_history` / `run_query` 消息 append 点挂一个 writer（`SessionTranscriptWriter`），把新 entry 追加到 `transcript_path`。Phase A 的 `SessionState` 已是唯一写入口，挂载点单一。

### 3.3 恢复（参考 `conversationRecovery.ts`）

```
load(session_id) →
  读 JSONL → 按 parentUuid chain-walk 到最新 leaf →
  清洗：
    - filter_unresolved_tool_uses（丢弃无配对 tool_result 的 tool_use 及其后续）
    - filter_orphaned_thinking（丢弃孤立 thinking-only 助手消息）
    - detect_turn_interruption（末条是 user 文本 → 标记中断，可注入 "Continue..."）
  → 反序列化为 messages → 塞进新建 SessionState.messages → 正常 REPL 继续
```

> Phase B 的清洗逻辑直接对标 `conversationRecovery.ts:154`（`deserializeMessagesWithInterruptDetection`）与 `:272`（`detectTurnInterruption`）。

### 3.4 会话列表 / 删除

- 列表：扫描 `projects/{hash}/*.jsonl`，按 mtime 排序，取首条 user message 文本作标题（`todolist:2499`）。
- 删除：删 `{session_id}.jsonl` + 清 `{session_id}/` sidechain 目录（`todolist:2502`）。

---

## 4. 影响/修改的文件

### Phase A
| 文件 | 变更 |
|---|---|
| `src/agent_core/session/state.py`（新建） | `SessionState` dataclass |
| `src/agent_core/session/__init__.py`（新建） | 导出 `SessionState` |
| `src/agent_core/models.py` | `DoneMessage` 增 `final_messages` |
| `src/agent_core/query/loop.py` | 各 `yield DoneMessage` 带 `final_messages=messages`；`session_store` 透传（与权限计划合并） |
| `src/agent_core/agent.py` | `Agent(config, session=None)`；`stream` 注入 `initial_messages`/`session_store` + 回写 `session.messages` |
| `src/agent_core/cli.py` | `_repl` 循环外建 `SessionState` 并注入；新增 `/clear` 命令清空 `session.messages`（可选） |
| `src/agent_core/agents/run.py` | 父历史来源改为 `session.messages`（语义不变） |

### Phase B（Item 10）
| 文件 | 变更 |
|---|---|
| `src/agent_core/persistence/transcript.py`（新建） | `SessionTranscriptWriter`（append JSONL）+ entry 模型 |
| `src/agent_core/persistence/recovery.py`（新建） | chain-walk + 清洗 + 反序列化（对标 conversationRecovery.ts） |
| `src/agent_core/persistence/session_list.py`（新建） | 列表 / 删除 |
| `src/agent_core/session/state.py` | 挂 `transcript_path` + writer 钩子 |
| `src/agent_core/cli.py` | `--resume` / `--continue` / `/sessions` |

---

## 5. 测试计划

### Phase A
| 测试 | 项 |
|---|---|
| `tests/unit/test_session_state.py`（新建） | `extend_history` / `snapshot_for_next_turn`；`permission_store` 字段在位 |
| `tests/integration/test_history_continuity.py`（新建） | **跨 prompt 延续**：第 1 个 prompt 说"记住 X=1"，第 2 个 prompt 问"X 是多少" → 第 2 轮 `initial_messages` 含第 1 轮内容（核心回归层 2 缺口） |
| 同上 | `DoneMessage.final_messages` 非空且 = 轮末 messages |
| 同上 | 压缩触发后 `final_messages` 是压缩后的（历史不无界增长） |
| `tests/unit/test_agent.py`（修改） | `Agent(config, session=None)` 向后兼容：不传 session 时行为同现状（每轮独立） |

### Phase B
| 测试 | 项 |
|---|---|
| `tests/unit/test_transcript.py` | append entry → 读回 → uuid/parentUuid 链正确 |
| `tests/unit/test_recovery.py` | 未闭合 tool_use 被清洗；末条 user 文本 → 标记中断 |
| `tests/integration/test_resume.py` | 写一个会话 → 进程外 load → messages 一致 → 继续对话 |

---

## 6. 实现步骤

### Phase A（先做，补层 2）
1. `session/state.py`：`SessionState`（含 `permission_store`）。
2. `models.py`：`DoneMessage.final_messages`。
3. `loop.py`：`yield DoneMessage(..., final_messages=messages)`。
4. `agent.py`：`Agent(config, session=None)`；`stream` 注入 + 回写。
5. `cli.py`：`_repl` 循环外建 `SessionState` 注入；（可选）`/clear`。
6. `run.py`：父历史取自 `session.messages`。
7. 测试 + 回归。

### Phase B（= Item 10，后做）
8. `persistence/transcript.py` writer，挂到 `SessionState`。
9. `persistence/recovery.py` chain-walk + 清洗。
10. `persistence/session_list.py` 列表/删除。
11. CLI `--resume`/`--continue`/`/sessions`。
12. 测试。

---

## 7. 风险与缓解

| 风险 | 缓解 |
|---|---|
| `final_messages` 把整段历史搬来搬去，长会话内存/拷贝开销 | `snapshot_for_next_turn` 浅拷贝；压缩路径已限制 `messages` 规模；Phase B 后可改为 writer 增量、内存只留窗口 |
| 不传 `session` 的旧调用（SDK / 测试）行为变化 | `session=None` 时完全等价现状（每轮独立、无历史） |
| 跨 prompt 历史使 token 持续上涨 | 复用现有 `hard_truncate` 压缩；回吐压缩后 messages |
| 子 Agent 历史与主会话混淆 | 物理分流：主 `{session_id}.jsonl`，子 Agent sidechain 子目录（沿用 `run.py:185`） |
| 与权限计划注入链冲突 | **合并为一条链**：`session_store` 即 `SessionState.permission_store`，一次注入两用 |

---

## 8. 与权限计划的关系（务必合并实施）

[permission-session-scope-plan.md] 的 `SessionPermissionStore` 作为 `SessionState.permission_store` 字段存在。两计划共享同一条注入链（`Agent → query → run_query`），**建议同一个 PR 一起做 Phase A**：

- 一次性把 `SessionState` 注入贯通，既载历史也载权限记忆；
- 避免为权限、为历史各改一遍相同的签名链。
