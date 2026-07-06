# 通用会话 Checkpoint：跨进程恢复对话状态（v1）

> **目标**：在 agent-core 本体添加会话 checkpoint 机制——每个会话结束时自动保存轻量快照，用户重启后可选择继续任意历史对话（CLI 交互式选择 + QQBot 自动恢复）。

---

> **状态**：📋 计划阶段，尚未实现。
>
> **审查记录**：
> - v1.1（修订版）— 2026-07-05 审查通过，已根据实际代码审查修正 14 处

---

## 0. 背景与现状

### 0.1 已有基础设施

| 组件 | 能力 | 限制 |
|------|------|------|
| `SessionState` | 内存中保存 `messages`、`permission_store`、`workspace_override`、`_written_count`、`_compaction_count` | 进程死即丢失 |
| JSONL Transcript | 每次 `flush_new_messages()` 追加到 `.jsonl` | 仅存对话文本，不含 `workspace_override` 和 `permission_store` |
| `load_session()` | 从 `.jsonl` 恢复 `messages` + `_written_count` + `_compaction_count` | 不恢复 `workspace_override`、`permission_store` |
| `list_sessions()` | 扫描 `.jsonl` 返回 `SessionInfo` 列表 | 需全文读取每个文件，O(n) |
| CLI `--resume <id>` / `--continue-last` | 命令行恢复指定 session | 无交互式选择；无 metadata 丰富度 |
| CLI `/sessions` | 列出已存 session | 信息有限（仅 session_id[:8]、时间、消息数、标题） |
| CLI `/quit` | 退出 REPL | 不触发 checkpoint——仅 JSONL 留在磁盘 |

### 0.2 痛点

1. **`workspace_override` 不持久化** — 用户在 QQBot 里 `/cd src` 后，进程重启或超时后回到 `workspace_root`，需重新切换
2. **`permission_store` 不跨 session 恢复** — CLI 已有注释 `[note: session permission memory is not restored across sessions]`
3. **QQBot 无任何持久化** — 从未调用 `begin_persistence()`，超时/重启后对话全部丢失
4. **CLI 无可视化恢复** — 只能用 `--resume <hex-id>`，用户记不住 32 位 hex session_id
5. **`list_sessions()` 慢** — 每次需全文读取 JSONL 获取 `message_count`；新增轻量 checkpoint 文件后可显著加速

### 0.3 目标体验

```
$ agent-core

  最近会话:
  [1] 修复了登录bug                 2026-07-05 14:30  (42 msgs, src/)
  [2] 分析性能问题                   2026-07-05 09:15  (18 msgs, .)
  [3] (空会话)                      2026-07-04 22:00  (0 msgs)

  输入编号继续，或直接回车开始新会话 > 1

  [resumed session abc12345... (42 messages)]
  [workspace: /home/user/project/src]

agent-core> /sessions          ← 随时查看
agent-core> /checkpoint        ← 手动存档
agent-core> /quit              ← 自动存档 + 退出
```

```
# QQBot 侧
用户: (1h 后回来发消息)
Bot:  [已从上次对话恢复 (42 条消息)]
      当前工作目录: C:\PU\src
用户: /ls
Bot:  C:\PU\src\
      📁 agent_core/    📁 tests/    ...
```

---

## 1. 设计决策

### 1.1 Checkpoint 文件格式

**路径**：`{project_dir}/{session_id}.checkpoint.json`

与 `.jsonl` 并列存放，示例：

```json
{
  "version": 1,
  "session_id": "abc123def4567890",
  "title": "修复了登录bug",
  "workspace_root": "/home/user/project",
  "workspace_override": "/home/user/project/src",
  "created_at": "2026-07-05T14:30:00.123456",
  "last_active_at": "2026-07-05T15:45:00.654321",
  "message_count": 42,
  "written_count": 42,
  "compaction_count": 1,
  "permission_rules": [
    {
      "tool_pattern": "bash",
      "action": "allow",
      "priority": 20,
      "path_constraint": null,
      "rule_content": null,
      "source": "session",
      "description": "Session allow for 'bash'."
    }
  ]
}
```

### 1.2 与 JSONL Transcript 的关系

**CLI 场景（session_id = UUID）**

```
.agent/sessions/<project_hash>/
├── abc12345.jsonl              ← 完整对话转录
├── abc12345.checkpoint.json    ← 轻量快照
├── def67890.jsonl
└── def67890.checkpoint.json
```

**QQBot 场景（checkpoint_key = sanitized bridge key）**

```
.agent/sessions/<project_hash>/
├── c2c_user1.jsonl              ← JSONL 也用 checkpoint_key 命名
├── c2c_user1.checkpoint.json    ← 与 JSONL 同名不同后缀
├── group456_user2.jsonl
└── group456_user2.checkpoint.json
```

> **关键规则**：QQBot 的 `begin_persistence` 必须用 `checkpoint_key`（而非 `session.session_id` / UUID）作为 JSONL 文件名，确保 `list_sessions()` 扫描 `.jsonl` 拿到的文件名就是 checkpoint key，从而能命中同名 `.checkpoint.json`。

- **JSONL** = 真相源（source of truth），对话的每一条消息
- **Checkpoint** = 恢复加速器（resume accelerator），元数据 + 非对话状态
- Checkpoint 丢失不影响 JSONL 恢复——`load_session()` 照常工作，只是 `workspace_override` 和 `permission_rules` 会丢失

### 1.3 触发时机

| 触发点 | CLI | QQBot |
|--------|-----|-------|
| 正常退出（`/quit`、`stop()`） | ✅ | ✅ |
| 中断退出（`SIGINT`、`Ctrl+C`） | ✅（`finally` 块） | ✅（`finally` 块） |
| 超时清理 | 不适用 | ✅（惰性过期回调） |
| 手动指令 `/checkpoint` | ✅ | ✅ |
| 每次消息后 | ❌（太频繁，I/O 开销） | ❌ |

### 1.4 去重策略

同名 session_id 已有 checkpoint 时直接覆盖（checkpoint 是幂等的快照，不是 append-only log）。

### 1.5 兼容性

- **不修改 JSONL 格式**
- **不修改 `TranscriptEntry` 结构**
- **不修改现有 `load_session()` 签名**
- CLI 的 `begin_persistence` 仍用 `session.session_id`（UUID）——行为不变
- QQBot 的 `begin_persistence` 改用 `checkpoint_key`（sanitized bridge key）——与 checkpoint 文件同名，确保 `list_sessions()` 可关联
- 新增 `load_session_full()` 作为增强版恢复入口
- 旧 session（无 `.checkpoint.json`）仍可通过 `load_session()` 恢复

---

## 2. 关键架构决策：双 Session Identity 路径

### 2.0 QQBot 与 CLI 的 session_id 不兼容问题

CLI 和 QQBot 用 **不同的 ID 体系**：

| 来源 | session_id 格式 | 示例 |
|------|----------------|------|
| CLI `SessionState` | `uuid.uuid4().hex`（32 位 hex） | `abc123def4567890...` |
| QQBot `AgentBridge` | `f"{channel_id}:{user_id}"` | `c2c:user1`、`group123:user2` |

**问题**：QQBot 超时后创建的新 `SessionState` 会生成**新 UUID**，旧 UUID 永远丢失——`on_session_created` 回调拿到的是新 UUID，无法用它找到旧 checkpoint。

### 2.0.1 解决方案：`checkpoint_key` 参数化

所有 checkpoint 读写函数接受显式的 `checkpoint_key: str` 参数，**默认值**为 `session.session_id`：

```python
async def save_checkpoint(
    session: SessionState,
    *,
    checkpoint_key: str | None = None,  # 默认 = session.session_id
    workspace_root: str | None = None,
) -> bool: ...

async def load_checkpoint(
    checkpoint_key: str,  # 必传——调用者明确身份
    *,
    workspace_root: str | None = None,
) -> dict | None: ...
```

| 调用者 | `checkpoint_key` 值 | 效果 |
|--------|---------------------|------|
| CLI `_repl()` | 不传（默认 `session.session_id`） | UUID 不变，checkpoint 可复用 |
| QQBot evict | `sanitize(f"{channel}:{user}")` → `"c2c_user1"` | 稳定的跨生命周期 key |
| QQBot create | `sanitize(f"{channel}:{user}")` → `"c2c_user1"` | 找到同一个 key 的旧 checkpoint |

**sanitize 规则**：`checkpoint_key = bridge_session_id.replace(":", "_").replace("\\", "_").replace("/", "_")`

这样 **CLI 不受影响**（UUID 始终是有效文件名），**QQBot 获得稳定的 checkpoint 关联**，且 **SessionState 的内部 `session_id` 不需要修改**。

---

## 3. 详细设计

### 3.1 文件改动清单

| # | 文件 | 操作 | 说明 |
|---|------|------|------|
| 1 | `session/state.py` | **修改** | `+checkpoint_dict()` / `+restore_checkpoint()` |
| 2 | `persistence/path.py` | **修改** | `+checkpoint_json_path(checkpoint_key)` |
| 3 | `persistence/session.py` | **修改** | `+save_checkpoint()` / `+load_checkpoint()` / `+load_session_full()` / 改 `list_sessions()` |
| 4 | `permissions/store.py` | **修改** | `+to_rules_list()` / `+restore_from_rules()` |
| 5 | `cli/_main.py` | **修改** | `/quit` 保存 checkpoint；`/checkpoint` 指令；交互式会话选择；`_repl` 入口增强 |
| 6 | `channels/qq_bot/channel.py` | **修改** | 注册 bridge 回调，超时/清理时存档，创建时恢复 |
| 7 | `bridge/agent_bridge.py` | **修改** | `+on_session_evict` / `+on_session_create` 回调钩子（回调接收 bridge_key） |
| 8 | `tests/unit/test_session_state.py` | **修改** | `+test_checkpoint_dict_roundtrip` 等 |
| 9 | `tests/unit/test_persistence_session.py` | **新增** | `TestCheckpoint` |
| 10 | `tests/unit/bridge/test_agent_bridge.py` | **修改** | `+test_checkpoint_callbacks` |
| 11 | `tests/unit/qq_bot/test_channel.py` | **修改** | `+test_checkpoint_hooks` |

### 3.2 `permissions/store.py` — 序列化支持

```python
# 新增方法

def to_rules_list(self) -> list[dict[str, Any]]:
    """Export all remembered rules as JSON-serializable dicts."""
    return [r.model_dump() for r in self._rules]

def restore_from_rules(self, rules: list[dict[str, Any]]) -> None:
    """Import rules from a previously exported list (idempotent)."""
    for r_dict in rules:
        rule = PermissionRule.model_validate(r_dict)
        # 去重
        dumped = rule.model_dump()
        if any(r.model_dump() == dumped for r in self._rules):
            continue
        self._rules.append(rule)
```

**注意**：`PermissionRule` 已经是 `pydantic.BaseModel`，`model_dump()` 和 `model_validate()` 天然支持序列化。

### 3.3 `persistence/path.py` — 新增路径函数

```python
def checkpoint_json_path(project_hash: str, checkpoint_key: str) -> str:
    """{project_dir}/{checkpoint_key}.checkpoint.json

    checkpoint_key 可以是 session.session_id (CLI) 或
    经过 sanitize 的 bridge session key (QQBot)。
    """
    return os.path.join(project_dir(project_hash), f"{checkpoint_key}.checkpoint.json")
```

### 3.4 `session/state.py` — 序列化方法

**新增字段**（`SessionState` dataclass 追加）：

```python
_created_at: str | None = field(default=None, repr=False)
"""ISO timestamp set on first checkpoint save. Read-only after set."""

_last_active_at: str | None = field(default=None, repr=False)
"""ISO timestamp updated on every checkpoint save."""
```

**新增方法**：

```python
def checkpoint_dict(self) -> dict[str, Any]:
    """Generate a JSON-serializable checkpoint snapshot.

    提取第一条 user 消息文本作为 title（截断 100 字符）。
    """
    title = "(新会话)"
    for msg in self.messages:
        if msg.get("role") == "user":
            content = msg.get("content", [])
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        title = (b.get("text", "") or "(空消息)")[:100].replace("\n", " ")
                        break
            elif isinstance(content, str):
                title = content[:100].replace("\n", " ")
            break

    return {
        "version": 1,
        "session_id": self.session_id,
        "title": title,
        "created_at": self._created_at,
        "last_active_at": self._last_active_at,
        "message_count": len(self.messages),
        "written_count": self._written_count,
        "compaction_count": self._compaction_count,
        "workspace_override": self.workspace_override,
        "permission_rules": self.permission_store.to_rules_list(),
    }

def restore_checkpoint(self, data: dict[str, Any]) -> None:
    """Apply checkpoint metadata to this session (in-place, idempotent).

    Only restores fields NOT already set by ``load_session`` from JSONL.
    ``_written_count`` and ``_compaction_count`` are **not** overwritten —
    JSONL is the authoritative source for message counts; checkpoint values
    are a snapshot that may be stale if JSONL was rebuilt.
    """
    self._created_at = data.get("created_at") or self._created_at
    self._last_active_at = data.get("last_active_at") or self._last_active_at
    if data.get("workspace_override") is not None:
        self.workspace_override = data["workspace_override"]
    rules = data.get("permission_rules", [])
    if rules:
        self.permission_store.restore_from_rules(rules)
```

**注意**：
- `_created_at` 仅在**首次** `save_checkpoint` 时写入（`save_checkpoint` 侧负责设置），后续调用不动它；`_last_active_at` 每次 `save_checkpoint` 更新
- `restore_checkpoint` **不恢复** `_written_count` 和 `_compaction_count` — 这两个计数由 `load_session` 从 JSONL 权威设置，checkpoint 中的值仅作快照参考，可能因 JSONL 重建而变得陈旧
- 因此 `load_session_full` 的调用顺序很重要：先 `load_session`（JSONL 权威），再 `restore_checkpoint`（叠加非 JSONL 元数据）

### 3.5 `persistence/session.py` — 读写 Checkpoint

**新增 import**：`from datetime import datetime`

```python
async def save_checkpoint(
    session: "SessionState",
    *,
    checkpoint_key: str | None = None,
    workspace_root: str | None = None,
    project_hash: str | None = None,
) -> bool:
    """Persist session metadata as .checkpoint.json alongside the .jsonl.

    幂等：同名文件直接覆盖。
    空 session（messages=[]）仍保存（允许恢复 workspace_override）。
    checkpoint_key 默认 = session.session_id。
    返回 True 表示写入成功。
    """
    if project_hash is None:
        project_hash = compute_project_hash(workspace_root)

    ckpt_key = checkpoint_key or session.session_id
    ckpt_path = checkpoint_json_path(project_hash, ckpt_key)

    # 确保目录存在
    os.makedirs(os.path.dirname(ckpt_path), exist_ok=True)

    # 更新时间戳（在 checkpoint_dict() 之前，只调用一次）
    now = datetime.now().isoformat()
    if session._created_at is None:
        session._created_at = now
    session._last_active_at = now

    async with aiofiles.open(ckpt_path, "w", encoding="utf-8") as fh:
        await fh.write(json.dumps(session.checkpoint_dict(), ensure_ascii=False, indent=2))
    return True


async def load_checkpoint(
    checkpoint_key: str,
    *,
    workspace_root: str | None = None,
    project_hash: str | None = None,
) -> dict[str, Any] | None:
    """Read .checkpoint.json, return raw dict or None."""
    if project_hash is None:
        project_hash = compute_project_hash(workspace_root)

    ckpt_path = checkpoint_json_path(project_hash, checkpoint_key)
    if not os.path.isfile(ckpt_path):
        return None

    async with aiofiles.open(ckpt_path, "r", encoding="utf-8") as fh:
        raw = await fh.read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Corrupted checkpoint: %s", ckpt_path)
        return None
```

**与 `SessionTranscriptWriter` 的交互**：当 QQBot session 被 evict 后重新创建、再次 `begin_persistence` 时，`SessionTranscriptWriter.open()` 会检测已有 JSONL 文件，自动恢复 `_last_uuid` 和 `_entry_count`，确保 parentUuid 链在跨生命周期重建中连续。这个机制是现有的（`storage.py:205-212`），checkpoint 无需介入。

### 3.6 `persistence/session.py` — `list_sessions()` 加速 + 双源扫描

```python
# 修改逻辑：
# 1. 同时扫描 .checkpoint.json 和 .jsonl，取并集（二者都可能孤立存在）
# 2. 有 checkpoint 时快速读取 metadata（O(1)），无 checkpoint 时回退 JSONL 扫描

def _list_all_session_keys(project_hash: str) -> set[str]:
    """扫描 project_dir，返回所有 session key（去掉后缀的并集）。"""
    d = project_dir(project_hash)
    if not os.path.isdir(d):
        return set()
    keys: set[str] = set()
    for name in os.listdir(d):
        if name.endswith(".checkpoint.json"):
            keys.add(name[:-len(".checkpoint.json")])
        elif name.endswith(".jsonl"):
            keys.add(name[:-len(".jsonl")])
    return keys

async def list_sessions(
    workspace_root: str | None = None,
) -> list[SessionInfo]:
    project_hash = compute_project_hash(workspace_root)
    all_keys = _list_all_session_keys(project_hash)
    if not all_keys:
        return []

    result: list[SessionInfo] = []
    for key in sorted(all_keys):
        # 1) 尝试从 checkpoint 快速读取
        ckpt = await load_checkpoint(key, project_hash=project_hash)
        if ckpt is not None:
            result.append(SessionInfo(
                session_id=key,
                title=ckpt.get("title", "(no title)"),
                created_at=ckpt.get("created_at", ""),
                last_activity=ckpt.get("last_active_at", ""),
                message_count=ckpt.get("message_count", 0),
                project_hash=project_hash,
                workspace_override=ckpt.get("workspace_override"),
            ))
            continue

        # 2) 无 checkpoint → 回退到 JSONL 扫描（现有逻辑）
        path = session_jsonl_path(project_hash, key)
        info = await _read_session_info(key, path, project_hash)
        if info is not None:
            result.append(info)

    result.sort(key=lambda x: x.last_activity, reverse=True)
    return result
```

**`SessionInfo` 新增字段**：`workspace_override: str | None = None`

**排序行为变更**：当前 `list_session_files()` 按文件 mtime 排序。新版改为按 `last_activity`（checkpoint 内存储的时间戳，或 JSONL 最后一条 entry 的 timestamp）排序。对于有 checkpoint 的 session，这比 mtime 更准确（mtime 可能因外部文件操作改变）；对于无 checkpoint 的旧 session，`last_activity` 来自 `_read_session_info` 的尾行扫描（行为不变）。

### 3.7 `persistence/session.py` — 增强恢复入口

```python
async def load_session_full(
    session_id: str,
    *,
    checkpoint_key: str | None = None,
    workspace_root: str | None = None,
    project_hash: str | None = None,
) -> "SessionState | None":
    """load_session() + checkpoint 恢复。

    1. 从 JSONL 恢复 messages + written_count + compaction_count
    2. 从 .checkpoint.json 恢复 workspace_override + permission_rules

    checkpoint_key 默认 = session_id。
    """
    session = await load_session(
        session_id,
        workspace_root=workspace_root,
        project_hash=project_hash,
    )
    if session is None:
        return None

    # 叠加 checkpoint 元数据
    ckpt_key = checkpoint_key or session_id
    ckpt = await load_checkpoint(
        ckpt_key,
        workspace_root=workspace_root,
        project_hash=project_hash,
    )
    if ckpt is not None:
        session.restore_checkpoint(ckpt)

    return session
```

**注意**：
- 不修改 `load_session()` 签名——旧调用者（如迁移工具、审计工具）不受影响
- 调用顺序有讲究：先 `load_session`（JSONL 设置 `_written_count`/`_compaction_count` 为权威值），再 `restore_checkpoint`（只叠加 `workspace_override`、`permission_rules`、时间戳等非 JSONL 元数据）。`restore_checkpoint` 不会覆盖 JSONL 设置的计数字段（见 3.4 节修订后的 docstring）
- 对于 QQBot：`checkpoint_key` 可能不等于 `session.session_id`（session_id 是 UUID，checkpoint_key 是 `"c2c_user1"`），这是因为 `load_session` 用 `session_id` 找 JSONL，而 checkpoint 用 `checkpoint_key` 找——在 QQBot 场景中二者是同一个 sanitized key（因为 evict 回调用 `checkpoint_key` 命名 JSONL）

### 3.8 `bridge/agent_bridge.py` — 回调钩子

```python
class AgentBridge:
    def __init__(self, ...):
        ...
        self._on_session_created: list[...] = []
        self._on_session_evicted: list[...] = []

    def on_session_created(self, callback):
        """注册：新 session 创建时调用（在 process() 的 lock 内 await）。"""
        self._on_session_created.append(callback)

    def on_session_evicted(self, callback):
        """注册：session 被超时清理/clear 前调用。"""
        self._on_session_evicted.append(callback)

    async def _fire_on_evicted(self, session_id: str, session: SessionState) -> None:
        """触发所有 evict 回调（异常隔离）。session 参数在 pop 前传入。"""
        for cb in self._on_session_evicted:
            try:
                await cb(session_id, session)
            except Exception as exc:
                logger.error("on_session_evicted callback failed: %s", exc)

    async def _fire_on_created(self, session_id: str, session: SessionState) -> None:
        """触发所有 create 回调（异常隔离）。"""
        for cb in self._on_session_created:
            try:
                await cb(session_id, session)
            except Exception as exc:
                logger.error("on_session_created callback failed: %s", exc)

    def _get_or_create_session(self, session_id: str) -> tuple[SessionState, bool]:
        """返回 (session, is_new)。

        超时分支在 pop 前触发 evict 回调（fire-and-forget）。
        is_new=True 时，调用者应在 lock 内 await _fire_on_created。
        """
        now = time.time()
        is_new = False

        if session_id in self._sessions:
            last = self._sessions_last_access.get(session_id, 0.0)
            if now - last > self._session_timeout:
                old_session = self._sessions.pop(session_id, None)
                self._session_locks.pop(session_id, None)
                self._sessions_last_access.pop(session_id, None)
                if old_session is not None:
                    asyncio.ensure_future(self._fire_on_evicted(session_id, old_session))
                logger.info(f"Session {session_id[:16]}... expired and removed")

        if session_id not in self._sessions:
            self._sessions[session_id] = SessionState()
            self._session_locks[session_id] = asyncio.Lock()
            is_new = True
        self._sessions_last_access[session_id] = now
        return self._sessions[session_id], is_new

    def clear_session(self, user_id: str, open_kfid: str) -> None:
        """用户发送 /reset 或结束客服会话时清除会话。"""
        sid = self.session_id(user_id, open_kfid)
        old = self._sessions.pop(sid, None)
        self._session_locks.pop(sid, None)
        self._sessions_last_access.pop(sid, None)
        if old is not None:
            asyncio.ensure_future(self._fire_on_evicted(sid, old))
```

**process() 变更**：

```python
async def process(self, user_id, open_kfid, content, msg_type):
    sid = self.session_id(user_id, open_kfid)
    session, is_new = self._get_or_create_session(sid)
    lock = self._session_locks[sid]

    async with lock:
        # 新 session：在 lock 内 await create 回调（保证 checkpoint 在 Agent 之前恢复）
        if is_new:
            await self._fire_on_created(sid, session)

        # ... 其余不变：build prompt, Agent(), stream, split ...
```

**get_session() 变更**（供 `/cd`、`/ls`、`/clear` 等同步查询用，不需要 create 回调）：

```python
def get_session(self, user_id: str, open_kfid: str) -> SessionState:
    sid = self.session_id(user_id, open_kfid)
    session, _ = self._get_or_create_session(sid)
    return session
```

**注意**：
- Evict 用 `asyncio.ensure_future` fire-and-forget（checkpoint 保存失败不影响 pop）
- Create 回调在 `process()` 的 lock 内 `await`（保证第一个消息在 checkpoint 恢复后才处理）
- `get_session()`（被 shell 指令调用）不触发 create 回调——shell 指令不需要 checkpoint 恢复。**已知限制**：如果用户重启后的**第一条消息**是 shell 命令（如 `/ls`、`/clear`），checkpoint 不会被恢复。对于 `/cd`（设置新路径）和 `/help`（无状态）无影响；对于 `/ls`，可能显示错误的目录。这属于边界情况，可接受——绝大多数用户的第一条消息是文本
- **并发注意**：`_get_or_create_session` 在 lock 外执行，两个并发请求可能同时创建 SessionState 并同时标记 `is_new=True`。`restore_checkpoint` 是幂等的（`data.get("created_at") or self._created_at`），重复调用无害

### 3.9 `channels/qq_bot/channel.py` — QQBot 接入

```python
@staticmethod
def _checkpoint_key(bridge_sid: str) -> str:
    """将 bridge session_id 转换为安全的文件名 key。"""
    return bridge_sid.replace(":", "_").replace("\\", "_").replace("/", "_")

def __init__(self, config: QQBotConfig):
    ...
    self._bridge.on_session_created(self._on_qqbot_session_created)
    self._bridge.on_session_evicted(self._on_qqbot_session_evicted)

async def _on_qqbot_session_created(self, sid: str, session: SessionState) -> None:
    """从 checkpoint 恢复 QQBot session。"""
    ckpt = await load_checkpoint(
        self._checkpoint_key(sid),
        workspace_root=self._agent_config.workspace_root,
    )
    if ckpt is not None:
        session.restore_checkpoint(ckpt)

async def _on_qqbot_session_evicted(self, sid: str, session: SessionState) -> None:
    """QQBot session 超时/清理前保存 checkpoint + JSONL。"""
    if not session.messages:
        return  # 空 session 不存档

    ck_key = self._checkpoint_key(sid)
    # 1) 保存 checkpoint
    await save_checkpoint(
        session,
        checkpoint_key=ck_key,
        workspace_root=self._agent_config.workspace_root,
    )
    # 2) 确保 JSONL 也持久化（用 checkpoint_key 命名，与 checkpoint 并列）
    if session.transcript_path is None:
        from ..persistence.path import compute_project_hash, session_jsonl_path
        ph = compute_project_hash(self._agent_config.workspace_root)
        await session.begin_persistence(session_jsonl_path(ph, ck_key))
    await session.end_persistence()
```

> **注意**：`/cd` 和 `/cd --reset` 指令在 `_handle_cd_command` 中会额外调用 `save_checkpoint` 即时存档 workspace_override（见 3.11 节）。即使这条 `/cd` 消息是用户的首条消息（在 `on_session_created` 之后），即时存档也会覆盖之前可能恢复的旧 workspace_override，保证状态一致。

### 3.10 `cli/_main.py` — CLI 交互式恢复

##### 3.10.1 交互式会话选择

```python
async def _interactive_session_picker(config: AgentConfig) -> SessionState | None:
    """显示最近会话列表，让用户选择继续或新建。
    
    Returns:
        SessionState if user chose to resume, None to start fresh.
    """
    sessions = await list_sessions(config.workspace_root)
    if not sessions:
        return None

    print("\n  最近会话:")
    for i, s in enumerate(sessions[:10], 1):  # 最多显示 10 条
        ws_info = ""
        if s.workspace_override:
            # 显示相对于 workspace_root 的路径
            root = config.workspace_root or os.getcwd()
            try:
                rel = os.path.relpath(s.workspace_override, root)
                ws_info = f", {rel}/"
            except ValueError:
                ws_info = f", {s.workspace_override}"
        print(f"  [{i}] {s.title:<32s} {s.last_activity[:19]}  "
              f"({s.message_count} msgs{ws_info})")

    print(f"\n  输入编号继续，或直接回车开始新会话 > ", end="")
    choice = input().strip()

    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(sessions):
            s = sessions[idx]
            session = await load_session_full(
                s.session_id, workspace_root=config.workspace_root
            )
            if session is not None:
                # 重新挂载 persistence
                from ..persistence.path import compute_project_hash, session_jsonl_path
                ph = compute_project_hash(config.workspace_root)
                tpath = session_jsonl_path(ph, session.session_id)
                await session.begin_persistence(tpath)
                return session

    return None  # 新建
```

##### 3.10.2 `/quit` 自动存档

```python
# 在 _repl() 的 finally 块（或 /quit 分支）中：
await save_checkpoint(session, workspace_root=config.workspace_root)
await session.end_persistence()
```

##### 3.10.3 `/checkpoint` 指令

```python
# 新增斜杠指令处理：
if prompt.strip() == "/checkpoint":
    await save_checkpoint(session, workspace_root=config.workspace_root)
    print(f"[checkpoint saved — {len(session.messages)} messages]")
    continue
```

##### 3.10.4 `--continue-last` 增强

使用 `load_session_full()` 替代 `load_session()` 以恢复 `workspace_override` 和 `permission_rules`。

### 3.11 QQBot `/cd` 后即时存档

`/cd`（切换路径）和 `/cd --reset`（重置路径）都会变更 `workspace_override`，但这两个操作不经过 Agent（不触发 `flush_new_messages`）。如果进程在这时崩溃，变更丢失。

**触发点**：`_handle_cd_command` 中，在 `workspace_override` 变更后、发送回复前。

```python
# _handle_cd_command 中，在 workspace_override 变更后调用：
async def _save_workspace_checkpoint(
    self, user_id: str, channel_id: str, session: SessionState,
) -> None:
    """/cd 或 /cd --reset 后即时保存 workspace_override 到 checkpoint。"""
    bridge_sid = self._bridge.session_id(user_id, channel_id)
    try:
        await save_checkpoint(
            session,
            checkpoint_key=self._checkpoint_key(bridge_sid),
            workspace_root=self._agent_config.workspace_root,
        )
    except Exception:
        logger.exception("Failed to save checkpoint after /cd")
```

原因：即时存档成本极低（几个 KB JSON），可接受。

---

## 4. 边界条件与测试覆盖

### 4.1 Checkpoint 数据完整性

| 场景 | 预期行为 |
|------|----------|
| 空 session 存档 | 正常保存（`messages=[]`、`workspace_override=None`） |
| 含 permission 的 session | `permission_rules` 完整序列化 → 恢复后 `PermissionEngine` 正确匹配 |
| `workspace_override` 为 `None` | checkpoint 中字段为 `null`，恢复后保持不变 |
| checkpoint 文件损坏 | `load_checkpoint()` 返回 `None`，`load_session_full()` 降级为纯 JSONL 恢复 |
| checkpoint 不存在 | 等同于纯 JSONL 恢复（现有行为） |
| `begin_persistence` 后立即 `save_checkpoint` | `written_count` 一致，不重复写入 |

### 4.2 CLI 交互式选择

| 场景 | 预期行为 |
|------|----------|
| 无历史 session | 直接进入新会话，无提示 |
| 1 个历史 session | 显示 1 条，用户可选或跳过 |
| >10 个历史 session | 只显示最近 10 条 |
| 用户输入无效编号 | 提示重试或开始新会话 |
| 用户直接回车 | 开始新会话 |
| 选择的 session JSONL 丢失 | 降级：提示 "session data missing, starting fresh" |

### 4.3 QQBot 自动恢复

| 场景 | 预期行为 |
|------|----------|
| 超时后用户再次发消息 | `_get_or_create_session` 创建新 session → `on_session_created` 回调尝试加载 checkpoint → 恢复 workspace_override |
| 超时时 session 为空 | `on_session_evicted` 不保存 checkpoint（`messages=[]` 跳过） |
| `allow_shell_commands=False` 时 | 仍正常存档/恢复（checkpoint 与权限无关） |
| checkpoint 中 `workspace_override` 已不存在的目录 | `/cd` 或 `/ls` 时 `_resolve_path` 检查发现 `os.path.isdir()` 失败 → 报错；不崩溃 |

### 4.4 并发安全

| 场景 | 预期行为 |
|------|----------|
| Agent 推理中 + 同时超时清理 | 已有 per-session Lock 保护，checkpoint 在 lock 外但数据已在 `flush_new_messages` 后一致 |
| 双用户同时 `/quit` | 各自 session 独立，无竞态 |

### 4.5 测试计划

#### `tests/unit/test_session_state.py`（+5）

```python
def test_checkpoint_dict_contains_all_fields():
    """checkpoint_dict() 包含所有关键字段。"""

def test_checkpoint_dict_extracts_title_from_first_user_msg():
    """checkpoint_dict() 从第一条 user 消息提取 title（截断 100 字符）。"""

def test_restore_checkpoint_applies_workspace_override():
    """restore_checkpoint() 正确恢复 workspace_override。"""

def test_restore_checkpoint_applies_permission_rules():
    """restore_checkpoint() 正确恢复 permission_rules。"""

def test_restore_checkpoint_idempotent():
    """重复 restore_checkpoint() 不会重复添加 permission_rules。"""
```

#### `tests/unit/test_persistence_session.py`（新增，+8）

```python
class TestCheckpoint:
    async def test_save_and_load_checkpoint_roundtrip(self, tmp_path):
        """保存 → 加载 → 字段一致。"""

    async def test_load_checkpoint_missing_file(self):
        """不存在的 checkpoint 返回 None。"""

    async def test_load_checkpoint_corrupted(self, tmp_path):
        """损坏的 JSON 返回 None。"""

    async def test_load_session_full_with_checkpoint(self, tmp_path):
        """load_session_full() 恢复 messages + workspace_override + permission。"""

    async def test_list_sessions_union_of_jsonl_and_checkpoint(self, tmp_path):
        """list_sessions() 联合扫描 .jsonl 和 .checkpoint.json（彼此都可能孤立）。"""

    async def test_list_sessions_checkpoint_fast_path(self, tmp_path):
        """有 checkpoint 时 list_sessions() 跳过 JSONL 扫描。"""

    async def test_load_session_full_with_checkpoint_key_mismatch(self, tmp_path):
        """checkpoint_key ≠ session_id 时 load_session_full() 仍正确恢复。"""

    async def test_save_checkpoint_created_at_stable(self, tmp_path):
        """多次 save_checkpoint 不改变 created_at，只更新 last_active_at。"""
```

#### `tests/unit/bridge/test_agent_bridge.py`（+4）

```python
class TestCheckpointCallbacks:
    async def test_on_session_created_callback(self):
        """新 session 创建时回调被调用。"""

    async def test_on_session_evicted_callback_on_timeout(self):
        """超时清理时 evict 回调在 pop 前被调用。"""

    async def test_on_session_evicted_callback_on_clear(self):
        """clear_session 时 evict 回调被调用。"""

    async def test_checkpoint_callbacks_exception_isolation(self):
        """单个回调抛异常不影响其他回调执行。"""
```

#### `tests/unit/qq_bot/test_channel.py`（+4）

```python
class TestCdCommand:
    async def test_cd_saves_checkpoint(self):
        """/cd 后 checkpoint 已保存 (workspace_override 非 null)。"""

    async def test_cd_reset_saves_checkpoint(self):
        """/cd --reset 后 checkpoint 已保存 (workspace_override 为 null)。"""

class TestCheckpointKey:
    def test_checkpoint_key_sanitize(self):
        """_checkpoint_key 正确替换 :、\\、/ 为 _。"""

class TestQqBotCheckpoint:
    async def test_session_create_loads_checkpoint(self):
        """session 创建时自动从 checkpoint 恢复 workspace_override。"""
```

---

## 5. 实现顺序

| 阶段 | 内容 | 预估 |
|------|------|------|
| **Phase 1** | `persistence/path.py` +`checkpoint_json_path`；`session/state.py` +`_created_at`/`_last_active_at`字段、`checkpoint_dict`/`restore_checkpoint`；`permissions/store.py` +`to_rules_list`/`restore_from_rules` | 0.5h |
| **Phase 2** | `persistence/session.py` +`save_checkpoint`/`load_checkpoint`/`load_session_full`；改 `list_sessions()`（双源扫描+`_list_all_session_keys`）；`SessionInfo` +`workspace_override` | 0.75h |
| **Phase 3** | `bridge/agent_bridge.py` +回调注册+`_fire_on_evicted`/`_fire_on_created`+超时路径触发；`channels/qq_bot/channel.py` 接入（含 JSONL 用 checkpoint_key 命名、/cd --reset 即时存档） | 0.75h |
| **Phase 4** | `cli/_main.py` 交互式选择器、`/checkpoint`、`/quit` 存档、`--continue-last` 增强 | 1h |
| **Phase 5** | 测试：`test_session_state` +5、`test_persistence_session` +8、`test_agent_bridge` +4、`test_channel` +4 | 2h |
| **合计** | | **5h** |

---

## 6. 不做的

- ❌ **不自动保存每次消息后的 checkpoint** — I/O 开销无必要
- ❌ **不修改 JSONL 格式或 TranscriptEntry** — 向后兼容
- ❌ **不引入新依赖** — 全部用 stdlib `json` + 已有 `aiofiles`
- ❌ **不支持 checkpoint 迁移/版本兼容** — v1 格式足够简单，未来 breaking change 时直接忽略旧文件（数据仍在 JSONL）
- ❌ **不在 CLI 显示超过 10 条历史会话** — 可通过 `/sessions` 查看完整列表
- ❌ **QQBot 不做每次消息后的增量 JSONL 持久化** — JSONL 仅在 evict 时全量写入；若进程在 evict 前崩溃，消息丢失（checkpoint 元数据不受影响，但对话转录不完整）。这是 QQBot 的已知限制，本计划不解决它
- ❌ **QQBot shell 命令（/ls、/clear）作为首条消息时不做 checkpoint 恢复** — `get_session()` 不触发 create 回调。对 `/cd`（会设置新 workspace_override）和 `/help`（无状态）无影响，`/ls` 可能短暂显示错误目录。绝大多数用户首条消息是文本，此边界情况影响极小

---

## 7. 风险与缓解

| 风险 | 缓解 |
|------|------|
| checkpoint 与 JSONL 不同步 | checkpoint 仅存 metadata，JSONL 是真相源；`load_session_full()` 先 `load_session`（JSONL 权威），再 `restore_checkpoint`（仅叠加非 JSONL 元数据）。`restore_checkpoint` 不覆盖 `_written_count`/`_compaction_count` |
| `on_session_evicted` 回调抛异常阻塞清理 | 回调用 `try/except` 包裹，异常记日志不传播 |
| `list_sessions()` 中 checkpoint 与 JSONL `message_count` 不一致 | checkpoint 的计数是快照时刻的值，允许微小偏差；`load_session_full()` 以 JSONL 恢复的实际 message_count 为准 |
| QQBot `session_id` 格式含 `:` 不能做文件名 | `_checkpoint_key()` 方法 sanitize：`replace(":", "_").replace("\\", "_").replace("/", "_")`，暴露在 `persistence/path.py` 之外，不污染底层 API |
| QQBot checkpoint key 冲突（两个不同项目、同一用户） | `checkpoint_json_path` 包含 `project_hash()`，不同项目天然隔离 |
| 双请求并发创建同一 session，`is_new` 被重复判定 | `restore_checkpoint` 是幂等的（`or self._created_at`），重复调用无害。`_get_or_create_session` 的竞态是预存问题，本计划不引入新竞态 |
| checkpoint 文件保存中途崩溃 | 小文件（<1KB），`aiofiles` + `flush` 原子性足够；最坏情况：损坏的 checkpoint → `load_checkpoint` 返回 `None`，降级为纯 JSONL 恢复 |
