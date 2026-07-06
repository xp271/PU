# QQ 机器人：跨会话记忆加载修复计划书

> **目标**：修复 QQBot channel 中 `AgentBridge` 从不从磁盘加载历史会话、也从不将对话持久化到磁盘的问题，实现真正的跨会话记忆。

> **状态**：📋 计划阶段，尚未实现。
>
> **审查记录**：
> - v1（初稿，2026-07-06）
> - v1 → v2（2026-07-06：移除 1 小时 session 超时清理机制——该机制由 `AgentBridge` 自行引入，非 botpy 行为，且与跨会话记忆持久化的目标冲突）
> - v2 → v3（2026-07-06：对照源码逐条审查——修正 `_resolve_path` 改为 async 的遗漏；记录 `session_id` 不一致、`clear_history` 遗留缺陷、测试适配量等发现）

---

## 0. 背景与动机

### 0.1 现象

用户在 QQBot 中发起的每一轮对话都是"失忆"的——即使在同一 C2C/群聊中，上一轮的对话历史不会出现在下一轮中。

### 0.2 根因

经过代码审查，定位到两条路径的不对称：

| 职责 | CLI 路径 (`cli/_main.py`) | QQBot 路径 (`bridge/agent_bridge.py`) |
|------|--------------------------|--------------------------------------|
| 加载历史 | ✅ `load_session(resume_id)` → 从 JSONL 恢复 `SessionState` | ❌ 永远 `SessionState()` 新建空白 |
| 激活持久化 | ✅ `session.begin_persistence(tpath)` → `_writer` 初始化 | ❌ 从未调用 → `_writer = None` |
| 写入磁盘 | ✅ `session.flush_new_messages()` → 真正 append JSONL | ❌ `flush_new_messages()` 因 `_writer is None` 静默跳过 |
| 会话 ID 映射 | ✅ 用户指定 `--resume` 或 `--continue` | ❌ `"c2c:xxx"` 无映射到磁盘的随机 UUID |

**关键代码路径**：

```
CLI:
  _repl() → load_session(resume_id) → begin_persistence(tpath)
    → Agent(session=session) → stream() → flush_new_messages() ✅

QQBot:
  AgentBridge._get_or_create_session() → SessionState()  ← 永远空白
    → 无 load_session()   ← 不读盘
    → 无 begin_persistence() ← 不写盘
    → Agent(session=session) → stream() → flush_new_messages() ← 静默跳过
```

`agent_bridge.py:79-83` 的 session 创建逻辑：

```python
if session_id not in self._sessions:
    self._sessions[session_id] = SessionState()  # ← 永远是空白新 session
    self._session_locks[session_id] = asyncio.Lock()
```

对比 `cli/_main.py:271`：

```python
await session.begin_persistence(tpath)  # ← CLI 有，bridge 没有！
```

`session/state.py:139-143` 的写入守卫：

```python
async def flush_new_messages(self, final_messages):
    if final_messages is None:
        return
    if self._writer is None or not self._writer.is_open:
        return  # ← QQBot 路径在此静默返回
```

### 0.3 额外问题：内置 1 小时超时清理（Bridge 自创，非 botpy）

`AgentBridge.__init__` 中硬编码了 `session_timeout=3600.0`（1 小时），并在 `_get_or_create_session` 中惰性检查：

```python
# agent_bridge.py:40
self._session_timeout = session_timeout  # 默认 3600 秒

# agent_bridge.py:73-80
if session_id in self._sessions:
    last = self._sessions_last_access.get(session_id, 0.0)
    if now - last > self._session_timeout:
        self._sessions.pop(session_id, None)   # 直接丢弃
        self._session_locks.pop(session_id, None)
        self._sessions_last_access.pop(session_id, None)
```

这导致用户超过 1 小时不说话后，即使之前有对话历史也全部丢失，**且未调用 `end_persistence()` 导致 writer 未正常关闭**。

**澄清**：botpy 库**不管理 session 生命周期**——它只负责 WebSocket 连接、心跳和重连。这个超时机制完全是 `AgentBridge` 自行引入的，与 botpy 无关。

**决策**：**彻底移除** `session_timeout`、`_sessions_last_access` 及所有关联的超时检查逻辑。理由：

1. 既然 session 已持久化到磁盘，内存中保留多久都不影响正确性——重启后可从磁盘恢复
2. 超时清理与"跨会话记忆"的根本目标矛盾
3. 内存压力可忽略——每个 `SessionState` 仅持有消息列表的引用，JSONL writer 的缓冲区很小
4. 唯一合理的清理时机是**进程退出**（`end_persistence()` 自然触发）

### 0.4 额外问题：Session ID 映射

| 来源 | Session ID 格式 | 示例 |
|------|----------------|------|
| QQBot bridge | `"{channel_id}:{user_id}"` | `"c2c:539AF88135E6636C9B4BFAF1B6F380EA"` |
| 磁盘持久化 | 随机 UUID（`uuid4().hex`） | `"62866d2c3cc542e1a82291c78f47d21c"` |

两者没有映射关系。bridge 不知道当前 QQ 用户对应磁盘上哪个 session。

---

## 1. 设计决策

### 1.1 Session ID 策略

**方案 A：直接使用 bridge 格式的 session ID 作为磁盘文件名**

优点：无需映射表，`"c2c:xxx"` 本身就是文件名
缺点：session ID 暴露用户 openid，不够隐私友好

**方案 B：维护一个映射文件（JSON），`bridge_id → disk_uuid`**

优点：保持现有 UUID 格式不变
缺点：多一个映射文件，多了读写失败的风险点

**✅ 采用方案 A**——桥接层直接使用 `"c2c:{openid}"` 或 `"{group_id}:{member_openid}"` 作为 session ID 传给 `SessionState`。

理由：
1. 最简单、零额外依赖
2. 用户 openid 并非敏感密码——它已经通过 bot API 暴露在消息体里
3. `compute_project_hash()` 已经对 workspace 做了哈希，路径层已有一层匿名化
4. 避免映射文件同步问题（删除 session 时忘了更新映射等）

### 1.2 持久化激活时机

**方案 A：`AgentBridge.process()` 每次消息都检查并激活**

```python
async def process(self, user_id, open_kfid, content, msg_type):
    session = await self._get_or_create_session(sid)  # 改为异步
    if session._writer is None:
        tpath = session_jsonl_path(ph, session.session_id)
        await session.begin_persistence(tpath)
```

**方案 B：`_get_or_create_session` 中一步完成加载 + 激活**

**✅ 采用方案 B**——在 `_get_or_create_session` 内部完成全部初始化：

```
_get_or_create_session(sid):
    1. 内存命中 → 直接返回（无超时检查）
    2. 尝试 load_session(safe_sid) 从磁盘恢复
    3. 磁盘无记录 → SessionState(session_id=sid) 新建
    4. begin_persistence(tpath) 激活写入
    5. 存入内存缓存
```

### 1.3 `AgentBridge._get_or_create_session` 改为异步

当前是同步方法，因为 `load_session()` 和 `begin_persistence()` 都是 `async`，必须改为异步。

影响面：
- `process()` 中已 `await` 调用 → 只需改方法签名
- `get_session()` 公开接口 → Channel 层调用需 `await`
- `clear_session()` → 清理时需 `await end_persistence()`

### 1.4 workspace_override 跨重启保持

当前 `workspace_override` 是纯内存字段（`SessionState.workspace_override`），重启后丢失。本计划暂不处理——因为：
- `/cd` 命令会先清 history，即使恢复了 workspace 也没历史可看
- 用户重启后重新 `/cd` 一次成本很低
- 未来可考虑单独计划处理

---

## 2. 修改范围

### 2.1 `src/agent_core/bridge/agent_bridge.py`

| 方法 | 变更 |
|------|------|
| `__init__` | 移除 `session_timeout`、`_sessions_last_access`；新增 `_workspace_root`、`_project_hash` |
| `session_id()` | 无需变更（格式已为 `channel_id:user_id`） |
| `_safe_session_id()` | **新增静态方法**：`sha256(sid)[:16]`，用于 Windows 安全的文件名 |
| `_get_or_create_session()` | **改为 `async`**；移除超时检查；加入 `load_session()` + `begin_persistence()` 流程 |
| `process()` | `_get_or_create_session` 调用加 `await` |
| `get_session()` | 改为 `async` |
| `clear_session()` | 改为 `async`，增加 `await session.end_persistence()`；移除 `_sessions_last_access` |
| ~~超时清理逻辑~~ | **完全移除** |

### 2.2 `src/agent_core/channels/qq_bot/channel.py`

| 方法 | 变更 |
|------|------|
| `_resolve_path()` | **改为 `async`**；`self._bridge.get_session()` 加 `await` |
| `_handle_cd_command()` | 内部 `self._bridge.get_session()` 加 `await`；`_resolve_path` 调用加 `await` |
| `_handle_ls_command()` | `_resolve_path` 调用加 `await` |
| `_handle_clear_command()` | `self._bridge.get_session()` 加 `await` |

> **注意**：`_resolve_path` 当前是同步方法，因 `get_session()` 改为异步，它也必须是异步的。这进而影响 `_handle_cd_command` 和 `_handle_ls_command` 中对它的调用。

### 2.3 测试文件

| 文件 | 变更 |
|------|------|
| `tests/unit/bridge/test_agent_bridge.py` | 新增/修改测试：session 异步创建、持久化激活、load 恢复 |
| `tests/unit/qq_bot/test_channel.py` | 更新 mock，适配异步 `get_session` |

### 2.4 不修改的文件

| 文件 | 原因 |
|------|------|
| `session/state.py` | `SessionState`、`begin_persistence`、`flush_new_messages` 逻辑本身正确，无需改动 |
| `persistence/session.py` | `load_session` 逻辑正确 |
| `cli/_main.py` | CLI 路径本身正常工作 |
| `agent.py` | Agent 的 session/recorder 交互邏輯正確 |
| `persistence/path.py` | `session_jsonl_path` 接受任意 session_id，无需改动 |

---

## 3. 详细设计

### 3.1 `AgentBridge.__init__` 变更

```python
def __init__(self, base_config, system_prompt=None, error_message=None):
    self._base = base_config
    self._system_prompt = system_prompt
    self._error_message = error_message or "..."
    self._sessions: dict[str, SessionState] = {}
    self._session_locks: dict[str, asyncio.Lock] = {}
    # 新增：持久化所需
    self._workspace_root = base_config.workspace_root
    self._project_hash: str | None = None  # 惰性计算
```

**移除的字段**：`_session_timeout`、`_sessions_last_access` 全部删除。session 生命周期仅由进程启动/退出管理。

### 3.2 新的 `_get_or_create_session`（异步）

```python
async def _get_or_create_session(self, session_id: str) -> SessionState:
    """获取或创建 SessionState，从磁盘恢复历史（如有），激活持久化。

    流程：
    1. 惰性计算 project_hash
    2. 内存命中 → 直接返回（无超时检查）
    3. 尝试 load_session(safe_sid) 从磁盘恢复
    4. 磁盘无记录 → SessionState(session_id=session_id) 新建
    5. begin_persistence(tpath) 激活写入
    6. 存入内存缓存
    """
    from ..persistence.path import compute_project_hash, session_jsonl_path
    from ..persistence.session import load_session

    # 惰性计算 project_hash
    if self._project_hash is None:
        self._project_hash = compute_project_hash(self._workspace_root)

    # 内存命中（无超时检查——session 生命周期 = 进程生命周期）
    if session_id in self._sessions:
        return self._sessions[session_id]

    # 尝试从磁盘恢复
    safe_sid = self._safe_session_id(session_id)
    session = await load_session(
        safe_sid,
        project_hash=self._project_hash,
    )

    # 磁盘无记录 → 新建
    if session is None:
        session = SessionState(session_id=session_id)

    # 激活持久化（如果尚未激活）
    if session._writer is None:
        tpath = session_jsonl_path(self._project_hash, safe_sid)
        await session.begin_persistence(tpath)

    # 缓存
    self._sessions[session_id] = session
    if session_id not in self._session_locks:
        self._session_locks[session_id] = asyncio.Lock()

    return session
```

### 3.3 `process()` 适配

```python
async def process(self, user_id, open_kfid, content, msg_type):
    sid = self.session_id(user_id, open_kfid)
    session = await self._get_or_create_session(sid)  # 改为 await
    lock = self._session_locks[sid]
    # ... 其余不变
```

### 3.4 `get_session()` 改为异步

```python
async def get_session(self, user_id, open_kfid):
    sid = self.session_id(user_id, open_kfid)
    return await self._get_or_create_session(sid)
```

### 3.5 `clear_session()` 适配

```python
async def clear_session(self, user_id, open_kfid):
    sid = self.session_id(user_id, open_kfid)
    old = self._sessions.pop(sid, None)
    if old is not None:
        await old.end_persistence()
    self._session_locks.pop(sid, None)
```

### 3.6 Channel 层适配（`channel.py`）

所有 `self._bridge.get_session(...)` → `await self._bridge.get_session(...)`：

```python
# _resolve_path() — 改为 async def
async def _resolve_path(self, user_id, channel_id, arg):
    session = await self._bridge.get_session(user_id, channel_id)
    # ... 其余不变

# _handle_cd_command()
session = await self._bridge.get_session(user_id, channel_id)
# _resolve_path 调用也加 await：
new_path, err = await self._resolve_path(user_id, channel_id, arg)

# _handle_ls_command()
target, err = await self._resolve_path(user_id, channel_id, arg)

# _handle_clear_command()
session = await self._bridge.get_session(user_id, channel_id)
```

---

## 4. 边界情况

### 4.1 机器人重启

- 内存中的 `self._sessions` 丢失 ← 正常
- 下次用户发消息 → `_get_or_create_session` 检测内存中无 → `load_session()` 从磁盘恢复
- `workspace_override` 在重启后丢失 → 回到 `config.workspace_root`（可接受，用户需重新 `/cd`）

### 4.2 首次对话（磁盘无历史）

- `load_session()` 返回 `None`
- 新建 `SessionState(session_id=sid)`
- `begin_persistence()` 创建新 JSONL 文件
- 表现与新用户完全一致

### 4.3 并发安全

- 两个异步消息同时抵达同一用户 → per-session `asyncio.Lock` 串行化
- `_get_or_create_session` 在 `lock` 外部调用（锁在 `process` 中获取）
- 极端情况：两个并发请求同时走到"磁盘无记录→新建"→两个 `SessionState` 创建。但由于 `begin_persistence` 是 append 模式，不会损坏文件；且 `load_session` 恢复时会走 `clean_and_rebuild` 去重。**低概率无危害**。

### 4.4 配置变更（workspace_root 变化）

- `_project_hash` 在 `__init__` 惰性计算一次
- 如果 `/restart` 后 `workspace_root` 变了，`_project_hash` 会重新计算
- 旧的 session 文件留在旧 `project_hash` 目录下（符合预期）

---

## 5. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| `_get_or_create_session` 改为异步可能遗漏调用点 | 运行时 `coroutine not awaited` 警告/崩溃 | 全项目 grep `get_session` + `_get_or_create_session`，确保所有调用点已适配 |
| `session_id` 直接使用 `"c2c:openid"` 可能与 v0 旧格式冲突 | 文件名包含 `:` 在 Windows 上非法 | `session_jsonl_path` 已对 session_id 做了安全处理（`compute_project_hash` 产出的目录下以 session_id 命名）；但需确认 Windows 兼容性。备选：对 session_id 做 `hashlib.sha256(sid).hexdigest()[:16]` |
| `begin_persistence` 在每次消息都检查是否浪费时间 | 可忽略 | `begin_persistence` 内部检查 `_writer is None` 是 O(1)；只在 session 第一次创建时真正执行 |
| 旧磁盘上的随机 UUID session 文件不会被新机制使用 | 遗留垃圾文件 | 可接受——`cleanup_old_sessions` 会按时间清理 |

### 5.1 特别关注：session_id 中的 `:` 字符

当前 `AgentBridge.session_id()` 返回 `"c2c:539AF88135E6636C9B4BFAF1B6F380EA"`，包含 `:`。

`persistence/path.py` 中 `session_jsonl_path` 的实现需确认是否对 `:` 做了处理。检查 `path.py`：

```python
# 预期路径格式
# {data_dir}/sessions/{project_hash}/{session_id}.jsonl
```

Windows 文件名不支持 `:`。**需要在 `session_id` 进入文件路径前做安全转义**。

**决策**：在 `_get_or_create_session` 中计算 `safe_session_id`：

```python
import hashlib

@staticmethod
def _safe_session_id(bridge_session_id: str) -> str:
    """将 bridge 格式的 session_id 转为文件安全的标识符。"""
    return hashlib.sha256(bridge_session_id.encode()).hexdigest()[:16]
```

这样磁盘上是 `a1b2c3d4e5f6g7h8.jsonl`，而 `SessionState.session_id` 保持为原始的 `"c2c:xxx"`。

同时，`load_session` 需要接收这个 hash 后的 ID 去查文件，但 `SessionState.session_id` 应保留原始 bridge ID。

**修正方案**：`SessionState.session_id` 使用 bridge ID（`"c2c:xxx"`），文件路径使用 hash：

```python
safe_sid = hashlib.sha256(session_id.encode()).hexdigest()[:16]
tpath = session_jsonl_path(self._project_hash, safe_sid)
session = SessionState(session_id=session_id)  # ← 保留原始 bridge ID
await session.begin_persistence(tpath)
```

`load_session()` 的调用也需要用 `safe_sid`。

---

## 6. 实施步骤

1. **阅读 `persistence/path.py`**，确认 `session_jsonl_path` 的路径构造逻辑及 Windows 兼容性
2. **修改 `AgentBridge`**：
   - `__init__`：移除 `session_timeout`、`_sessions_last_access`；新增 `_workspace_root`、`_project_hash`
   - 新增 `_safe_session_id()` 静态方法
   - `_get_or_create_session` → `async`，移除超时检查，加入 load + begin_persistence
   - `get_session` → `async`
   - `clear_session` → `async`，加入 end_persistence，移除 `_sessions_last_access`
   - `process` 中 `_get_or_create_session` 加 `await`
3. **修改 `QQBotChannel`**：所有 `get_session` 调用加 `await`
4. **更新测试**：新增 mock `load_session`/`begin_persistence` 的测试；移除超时相关测试
5. **回归测试**：运行 `pytest tests/unit/ -v`
6. **手动验证**：启动 QQBot，发送多条消息，重启，验证记忆保留

---

## 7. 测试用例

### 7.1 单元测试（`test_agent_bridge.py`）

| 用例 | 描述 |
|------|------|
| `test_session_persistence_activated_on_create` | 新建 session 后 `_writer` 不为 None |
| `test_session_loaded_from_disk_on_create` | 模拟 `load_session` 返回有历史的 session，验证消息列表非空、`_written_count` 预设正确 |
| `test_session_fresh_when_no_disk_record` | 模拟 `load_session` 返回 None，验证新建空白 session |
| `test_safe_session_id_no_colon` | `_safe_session_id("c2c:xxx")` 不包含 `:` |
| `test_clear_session_calls_end_persistence` | `clear_session` 触发 `end_persistence` 且 writer 关闭 |
| `test_no_session_timeout` | `AgentBridge.__init__` 不应有 `_session_timeout` 属性；超过 1 小时后同一 session_id 仍返回同一 SessionState |
| `test_get_session_now_async` | `get_session()` 是可 await 的协程 |

### 7.2 集成测试（手动 / e2e 脚本）

| 用例 | 步骤 |
|------|------|
| 跨重启记忆 | 1. QQBot 发送 "我叫小明" 2. 重启 QQBot 3. 发送 "我叫什么" → 期望回答包含 "小明" |
| 首次对话 | 1. 新用户发消息 2. 检查 `{data_dir}/sessions/{hash}/{safe_id}.jsonl` 已创建 |
| 长时间无活动 | 1. 发消息 2. 等待任意时长（不设上限） 3. 发消息 → 历史应保留（session 终身有效，直至进程退出） |

---

## 8. 工时预估

| 任务 | 预估 |
|------|------|
| 阅读 `persistence/path.py` + 确认 Windows 兼容 | 0.1d |
| 修改 `AgentBridge` | 0.2d |
| 修改 `QQBotChannel` 适配 | 0.05d |
| 更新单元测试 | 0.15d |
| 回归 + 手动验证 | 0.1d |
| **合计** | **~0.5d** |

---

## 9. 代码审查发现（2026-07-06 对照源码逐条核查）

### 9.1 ✅ 计划书假设已验证

| 假设 | 源码验证 | 结论 |
|------|----------|------|
| `session_jsonl_path` 直接用 `os.path.join`，`:` 在 Windows 上非法 | `path.py:58`: `os.path.join(project_dir(project_hash), f"{session_id}.jsonl")` | ✅ 确认，`_safe_session_id` 方案必要 |
| `load_session` 接收 `project_hash` 参数 | `session.py:200`: `async def load_session(session_id, *, workspace_root=None, project_hash=None)` | ✅ 确认 |
| `load_session` 不调用 `begin_persistence` | `session.py:235-249`: 手动设 `_written_count`、`transcript_path`，不调 `begin_persistence` | ✅ 确认，bridge 侧需自行调用 |
| `begin_persistence` 检查 `_writer is None` 才创建新 writer | `state.py:101-118`: 创建 `SessionTranscriptWriter`，从 `_written_count` 开始写 | ✅ 确认，幂等安全 |
| `flush_new_messages` 检查 `_writer is None or not _writer.is_open` | `state.py:139-143` | ✅ 确认，这就是当前静默跳过的守卫 |
| `process()` 中 `_get_or_create_session` 在 lock 外调用 | `agent_bridge.py:111-112` | ✅ 确认，异步化后保持此顺序 |
| `channel.py` 调用 `get_session` 共 3 处 | `channel.py:170, 215, 328` | ✅ 确认 |

### 9.2 ⚠️ 计划书遗漏 / 需修正

#### 9.2.1 `_resolve_path` 必须改为 async（已修正）

原计划只说了"`get_session()` 加 `await`"，但 `_resolve_path` 本身是 `def` 不是 `async def`。已在 §2.2 和 §3.6 中修正。

#### 9.2.2 `load_session` 返回的 `SessionState.session_id` 与新建 session 不一致

调用链：
```python
safe_sid = self._safe_session_id(session_id)         # e.g. "a1b2c3d4e5f6g7h8"
session = await load_session(safe_sid, ...)           # → SessionState(session_id="a1b2...")
# 而新建时：
session = SessionState(session_id=session_id)         # → SessionState(session_id="c2c:xxx")
```

**影响**：`session.session_id` 在 load 路径是 hash，在新建路径是 bridge 格式。`session_id` 被 writer 写入 JSONL entry 的 `session_id` 字段，日志中也会出现。**功能无影响**（文件路径用 `safe_sid`，内存 key 用 bridge format），但日志可读性下降。

**决策**：接受此不一致。如需完美一致，可在 `load_session` 返回后手动覆盖 `session.session_id = session_id`（bridge 格式）。实施时再定。

#### 9.2.3 `clear_history()` 不清除已持久化的 JSONL 数据（已知遗留问题）

当前 `SessionState.clear_history()` 设 `messages=[]` 和 `_written_count=0`，但不关闭/截断 writer。下一次 `flush_new_messages` 会从文件当前位置 append，导致 JSONL 文件中同时存在旧历史和新历史。`load_session` → `clean_and_rebuild` 会合并所有条目，导致 `/clear` 后重启又恢复旧历史。

**影响**：这是 `SessionState.clear_history()` 的**既有设计缺陷**，不是本次修改引入的。bridge 修复后，/clear 后的 session 重启后可能带有混合历史。

**决策**：不在本次计划中修复。临时缓解：`/clear` 时调用 `bridge.clear_session()` 替代 `session.clear_history()`，但 `clear_session` 会完全删除 session（包括 permission_store），语义更强。长期应由 `clear_history` 在 writer 打开时写入一个 `type="clear"` 哨兵条目，`clean_and_rebuild` 识别并截断。

#### 9.2.4 测试文件适配量比预期大

| 文件 | 受影响测试 | 变更类型 |
|------|-----------|----------|
| `test_agent_bridge.py` | `bridge` fixture 传 `session_timeout=0.1` → 移除参数 | 签名变更 |
| | `test_session_timeout_lazy_cleanup` | **删除** |
| | `TestGetSession` 3 个测试：`get_session()` 改为 `await` | 加 `await` |
| | `TestWorkspaceOverride` 3 个测试：`get_session()` 改为 `await` | 加 `await` |
| | `TestSessionManagement.test_session_creation` 等：`_get_or_create_session` 同步调用需 mock 绕过（因其变 async 且依赖磁盘 I/O） | **重构为 async + mock** |
| `test_channel.py` | 全部约 30 个 `channel._bridge.get_session(...)` 调用 | 加 `await` |
| | `TestCdCommand` 中 `_resolve_path` 相关调用 | 加 `await` |

> 测试改动量大，建议实施时先改源码让类型检查通过，再批量修测试。

### 9.3 🔴 不需要修改的确认

| 文件/方法 | 原因 |
|-----------|------|
| `agent.py:186` `flush_new_messages` | Agent 侧逻辑正确，bridge 只需激活 writer 即可生效 |
| `cli/_main.py:271` `begin_persistence` | CLI 路径独立且正常工作 |
| `persistence/path.py` | `session_jsonl_path` 接受任意 string，无需修改 |
| `persistence/session.py` `load_session` | 签名和逻辑正确 |
| `session/state.py` | 所有方法逻辑正确 |
| `channel.py` `_handle_restart_command` | 不调用 `get_session`（用 `os.execv` 替换进程），无需修改 |
