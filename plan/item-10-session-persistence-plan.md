# Item 10: 会话持久化 & 恢复 (Phase B) — 详细实现计划

> **状态**: Phase A 已完成（`SessionState` + 跨 prompt 历史 + `DoneMessage.final_messages` 回吐 + Agent/CLI 注入链），Phase B 待实现。
> **预估**: 3 天（~23 工时，原 16h 偏乐观）— 含问题 #2/#3/#5 的连带改动：
>   sidechain unwrap、压缩 checkpoint 分段链（保留压缩前完整 transcript）+ LIVE/FULL
>   双路恢复、session→ToolUseContext 传递链。Step 拆解相加原为 ~16h 且零 buffer，
>   已上调（见 §5）。
> **前置依赖**: Phase A（已满足）— `SessionState.messages` 是落盘的唯一事实源。
> **参考实现**: cc-haha (`sessionStorage.ts`, `conversationRecovery.ts`)
> **关联计划**: [session-state-and-history-plan.md](./session-state-and-history-plan.md) §3、[permission-session-scope-plan.md](./permission-session-scope-plan.md)

---

## 0′. 评审发现 & 修订记录（基于现有代码核对）

> 下表为对照 `session/state.py`、`agents/fork.py`、`agents/run.py`、`agents/tool.py`、
> `tools/base.py`、`agent.py`、`compression/` 后发现的问题。文中相关小节已就地标注
> 「（问题 #N）」并给出修正。**🔴 = 会导致数据损坏/功能失效，必须改**。

| # | 严重度 | 问题 | 修正所在 |
|---|--------|------|----------|
| 1 | 🔴 | `begin_persistence` 在 `--resume` 用同一 session_id/同一文件、'a' 模式把历史全量 append 一遍 → 文件翻倍且无法回滚 | §3.1、§2.4(load_session 预置 `_written_count`) |
| 2 | 🔴 | sidechain 改 `TranscriptEntry` 信封后，`fork.py:_load_sidechain_messages` 仍把整行当裸 message 喂给 `run_agent`，恢复的子 Agent 读坏 | §3.3.2.b、风险表 |
| 3 | 🔴 | 增量写 `final_messages[_written_count:]` 与 in-loop compaction 冲突（`compression/` 确实会重写/缩短列表），JSONL 与内存永久分叉。**方案：append-only checkpoint 分段链，保留压缩前完整 transcript**（非整段重写） | §2.2 `CompactionRecord`/写入算法、§2.3.5 LIVE 恢复、§2.3.7 FULL 审计、§3.1、不变量 #8/#9 |
| 4 | 🟠 | 'a' 模式缺尾换行守卫：crash 留半行后下次 append 拼成无法解析的合并行（损坏两行） | §2.2 `open()` |
| 5 | 🟠 | session_id/project_hash → `run_agent` 的传递链整段缺失：`ToolUseContext` 无 session 字段、query 未注入；Step 9 仅一句带过、零工时 | §3.3.2.a、Step 9 |
| 6 | 🟠 | `migrate_transcript` 无接入点，主流程从不调用 → 悬空死代码 | §2.4 load_session 第 2 步、Step 4 |
| 7 | 🟠 | `list_sessions` 声称「只 seek 首末行」却又要精确 `message_count`/`has_unfinished_turn`（需读全文件）——自相矛盾 | §2.4、风险表 |
| 8 | 🟡 | `filter_orphaned_thinking` 朴素规则会误删 `thinking+tool_use` 消息→孤儿 tool_result；且 thinking 启用态有 signature 保留约束 | §2.3.3 |
| 9 | 🟡 | v1 严格线性下 `walk_entry_chain` 的 parentUuid 回溯是纯开销且易走偏；应直接线性读 | §2.3.1 |
| 10 | 🟡 | resume 丢弃 `SessionPermissionStore`，用户被重新弹窗——与「恢复」语义冲突，需 UX 明示 | §2.4 第 7 步、Step 10 |
| 11 | 🟡 | 文件名不一致：§1 图作 `transcript.py`，§2.2 为 `storage.py` | §1（已统一为 storage.py） |
| 12 | 🟡 | 工时「2 天/16h」零 buffer 且未含 #2/#3/#5 连带改动，偏乐观 | 顶部预估、§5 各 Step |

---

## 0. 当前状态摘要

### 已具备的基础设施

| 组件 | 文件 | 状态 |
|------|------|------|
| `SessionState` (messages + permission_store + transcript_path) | `session/state.py` | ✅ 已实现 |
| `DoneMessage.final_messages` 回吐 | `models.py:449-457` | ✅ 已实现 |
| `Agent(config, session=)` 注入 + 回写 | `agent.py:60-90` | ✅ 已实现 |
| `cli._repl` 循环外 `SessionState()` + `/clear` | `cli.py:316-375` | ✅ 已实现 |
| `SessionPermissionStore` 会话权限记忆 | `permissions/store.py` | ✅ 已实现 |
| 子 Agent sidechain JSONL 写入 | `agents/run.py:202-205,316-368` | ✅ 已实现 (需适配路径) |
| `filter_incomplete_tool_calls()` (fork 清洗) | `agents/fork.py:35-81` | ✅ 可复用 |
| `_load_sidechain_messages()` (JSONL 读取) | `agents/fork.py:165-184` | ✅ 可复用参考 |

### 关键差异：现有 sidechain vs 目标格式

**现有 sidechain 格式** (`run.py:338-344`):
```json
{"role": "assistant", "content": [...], "timestamp": "ISO8601", "turn": 0}
{"role": "user", "content": [...], "timestamp": "ISO8601", "turn": 0}
```
- 路径: `~/.agent/sessions/{parent_trace_id}/{agent_id}.jsonl`
- 没有 `uuid` / `parentUuid` / `sessionId` / `isSidechain` / `version` 元数据
- 子 Agent 专用，不覆盖主会话

**目标 cc-haha 格式**:
```json
{"uuid": "abc123", "parentUuid": "def456", "sessionId": "session-1", "timestamp": "2026-06-25T10:00:00Z", "isSidechain": true, "version": 1, "message": {"role": "assistant", "content": [...]}}
```
- 路径: `~/.agent/projects/{project_hash}/{session_id}.jsonl` (主会话)
- 路径: `~/.agent/projects/{project_hash}/{session_id}/{agent_id}.jsonl` (sidechain)
- 统一的 `TranscriptEntry` 包装，`parentUuid` 链支持 fork

---

## 1. 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                      cli.py / SDK                           │
│  --resume <id>  --continue  /sessions  /clear               │
└──────────────┬──────────────────────────────────────────────┘
               │ creates / loads
               ▼
┌─────────────────────────────────────────────────────────────┐
│                    SessionState                             │
│  session_id, messages, permission_store                     │
│  transcript_path: str | None                                │
│  _writer: SessionTranscriptWriter | None   ← NEW            │
│                                                             │
│  begin_persistence(path) → None            ← NEW            │
│  end_persistence() → None                  ← NEW            │
└──────────────┬──────────────────────────────────────────────┘
               │ hooks write on record_turn_result()
               ▼
┌─────────────────────────────────────────────────────────────┐
│              persistence/storage.py                         │
│  SessionTranscriptWriter (aiofiles append + write_checkpoint)│
│  TranscriptEntry (message | compaction) + CompactionRecord  │
└─────────────────────────────────────────────────────────────┘
               │ reads back
               ▼
┌─────────────────────────────────────────────────────────────┐
│              persistence/recovery.py                        │
│  reconstruct_live_messages() → 分段链 LIVE 活跃列表          │
│  reconstruct_full_history() → 保留压缩前完整 transcript(审计) │
│  walk_entry_chain() → chain-walk by parentUuid (审计/fork)   │
│  filter_unresolved_tool_uses() → 丢弃未配对 tool_use         │
│  filter_orphaned_thinking() → 丢弃孤立 thinking              │
│  detect_turn_interruption() → 检测 mid-turn 中断             │
│  reconstruct_messages() → LIVE + 清洗包装                    │
└─────────────────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────┐
│              persistence/session.py                         │
│  list_sessions() → SessionInfo 列表                         │
│  load_session() → SessionState (含恢复)                      │
│  delete_session() → 清 JSONL + sidechain 子目录              │
│  SessionInfo: id, title, created_at, last_activity, count   │
└─────────────────────────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────────┐
│              persistence/migrate.py                         │
│  migrate_transcript(path) → version 驱动自动升级              │
│  CURRENT_VERSION = 1                                        │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. 详细文件设计

### 2.1 `persistence/path.py` — 存储路径计算

**职责**: 纯函数，无副作用。所有路径计算集中于此，其余模块通过它获取路径。

```python
# ---- 常量 ----
AGENT_HOME = "~/.agent"
PROJECTS_DIR = "projects"
CURRENT_VERSION = 1

# ---- 函数 ----

def compute_project_hash(workspace_root: str | None = None) -> str:
    """SHA-256 前 16 位 hex → 短哈希。
    
    优先级: workspace_root → os.getcwd()。
    对绝对路径的 utf-8 字节求 SHA-256，取前 16 字符。
    """

def project_dir(project_hash: str) -> str:
    """~/.agent/projects/{project_hash}/"""

def session_jsonl_path(project_hash: str, session_id: str) -> str:
    """~/.agent/projects/{project_hash}/{session_id}.jsonl"""

def sidechain_dir(project_hash: str, session_id: str) -> str:
    """~/.agent/projects/{project_hash}/{session_id}/"""

def sidechain_jsonl_path(project_hash: str, session_id: str, agent_id: str) -> str:
    """~/.agent/projects/{project_hash}/{session_id}/{agent_id}.jsonl"""

def list_session_files(project_hash: str) -> list[str]:
    """按 mtime 降序返回 {session_id}.jsonl 文件名列表。
    不存在的 project_hash → 返回 []。
    """
```

**设计要点**:
- `project_hash` 从 `workspace_root` 计算，CLI 中 `workspace_root` 默认为 `AgentConfig.workspace_root`（可能为 `None` → fallback 到 `os.getcwd()`）
- `agent_id` 在子 Agent 创建时生成（`uuid.uuid4().hex`），沿用现有 `run.py:107` 逻辑
- 所有路径函数在调用前 `os.makedirs` 父目录（由 writer / loader 负责）

### 2.2 `persistence/storage.py` — JSONL 读写

**职责**: 定义 `TranscriptEntry` 数据模型 + `SessionTranscriptWriter` 写入器 + 读取辅助函数。

#### 数据模型

```python
class TranscriptEntry(BaseModel):
    """单条 JSONL entry — 与 cc-haha 完全兼容。"""
    uuid: str                              # uuid4().hex
    parentUuid: str | None = None          # 上一条 entry 的 uuid（v1 线性）
    sessionId: str                         # SessionState.session_id
    timestamp: str                         # ISO 8601 (UTC)
    isSidechain: bool = False              # True → 子 Agent 条目
    version: int = 1                       # 格式版本号
    # ---- entry 类别判别（问题 #3：分段链 / checkpoint）----
    type: Literal["message", "compaction"] = "message"  # 默认普通消息
    message: dict[str, Any] | None = None  # type="message" 时为 {role, content}
    compaction: CompactionRecord | None = None  # type="compaction" 时的 checkpoint 载荷

    @classmethod
    def from_message(
        cls,
        message: dict[str, Any],
        session_id: str,
        parent_uuid: str | None = None,
        is_sidechain: bool = False,
    ) -> "TranscriptEntry": ...

    @classmethod
    def from_checkpoint(
        cls,
        record: "CompactionRecord",
        session_id: str,
        parent_uuid: str | None = None,
    ) -> "TranscriptEntry": ...   # type="compaction"，message=None
```

#### CompactionRecord — checkpoint 载荷（问题 #3：保留压缩前完整 transcript）

```python
class CompactionRecord(BaseModel):
    """一次 in-loop 压缩的边界检查点。

    设计目标：**append-only，绝不重写/删除压缩前的原始 entries**。压缩发生时
    只 append 一条 type="compaction" 的 checkpoint entry，把「压缩后的 live 列表」
    作为自包含快照存入 `snapshot`；其前的原始消息 entries 原样留在磁盘上供审计。

    - LIVE 恢复：取「最后一个 checkpoint 的 snapshot」+ 其后追加的普通消息 entries。
    - FULL 审计：所有原始 message entries 全部保留，checkpoint 标记每段边界。
    """
    segment_index: int                      # 第几次压缩（1-based）
    snapshot: list[dict[str, Any]]          # 压缩后完整 live 列表 = summary + 保留尾部
    summary_count: int                      # snapshot 前缀中属于「摘要」的消息条数
    kept_count: int                         # snapshot 尾部「原样保留」的消息条数
    # 审计映射：本次 summary 取代了哪些原始 entries（连续区间，闭区间 uuid）
    summarized_uuid_first: str | None = None
    summarized_uuid_last: str | None = None
    summarized_count: int = 0               # 被摘要替换掉的原始 live 消息数
    prev_checkpoint_uuid: str | None = None # 上一个 checkpoint 的 uuid（segment 链）
    # 可选会计
    trigger: str = "token_threshold"        # 触发原因
    tokens_before: int | None = None
    tokens_after: int | None = None
```

> **不变量**：每个 segment 的原始 entries 不可变；checkpoint 只追加。`snapshot`
> 自包含（含保留尾部的副本），使 LIVE 恢复非递归、O(1) 定位最后一段。代价是
> 保留尾部被复制一份（有界，≈ 最近 N 条），换取审计完整性与恢复简单性。

#### SessionTranscriptWriter

```python
class SessionTranscriptWriter:
    """Append-only JSONL writer，每个 SessionState 最多一个实例。
    
    写操作是同步 append + flush（通过 aiofiles），保证 crash-consistency：
    最多丢失最后一行未 flush 的 JSON。
    """

    def __init__(self, path: str, session_id: str) -> None: ...
    
    async def open(self) -> None:
        """打开 aiofiles 文件句柄（'a' 模式），确保父目录存在。

        ⚠ 崩溃健壮性（问题 #4）：进程若在写某行中途崩溃，会留下无尾随
        '\\n' 的半行。直接 'a' append 会把新内容拼到半行后 → 产生一条
        无法解析的合并行（损坏两行而非一行）。因此 open() 必须：
          1. 若文件非空且最后一个字节不是 '\\n' → 先 append 一个 '\\n'
             （把残缺半行单独隔离，load_entries 后续会作为坏行跳过）；
          2. 再进入正常 append。
        这样可兑现不变量 #1「最多丢失最后一行」。"""
    
    async def write_entry(self, message: dict[str, Any], *, is_sidechain: bool = False) -> str:
        """将一条 message 序列化为 TranscriptEntry 并 append 到 JSONL。
        
        自动生成 uuid，设置 parentUuid 为上次写入的 uuid（v1 线性链）。
        返回本条 entry 的 uuid。
        """

    async def write_checkpoint(self, record: CompactionRecord) -> str:
        """append 一条 type="compaction" 的 checkpoint entry（问题 #3）。

        **纯 append，绝不重写/截断已有内容** —— 压缩前的原始 entries 保持不动。
        parentUuid = 上次写入的 uuid（checkpoint 也在同一条线性链上，保证 FULL
        审计可线性走完）。其后的普通消息 entries 的 parentUuid 接到本 checkpoint。
        返回本 checkpoint 的 uuid。

        调用方在调用前应填好 record.prev_checkpoint_uuid /
        summarized_uuid_first/last（可由 writer 内部依据 last_uuid 与水位辅助计算）。
        """
    
    async def flush(self) -> None: ...
    
    async def close(self) -> None:
        """flush + close 文件句柄。幂等（已关闭则无操作）。"""
    
    @property
    def is_open(self) -> bool: ...
    
    @property
    def entry_count(self) -> int: ...
    
    @property
    def last_uuid(self) -> str | None: ...

    @property
    def last_checkpoint_uuid(self) -> str | None:
        """最近写入的 checkpoint uuid（供下一个 CompactionRecord 串 segment 链）。"""
```

**写入时机**: 不在 `query()` 内部逐条写（那会侵入 turn loop），而是在 `SessionState.record_turn_result()` 中批量写新消息。

**写入算法（增量 + 压缩感知，append-only 分段链）**:
```
flush_new_messages(final_messages):
    if self._writer is None:
        return
    # ⚠ 关键约束（问题 #3）：final_messages 已「reflects any in-loop
    # compression」（见 state.py:45 文档）。query loop 触发压缩时，消息列表会被
    # 「重写/缩短」（head 被摘要替换），纯切片 final_messages[_written_count:]
    # 会漏写/错写，使 append-only 的 JSONL 与内存列表永久分叉。
    if self._is_compaction(final_messages):
        record = self._build_compaction_record(final_messages)  # 见下
        await self._writer.write_checkpoint(record)   # 纯 append，不动旧 entries
        self._written_count = len(final_messages)     # 水位 = 新 live 列表长度
    else:
        for msg in final_messages[self._written_count:]:
            await self._writer.write_entry(msg)
        self._written_count = len(final_messages)
    self.messages = list(final_messages)
```

**压缩检测**（`_is_compaction`）：已落盘前缀 `self.messages[:_written_count]` 是否仍是
`final_messages` 的逐条前缀；长度缩短或前缀被改写（head 变摘要）→ 判定为压缩。

**`_build_compaction_record(final_messages)`**：构造 `CompactionRecord`：
- `snapshot = final_messages`（压缩后完整 live 列表 = summary + 保留尾部，自包含）
- `summary_count` / `kept_count`：由「检测到的改写边界」推出——`self.messages` 与
  `final_messages` 的最长公共尾部即保留尾部 `kept_count`，`snapshot` 余下前缀即摘要
- `summarized_count = self._written_count - kept_count`（被摘要替换掉的原始 live 数）
- `summarized_uuid_first/last`：被替换区间对应的磁盘 entry uuid（writer 按水位映射）
- `prev_checkpoint_uuid = writer.last_checkpoint_uuid`
- `segment_index = 上次 +1`

> **append-only 保证（问题 #3 / 不变量 #1、#8）**：压缩**不**重写整个 JSONL，只
> 追加一条 checkpoint。压缩前的原始 entries 永久保留 → 满足「保留压缩前完整
> transcript」。崩溃语义不变（最多丢最后一行）。代价：保留尾部在 checkpoint
> snapshot 中复制一份（有界）。详见 §2.3.5 LIVE 恢复 与 §2.3.7 FULL 审计。

**磁盘布局示例**（段 0 写 10 条 → 压缩保留尾 2 条 → 再写 2 条）：
```
E0 E1 E2 E3 E4 E5 E6 E7 E8 E9      ← 段0：10 条原始 message entry（_written_count=10）
CK1                                ← checkpoint：snapshot=[S, E8, E9]（S=摘要）, kept=2,
                                      summarized=[E0..E7]（summarized_uuid_first=E0, last=E7）
E10 E11                            ← 段1：压缩后新增的 2 条（_written_count: 3→4→5）

LIVE 恢复  = CK1.snapshot + [E10,E11] = [S, E8, E9, E10, E11]   （5 条，无重复）
FULL 审计  = 段0[E0..E9] +（CK1: E0..E7→S）+ 段1[E10,E11]        （原始全保留）
磁盘水位    _written_count = len(snapshot)+2 = 5  ←→ LIVE 长度 5  （对齐）
```

#### 读取辅助函数

```python
async def load_entries(path: str) -> list[TranscriptEntry]:
    """逐行读取 JSONL，解析为 TranscriptEntry 列表。
    非法行（JSON decode error）跳过并 warning 日志。
    文件不存在 → 返回 []。
    """

async def save_entries(path: str, entries: list[TranscriptEntry]) -> None:
    """全量覆写（用于迁移）。先写临时文件，再 os.replace 原子替换。"""
```

### 2.3 `persistence/recovery.py` — 恢复 + 清洗

**职责**: 对标 cc-haha `conversationRecovery.ts`。从 `TranscriptEntry` 列表还原干净的消息列表。

#### 2.3.1 `walk_entry_chain()`

```python
def walk_entry_chain(entries: list[TranscriptEntry]) -> list[TranscriptEntry]:
    """从最新 entry 沿 parentUuid 链回溯到 root，返回从 root→leaf 的有序列表。
    
    算法:
        1. 构建 {uuid: entry} 索引
        2. 从最后一个 entry 开始，沿 parentUuid 回溯收集 uuid
        3. 反转得到 root→leaf 顺序
        4. 去重 + 按链排序
    
    处理分叉: 只走第一个 parent 链（v1 线性）。
    """
```

> **⚠ v1 简化（问题 #9）**：不变量 #5 规定 v1 严格线性，主会话 v1 也不支持
> fork，因此 parentUuid 回溯+反转在 v1 没有任何收益，反而增加恢复期复杂度与
> bug 面（多 root、末行恰为 sidechain entry 时会走偏）。**v1 实际恢复走
> `reconstruct_live_messages()`（§2.3.5）的按文件顺序单遍 + checkpoint reset**，
> 而非 parentUuid 回溯。`parentUuid` 字段照常写入（含 checkpoint 在同一线性链上）
> 以备未来 fork 与 FULL 审计的链式校验，但 LIVE 恢复路径不依赖它。
> `walk_entry_chain()` 保留为「未来 fork / 审计链校验」入口。

#### 2.3.2 `filter_unresolved_tool_uses()`

**复用 `agents/fork.py:filter_incomplete_tool_calls()` 核心逻辑**，但将其提升到 `persistence/recovery.py` 或让 fork.py 导入本模块。

```python
def filter_unresolved_tool_uses(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """丢弃无配对 tool_result 的 tool_use block，移除变空的 assistant 消息。
    
    直接委托到 agents/fork.py 现有实现。保持单一实现源。
    """
```

#### 2.3.3 `filter_orphaned_thinking()` (新增)

```python
def filter_orphaned_thinking(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """移除「纯孤立」thinking 的 assistant 消息。

    Anthropic extended thinking 产出的 thinking block 含 signature，没有后续
    实际内容时是孤儿数据 — 对恢复的对话无贡献且浪费 token。

    ⚠ 安全约束（问题 #8）——朴素「text 全空 + 有 thinking → 删整条」会误删
    带 tool_use 的 assistant 消息，连带删掉 tool_use，使其 tool_result 变孤儿。
    `thinking + tool_use`（无 text）是 extended thinking 的常见形态，绝不能删。
    因此判定「孤立」必须同时满足：
      - 存在 thinking block；
      - 不含任何 tool_use block；
      - 不含任何非空 text block。
    只有 content 实质上「只有 thinking」时才移除整条消息。

    ⚠ thinking 启用态（问题 #8 续）：开启 extended thinking 时，Anthropic 要求
    assistant 轮的 thinking block 及其 signature 被原样保留并回传，且 tool_use
    必须与同轮 thinking 一起出现。本函数只删「孤立纯 thinking」消息，绝不剥离
    仍连着 tool_use/text 的 thinking。若恢复后 config 仍启用 thinking，保留的
    thinking signature 必须完整，否则后续请求会被 API 拒绝。
    """
```

#### 2.3.4 `detect_turn_interruption()` (新增)

```python
@dataclass
class TurnInterruption:
    interrupted: bool
    last_user_message: dict[str, Any] | None = None
    pending_tool_uses: list[str] = []  # 未完成的 tool_use ID 列表
    detail: str = ""

def detect_turn_interruption(messages: list[dict[str, Any]]) -> TurnInterruption:
    """检测会话是否在 mid-turn 被中断。
    
    判断条件:
    - 最后一条消息 role="user"（非 tool_result）→ 用户已发 prompt 但 LLM 未响应
    - 或最后一条 assistant 消息含 tool_use 但下一个 user 消息无对应 tool_result
    
    恢复策略: 调用方可在 messages 末尾注入 "Continue..." user prompt 让 Agent 继续。
    """
```

#### 2.3.5 `reconstruct_live_messages()` — LIVE 恢复（分段链）

```python
def reconstruct_live_messages(entries: list[TranscriptEntry]) -> list[dict[str, Any]]:
    """从 entries 还原「当前活跃」messages 列表（喂给下一轮 LLM 的那份）。

    分段链算法（问题 #3）—— 自包含 snapshot 使其非递归、O(n) 单遍：
        base = []          # 最近一个 checkpoint 的 snapshot
        tail = []          # 该 checkpoint 之后追加的普通消息
        for e in entries（按文件顺序，已过滤 isSidechain）:
            if e.type == "compaction":
                base = e.compaction.snapshot   # 重置：摘要 + 保留尾部
                tail = []
            elif e.type == "message":
                tail.append(e.message)
        return base + tail

    即：LIVE = 「最后一段压缩后的快照」+「其后新增消息」。压缩前的原始 entries
    虽在文件里，但被本算法在遇到 checkpoint 时整体丢弃（reset），不会重复计入。
    无 checkpoint → 退化为「全部消息顺序拼接」。
    """
```

> 与 §2.2 写入水位的一致性：checkpoint 后 `_written_count = len(snapshot)`，此后每
> append k 条普通 entry 即 `_written_count = len(snapshot)+k`，恰对应本算法
> `len(base)+len(tail)`。写读两侧水位严格对齐。

#### 2.3.5b `reconstruct_messages()` — 清洗包装

```python
async def reconstruct_messages(entries: list[TranscriptEntry]) -> list[dict[str, Any]]:
    """LIVE 恢复 + 清洗。
    
    1. reconstruct_live_messages() 取活跃列表（分段链，过滤 isSidechain）
    2. filter_unresolved_tool_uses()
    3. filter_orphaned_thinking()
    4. detect_turn_interruption() — 返回 (clean_messages, interruption)
    """
```

#### 2.3.7 `reconstruct_full_history()` — FULL 审计视图（新增）

```python
@dataclass
class HistorySegment:
    index: int                              # 0 = 首段；之后每个 checkpoint 起新段
    messages: list[dict[str, Any]]          # 本段原始消息（未被压缩改写）
    compaction: CompactionRecord | None     # 收尾本段的 checkpoint（末段为 None）

def reconstruct_full_history(entries: list[TranscriptEntry]) -> list[HistorySegment]:
    """还原【完整、未删减】的历史，按 checkpoint 切分为段，供审计/回溯/导出。

    与 LIVE 不同：**不丢弃任何原始消息**。每遇到一个 type="compaction" entry
    即收尾当前段（附上其 CompactionRecord），开启下一段。最终：
      - 段 i 的原始消息 + 段 i 的 checkpoint.compaction（说明「这些被摘要成了什么」）
      - 末段无 checkpoint。
    审计工具可据 CompactionRecord.summarized_uuid_first/last 把「摘要」回链到它
    替换掉的具体原始 entries，实现「展开压缩」。
    """
```

#### 2.3.6 `clean_and_rebuild()` — 一站式入口

```python
@dataclass
class RecoveryResult:
    messages: list[dict[str, Any]]          # LIVE 活跃列表（清洗后）
    interruption: TurnInterruption | None
    entries_loaded: int
    entries_kept: int
    tool_uses_removed: int
    warnings: list[str]
    # ---- 分段链信息（问题 #3）----
    compaction_count: int = 0               # checkpoint 数量（=压缩次数）
    written_count: int = 0                  # LIVE 列表长度 → 预置 SessionState._written_count
    full_segments: list[HistorySegment] | None = None  # 仅 want_full=True 时填充

async def clean_and_rebuild(
    entries: list[TranscriptEntry],
    *,
    include_sidechain: bool = False,
    want_full: bool = False,
) -> RecoveryResult:
    """完整的清洗+重建流程，返回 RecoveryResult。

    - LIVE 路径（默认）：reconstruct_messages()（§2.3.5b，分段链 + 清洗）。
      `written_count = len(messages_before_cleaning_live)`，供 load_session 预置
      `_written_count`，确保 begin_persistence 不重复 append（问题 #1）。
    - want_full=True → 同时填 `full_segments = reconstruct_full_history(entries)`
      （审计用，不参与喂模型）。
    - include_sidechain=True → 同时包含 sidechain 条目（用于子 Agent 恢复）。

    ⚠ `_written_count` 应取「清洗前」的 LIVE 长度，因为清洗（丢 tool_use / 孤立
    thinking）只影响喂模型的副本，不改变磁盘水位；磁盘水位必须对齐实际写入的
    entry 数，否则下一次 flush 的增量切片会错位。清洗后的 messages 仅用于运行时，
    self.messages 在首次 flush_new_messages 时会被 final_messages 覆盖回对齐态。
    """
```

> **水位对齐要点**：磁盘上「当前段」的消息数 = LIVE 清洗前长度。`_written_count`
> 必须等于它，而不是清洗后长度。否则 resume 后第一次增量写会把已落盘消息再写一遍
> 或漏写。测试需覆盖「resume 一个含 tool_use 被清洗的会话后继续对话，JSONL 行数
> 正确递增」。

### 2.4 `persistence/session.py` — 会话管理

**职责**: 会话 CRUD — 列表、加载（恢复）、删除、清理。

```python
@dataclass
class SessionInfo:
    session_id: str
    title: str                      # 首条 user message 的前 100 字符
    created_at: str                 # ISO timestamp
    last_activity: str              # ISO timestamp
    message_count: int
    project_hash: str
    has_unfinished_turn: bool = False  # 有未完成轮次（中断）


async def list_sessions(workspace_root: str | None = None) -> list[SessionInfo]:
    """扫描 ~/.agent/projects/{project_hash}/*.jsonl，按 last_activity 降序。
    
    对于每个 session JSONL:
    1. 读取第一条和最后一条 entry，提取时间戳
    2. 提取第一个 user message 的 text[:100] 作为 title
    3. 计数 entries
    4. 返回 SessionInfo 列表

    ⚠ 性能声明修正（问题 #7）：`message_count` 需统计全部 entries、
    `has_unfinished_turn` 需看最后一条 entry 的 role，二者无法只靠 seek
    首/末两行得到——精确计数必须读全文件。因此 v1 取舍如下：
      - title / created_at：读首行即可（seek 开头）；
      - last_activity：seek 末行（反向找最后一个 '\\n'）即可；
      - message_count / has_unfinished_turn：**需要时才读全文件**；列表视图
        若只展示标题与时间，可将 count 标为「近似/惰性」，进入会话详情时再算。
    不要在风险表与本节同时声称「只 seek 首末行」却又给出精确 count。
    （见下文风险表 #4 已同步修正。）
    """

async def load_session(
    session_id: str,
    *,
    workspace_root: str | None = None,
    project_hash: str | None = None,
) -> SessionState | None:
    """从磁盘恢复一个 SessionState。
    
    流程:
    1. 确定 project_hash（参数 > workspace_root 计算）
    2. migrate_transcript(path) — ⚠ 接入点（问题 #6）：迁移必须在这里被调用，
       否则 migrate.py 永远是悬空死代码。先升级（v0→v1，幂等）再读取。
    3. load_entries(path)
    4. clean_and_rebuild(entries)
    5. 构造 SessionState(session_id=session_id, messages=rebuilt)
       ⚠ 防重复落盘（问题 #1/#5）：恢复出的 SessionState 必须携带
       「已落盘水位」——把 _written_count 预置为 len(rebuilt)，且
       transcript_path 指向被加载的同一个文件。这样 begin_persistence()
       不会再把历史 append 一遍（详见 §3.1 修订）。
    6. 如果存在中断且最后一条是 user message，不注入 "Continue..."，
       而是让调用方（CLI）传 --continue 显式决定
    7. SessionPermissionStore（问题 #10）：v1 不从 JSONL 恢复权限记忆（权限
       不落盘，见不变量 #7），故 resume 后「always allow」决定会丢失、用户会被
       重新弹窗。这是已知 UX 退化，需在 resume 提示里明示「权限记忆未恢复」，
       并在 permission-session-scope-plan 中跟踪后续持久化。

    返回 None → 会话不存在或损坏。
    """

async def delete_session(
    session_id: str,
    *,
    workspace_root: str | None = None,
    project_hash: str | None = None,
) -> bool:
    """删除会话 JSONL 文件 + sidechain 子目录。
    
    1. 删除 {session_id}.jsonl
    2. shutil.rmtree sidechain_dir（如果存在）
    返回 True → 删除成功；False → 会话不存在。
    """

async def cleanup_old_sessions(
    workspace_root: str | None = None,
    *,
    max_age_days: int = 30,
    dry_run: bool = False,
) -> list[str]:
    """清理超过 max_age_days 的旧会话。
    返回被删除的 session_id 列表。
    """
```

### 2.5 `persistence/migrate.py` — 格式迁移

**职责**: 格式版本升级。`CURRENT_VERSION = 1`。

```python
CURRENT_VERSION = 1

async def migrate_transcript(path: str) -> bool:
    """读取 JSONL → 检测版本 → 按需升级 → 原子写入。
    
    版本链:
      v0 → v1: 旧 sidechain 格式（裸 message dict）→ 包装为 TranscriptEntry
      v1+: 当前格式，无需迁移
    
    返回 True → 发生了迁移；False → 无需迁移。
    迁移失败不损坏原文件（先写 tempfile + os.replace）。
    """

def needs_migration(entries: list[dict[str, Any]]) -> bool:
    """检查 entries 中是否有非 TranscriptEntry 格式的裸 message 或 version < CURRENT_VERSION。"""
```

**v0 → v1 迁移规则**:
- 检测: 第一行 JSON 无 `uuid` 字段 → v0
- 升级: 每行包一层 `TranscriptEntry`，uuid 自动生成，parentUuid 串成线性链

### 2.6 `persistence/__init__.py` — 公开导出

```python
from agent_core.persistence import (
    SessionTranscriptWriter,
    TranscriptEntry,
    SessionInfo,
    # path functions
    compute_project_hash,
    session_jsonl_path,
    sidechain_dir,
    sidechain_jsonl_path,
    # session CRUD
    list_sessions,
    load_session,
    delete_session,
    cleanup_old_sessions,
    # recovery
    clean_and_rebuild,
    RecoveryResult,
    TurnInterruption,
    # migration
    migrate_transcript,
)
```

---

## 3. 集成变更

### 3.1 `SessionState` 扩展 (`session/state.py`)

新增字段和方法：

```python
@dataclass
class SessionState:
    # ... 现有字段保持不变 ...

    # ---- NEW (Phase B) ----
    _writer: SessionTranscriptWriter | None = field(default=None, repr=False)
    _written_count: int = field(default=0, repr=False)

    async def begin_persistence(self, path: str) -> None:
        """开启落盘。创建 writer。

        ⚠ 防重复落盘（问题 #1/#5）：旧设计无条件把 self.messages 全量
        append 一遍。但 --resume 用同一个 session_id → 同一个文件，
        load_session 刚从该文件读出 messages，再 append 会让文件内容翻倍，
        且 _written_count 在 append 之后才置位、重复无法回滚。

        正确规则：只写「磁盘上尚不存在的消息」。以 self._written_count 为
        已落盘水位（load_session 已将其预置为 len(messages)，见 §2.4）：
          - 新建会话：_written_count 初始为 0 → 落盘全部消息（通常本就为空）；
          - 恢复会话：_written_count == len(self.messages) → 不重写任何历史。
        """
        self.transcript_path = path
        self._writer = SessionTranscriptWriter(path, self.session_id)
        await self._writer.open()  # open() 内部保证文件以 '\n' 结尾（问题 #4）
        # 仅落盘水位之后的消息，避免 resume 时重复 append。
        for msg in self.messages[self._written_count:]:
            await self._writer.write_entry(msg)
        self._written_count = len(self.messages)

    async def end_persistence(self) -> None:
        """关闭落盘。后续 record_turn_result 不再写磁盘。"""
        if self._writer is not None:
            await self._writer.close()
            self._writer = None
```

修改 `record_turn_result()`:

```python
def record_turn_result(self, final_messages: list[dict[str, Any]] | None) -> None:
    if final_messages is None:
        return
    
    # Phase B: 增量写入新消息
    if self._writer is not None and self._writer.is_open:
        new_messages = final_messages[self._written_count:]
        # 写入在后台进行，但 record_turn_result 是同步的
        # → 需要一个 pending write 机制或改为 async
        # 实际采用: 调用方（Agent.stream）在拿到 DoneMessage 后显式调用
        #   await session._flush_new_messages(final_messages)
        # 而不是在 record_turn_result 内写
        ...
    
    self.messages = list(final_messages)
```

**更简洁的方案**: 不在 `record_turn_result` 内写，而是在 `SessionState` 上提供 `async flush_messages(final_messages)` 由 `Agent.stream()` 在消费到 `DoneMessage` 后调用:

```python
async def flush_new_messages(self, final_messages: list[dict[str, Any]]) -> None:
    """将 final_messages 中尚未落盘的部分写入 JSONL。

    ⚠ 压缩感知（问题 #3）：final_messages 可能因 in-loop compaction 被
    重写/缩短，不能盲目切片。检测到压缩 → 整段重写（rewrite_after_compaction），
    否则才走增量 append。算法见 §2.2「写入算法（增量 + 压缩感知）」。
    """
    if self._writer is None or not self._writer.is_open:
        return
    if self._is_compaction(final_messages):
        # append-only：写 checkpoint，保留压缩前完整 transcript（问题 #3）
        record = self._build_compaction_record(final_messages)
        await self._writer.write_checkpoint(record)
    else:
        for msg in final_messages[self._written_count:]:
            await self._writer.write_entry(msg)
    self._written_count = len(final_messages)
    self.messages = list(final_messages)

def _is_compaction(self, final_messages: list[dict[str, Any]]) -> bool:
    """已落盘前缀 self.messages[:_written_count] 是否仍是 final_messages 的
    前缀；不是（被重写/缩短）则判定为压缩。"""
    if len(final_messages) < self._written_count:
        return True
    return self.messages[:self._written_count] != final_messages[:self._written_count]

def _build_compaction_record(self, final_messages):
    """由 self.messages（压缩前 live）与 final_messages（压缩后 live）推出
    CompactionRecord：最长公共尾部 = kept_count，snapshot = final_messages，
    summarized_count = _written_count - kept_count（见 §2.2）。"""
    ...
```

### 3.2 `Agent.stream()` 集成 (`agent.py`)

需要新增的 await 点：消费到 `DoneMessage` 后，session 落盘：

```python
# agent.py Agent.stream() 中，现有代码:
if session is not None and message.type == "done":
    session.record_turn_result(message.final_messages)

# 新增:
if session is not None and message.type == "done":
    await session.flush_new_messages(message.final_messages)  # NEW: 落盘
    session.record_turn_result(message.final_messages)
```

### 3.3 子 Agent sidechain 路径适配 (`agents/run.py`)

要改动两个地方：

**3.3.1 `_sidechain_dir()` 函数** — 适配新路径方案:

```python
def _sidechain_dir(session_id: str, project_hash: str) -> str:
    """新路径: ~/.agent/projects/{project_hash}/{session_id}/"""
    home = os.path.expanduser("~")
    return os.path.join(home, ".agent", "projects", project_hash, session_id)
```

**3.3.2 `run_agent()` 签名新增参数** — 传递 session_id 和 project_hash:

```python
async def run_agent(
    prompt: str,
    definition: AgentDefinition,
    *,
    # ... 现有参数 ...
    session_id: str | None = None,       # NEW: 父会话 session_id
    project_hash: str | None = None,     # NEW: 项目哈希
) -> AsyncGenerator[AgentMessage, None]:
```

当 `session_id` 和 `project_hash` 均非 None 时，使用新路径格式。否则回退到旧路径（向后兼容，用于没有父会话的独立子 Agent）。

**⚠ 3.3.2.a 完整传递链（问题 #5/#6）** — 「`AgentTool.call()` 传递新参数」一句
掩盖了一整条跨模块管线。`run_agent` 的实参全部来自 `ToolUseContext`
（见 `tool.py:220-231`：`context.parent_trace_id` / `context.messages` / …），
而 `ToolUseContext`（`tools/base.py`）**目前只有 `parent_trace_id`，没有
`session_id` / `project_hash`**，query loop 构造 context 时也从未注入 session。
要把 session 透到 `run_agent`，需依次改：
  1. `tools/base.py`：`ToolUseContext` 增加 `session_id: str | None` /
     `project_hash: str | None`（或一个 `persistence_ctx` 子对象）；
  2. query loop 构造 `ToolUseContext` 的位置：从当前 `SessionState` 注入这两个值
     （`Agent.stream` 已持有 `session`，需把它透到 query → context 构造点）；
  3. `agents/tool.py` 两条执行路径 `_run_sync`（:220）与 `_run_async`（:244）：
     把 `context.session_id` / `context.project_hash` 传给 `run_agent`；
  4. `run_agent` 再透传给 `_sidechain_dir()`。
这条链是真实工作量，Step 9 必须显式包含（原 0 工时预算偏低，见 §5 修订）。

**⚠ 3.3.2.b `resume_agent` / `_load_sidechain_messages` 必须 unwrap（问题 #2）** —
sidechain 行一旦从裸 `{role, content}` 改成 `TranscriptEntry` 信封
（`{uuid, parentUuid, …, message:{role,content}}`），`fork.py:165
_load_sidechain_messages()` 现在把每行 JSON **直接当消息** append 进
`messages` 并喂给 `run_agent(parent_messages=…)`，结果顶层没有 `role` 的信封
会污染模型输入、恢复出的子 Agent 直接读坏。**必须同步修改**：
  - `_load_sidechain_messages()` 解析每行后取 `record["message"]`（兼容旧格式：
    若无 `"message"` 键则按裸 message 处理 / 或先跑 migrate）；
  - 顺便复用 §2.3 的清洗（`filter_unresolved_tool_uses`）保持与主会话一致。
此项原计划完全漏列，是格式变更的硬性连带改动，须加进 Step 9。

**3.3.3 sidechain 写入格式升级** — `_write_turn_to_sidechain()` 和 `_write_tool_result_to_sidechain()` 改为使用 `TranscriptEntry` 格式:

```python
async def _write_turn_to_sidechain(fh, turn, texts, tool_uses, session_id, last_uuid):
    """写入 assistant 消息为 TranscriptEntry 格式。"""
    entry = TranscriptEntry.from_message(
        message={"role": "assistant", "content": content},
        session_id=session_id,
        parent_uuid=last_uuid,
        is_sidechain=True,
    )
    await fh.write(entry.model_dump_json() + "\n")
```

### 3.4 CLI 集成 (`cli.py`)

#### 3.4.1 新增命令行参数

在 `main()` 和 `_main()` 中解析 `--resume` / `--continue`:

```python
def main() -> None:
    config = _load_config()
    # 解析命令行参数（简单解析，不用 argparse）
    resume_id: str | None = None
    continue_last: bool = False
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--resume" and i + 1 < len(args):
            resume_id = args[i + 1]
        elif arg == "--continue":
            continue_last = True
    
    asyncio.run(_main(config, resume_id=resume_id, continue_last=continue_last))
```

#### 3.4.2 `/sessions` 命令

```python
# _handle_command() 中新增:
if cmd == "/sessions":
    await _list_sessions(config)
    return False
```

```python
async def _list_sessions(config: AgentConfig) -> None:
    """列出当前项目的所有会话。"""
    from .persistence.session import list_sessions
    
    sessions = await list_sessions(config.workspace_root)
    if not sessions:
        print("(no saved sessions)")
        return
    
    for s in sessions:
        marker = " [unfinished]" if s.has_unfinished_turn else ""
        print(f"  {s.session_id[:8]}...  {s.last_activity}  ({s.message_count} msgs)  {s.title}{marker}")
```

#### 3.4.3 `_repl()` 中的会话恢复

```python
async def _repl(config: AgentConfig, *, resume_id: str | None = None, continue_last: bool = False) -> None:
    session = SessionState()
    
    # ---- 会话恢复 ----
    if resume_id is not None or continue_last:
        from .persistence.session import list_sessions, load_session
        
        if continue_last:
            sessions = await list_sessions(config.workspace_root)
            if sessions:
                resume_id = sessions[0].session_id  # 最近的
        
        if resume_id:
            loaded = await load_session(resume_id, workspace_root=config.workspace_root)
            if loaded is not None:
                session = loaded
                print(f"[resumed session {resume_id[:8]}... ({len(session.messages)} messages)]")
            else:
                print(f"[session {resume_id[:8]}... not found, starting fresh]")
    
    # ---- 开启落盘 ----
    from .persistence.path import compute_project_hash, session_jsonl_path
    project_hash = compute_project_hash(config.workspace_root)
    transcript_path = session_jsonl_path(project_hash, session.session_id)
    await session.begin_persistence(transcript_path)
    
    try:
        # ... 现有 REPL 循环（不变）...
    finally:
        await session.end_persistence()
```

---

## 4. 测试计划

### 4.1 单元测试 (`tests/unit/`)

| 测试文件 | 测试项 | 数量 |
|---------|--------|------|
| `test_persistence_path.py` (新建) | `compute_project_hash` 确定性/空workspace/cwd fallback；`session_jsonl_path` / `sidechain_dir` 路径正确性；`list_session_files` 过滤/排序/空目录 | ~8 |
| `test_persistence_storage.py` (新建) | `TranscriptEntry` 模型校验/from_message/序列化；`SessionTranscriptWriter` open/close/write/entry_count/last_uuid 链/idempotent close；**open() 对无尾换行文件补 '\n'（问题 #4）**；**rewrite_after_compaction 原子覆写（问题 #3）** | ~13 |
| `test_persistence_recovery.py` (新建) | `walk_entry_chain` 线性链/单条/空列表/分叉；`filter_orphaned_thinking` 纯thinking/混合内容/空assistant/**thinking+tool_use 不被误删（问题 #8）**；`detect_turn_interruption` mid-turn中断/正常结束/空列表；`reconstruct_messages` 完整流程；**`reconstruct_live_messages` 无checkpoint/单checkpoint/多checkpoint reset 正确（问题 #3）**；**`reconstruct_full_history` 分段完整、不丢原始消息** | ~17 |
| `test_persistence_session.py` (新建) | `list_sessions` 空目录/单会话/多会话排序；`load_session` 正常恢复/不存在/损坏JSONL/**调用 migrate（问题 #6）**/**预置 _written_count 防 resume 重复落盘（问题 #1）**；`delete_session` 成功/不存在 | ~10 |
| `test_persistence_migrate.py` (新建) | `migrate_transcript` v0→v1升级/已是v1不升级/空文件/非法JSON跳过 | ~6 |
| `test_session_state.py` (修改) | 新增 `begin_persistence` / `end_persistence` / `flush_new_messages` / writer 生命周期测试；**resume 后 begin_persistence 不重复 append（问题 #1）**；**`_is_compaction` 检测 + 压缩后整段重写（问题 #3）** | +~6 |

### 4.2 集成测试 (`tests/integration/`)

| 测试文件 | 测试项 |
|---------|--------|
| `test_persistence_flow.py` (新建) | 完整落盘+恢复流程：发送 prompt → 检查 JSONL 文件存在+格式 → 加载恢复 → messages 一致 → 继续对话 → 历史延续 |
| `test_persistence_sidechain.py` (新建) | 子 Agent sidechain 新格式写入 → 路径正确 → parentUuid 链连续 |
| `test_persistence_resume.py` (新建) | 中断会话（mid-turn tool_use 无结果）→ 恢复 → filter_unresolved_tool_uses 清洗 → 注入 Continue → Agent 继续 |
| `test_persistence_compaction.py` (新建，问题 #3) | 触发压缩 → checkpoint append 后**旧原始 entries 仍在文件中**（FULL 完整）；`reconstruct_live_messages` 只返回 summary+尾部（LIVE 正确、无重复）；多次压缩 → 多段；`reconstruct_full_history` 段数/边界正确；压缩后继续对话 → 增量 entry 接在最后 checkpoint 之后、水位对齐 |
| `test_history_continuity.py` (修改) | 新增：确认跨 prompt 后 JSONL 正确追加了新行；resume 后继续不重复 append |

---

## 5. 实现步骤（按依赖顺序）

### Step 1: `persistence/path.py` (0.5h)
- 实现 `compute_project_hash` / `session_jsonl_path` / `sidechain_dir` 等纯函数
- 单元测试

### Step 2: `persistence/storage.py` (2.5h ⬆ 原 1.5h)
- 实现 `TranscriptEntry`（含 `type`/`compaction` 判别）+ `CompactionRecord` 模型
- `open()` 补尾换行守卫（问题 #4）；新增 `write_checkpoint()` + `last_checkpoint_uuid`
  （问题 #3，append-only 分段链，**不重写文件**）
- 实现 `load_entries` / `save_entries`
- 单元测试（含 checkpoint append 不破坏旧 entries）

### Step 3: `persistence/recovery.py` (2.5h ⬆ 原 2h)
- 实现 `walk_entry_chain`（审计/未来 fork 用）
- 实现 `filter_orphaned_thinking` + `detect_turn_interruption`
- 实现 `reconstruct_live_messages`（分段链 LIVE）+ `reconstruct_full_history`（FULL 审计）
- 复用 `filter_incomplete_tool_calls`（或提取到 persistence/recovery，更新 fork.py 引用）
- 实现 `reconstruct_messages` + `clean_and_rebuild`（含 `written_count`/`want_full`）
- 单元测试

### Step 4: `persistence/session.py` (2h ⬆ 原 1.5h)
- 实现 `list_sessions` / `load_session` / `delete_session` / `cleanup_old_sessions`
- `load_session` 内调用 `migrate_transcript`（问题 #6）+ 预置 `_written_count`
  防 resume 重复落盘（问题 #1/#5）
- `list_sessions` 精确 count 读全文件 / 或标惰性（问题 #7）
- 依赖：Step 5（migrate）需先于本步可用
- 单元测试

### Step 5: `persistence/migrate.py` (1h)
- 实现 `migrate_transcript` v0→v1
- 单元测试

### Step 6: `persistence/__init__.py` (0.25h)
- 公开 API 导出

### Step 7: `SessionState` 扩展 (1.5h ⬆ 原 1h)
- 新增 `_writer` / `_written_count` / `begin_persistence` / `end_persistence` /
  `flush_new_messages` / `_is_compaction`
- `begin_persistence` 仅落盘 `_written_count` 之后的消息（问题 #1）
- `flush_new_messages` 压缩感知（问题 #3）
- 单元测试补充

### Step 8: `Agent.stream()` 集成 (0.5h)
- `DoneMessage` 后先 `await session.flush_new_messages()` 再 `record_turn_result()`
- 向后兼容：`session=None` 时无变化

### Step 9: `run.py` sidechain 适配 (2.5h ⬆ 原 1h，问题 #2/#5/#6)
- 新增 `session_id` / `project_hash` 参数
- `_sidechain_dir()` 使用新路径方案（保留旧路径 fallback）
- sidechain 写入格式升级为 `TranscriptEntry`
- **完整传递链**：`ToolUseContext` 增 `session_id`/`project_hash`（base.py）→
  query loop 构造 context 时从 `SessionState` 注入 → `AgentTool._run_sync`/
  `_run_async` 透传 → `run_agent`（问题 #5/#6）
- **`resume_agent`/`_load_sidechain_messages` unwrap `.message`**，兼容旧裸格式
  （问题 #2）——格式变更的硬性连带改动，不可遗漏

### Step 10: CLI 集成 (2h)
- 命令行参数 `--resume` / `--continue`
- `/sessions` 命令
- `_repl()` 中开启/关闭落盘
- `_repl()` 中会话恢复逻辑
- resume 提示明示「权限记忆未恢复」（问题 #10）
- 关闭时 `finally` 保证 `end_persistence()`

### Step 11: 集成测试 (1.5h)
- `test_persistence_flow.py`
- `test_persistence_sidechain.py`
- `test_persistence_resume.py`
- `test_history_continuity.py` 补充

### Step 12: 全量回归 + 文档 (2h)
- 全量 pytest 确认无回归
- 更新 `agent_core/__init__.py` 公开导出（如有需要）
- 更新 `AGENTS.md` / README 中的持久化用法说明

---

## 6. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| sidechain 格式改动破坏现有子 Agent 转录 | **高** | **高** | 新信封格式会让 `_load_sidechain_messages` 直接读坏（问题 #2）；必须同步改 unwrap `.message` + 保留旧格式读取兼容（`migrate_transcript` 可升级旧文件） |
| resume 后 begin_persistence 重复 append 致文件翻倍 | **高** | **高** | 预置 `_written_count=len(messages)`，begin_persistence 只写水位之后（问题 #1/#5） |
| in-loop 压缩使增量切片错写、JSONL 与内存分叉 | 中 | 中 | `flush_new_messages` 压缩感知 → append checkpoint 分段链，保留压缩前完整 transcript（问题 #3） |
| checkpoint snapshot 复制保留尾部致文件膨胀 | 低 | 低 | 保留尾部有界（≈最近 N 条）；snapshot 自包含换取非递归恢复；如需极致省空间，未来可加「lean checkpoint」（仅存 summary + 尾部 uuid 引用） |
| crash 留半行 → 下次 append 拼成坏行 | 中 | 中 | `open()` 检测尾字节非 '\n' 时先补换行隔离（问题 #4） |
| session→run_agent 传递链未打通 | 中 | 中 | 显式改 `ToolUseContext` + query 注入 + tool 透传（问题 #5/#6，已计入 Step 9 工时） |
| `session.flush_new_messages()` 异步写入阻塞事件循环 | 低 | 低 | aiofiles 异步 I/O + 单行 JSON 极小，每 prompt 只写 2-10 行 |
| project_hash 变化导致旧会话"丢失" | 低 | 中 | `workspace_root` 稳定（项目目录不变），hash 确定性；提供 `--project-hash` 参数覆盖 |
| 长会话 JSONL 文件变大，`list_sessions` 变慢 | 低 | 低 | title/时间 seek 首末行即可；message_count/has_unfinished_turn 需读全文件（问题 #7），v1 标为惰性/近似或进会话详情时再算；未来维护索引文件 |
| `begin_persistence` / `end_persistence` 配对不当时文件泄漏 | 低 | 低 | `_repl` 用 `try/finally` 保证关闭；`SessionTranscriptWriter.close()` 幂等 |

---

## 7. 依赖关系图

```
Step 1 (path.py)
  ├── Step 2 (storage.py)
  │     ├── Step 3 (recovery.py)
  │     │     └── Step 4 (session.py)
  │     │           └── Step 10 (CLI integration)
  │     ├── Step 7 (SessionState extension)
  │     │     └── Step 8 (Agent.stream integration)
  │     └── Step 9 (run.py sidechain)
  └── Step 5 (migrate.py) [独立，仅依赖 Step 1-2]
  
Step 6 (__init__.py) → after Steps 1-5
Step 11 (集成测试) → after Steps 1-10
Step 12 (回归 + 文档) → after Step 11
```

Steps 3 和 5 可并行；Steps 7-9 可并行。

---

## 8. 不变量（跨模块保证）

1. **写入原子性**: 每条 JSONL entry 写入后立即 flush，crash 时最多丢失最后一行；
   `open()` 补尾换行守卫保证崩溃残行不污染下一行（问题 #4）
2. **路径一致性**: 所有路径计算走 `persistence/path.py`，不内联路径拼接
3. **向后兼容**: `session=None` 时 Agent 行为与 Phase A 完全一致
4. **sidechain 兼容**: 旧 sidechain 文件可被 `migrate_transcript()` 升级；
   `_load_sidechain_messages` unwrap `.message` 兼容新信封（问题 #2）
5. **parentUuid 链完整性**: v1 严格线性（每条 entry.parentUuid = 上条 entry.uuid），
   checkpoint entry 也在同一条链上
6. **递归安全**: `begin_persistence` → `end_persistence` 按 LIFO 配对，嵌套调用不会创建多个 writer
7. **权限记忆**: v1 不从 JSONL 恢复 `SessionPermissionStore`（权限记忆不落盘，问题 #10 已在 UX 明示）
8. **压缩 append-only / 完整 transcript（问题 #3）**: 压缩**永不**重写或截断 JSONL，
   只 append 一条 checkpoint entry。压缩前的原始消息 entries 永久保留 → FULL 审计可
   还原全量历史；LIVE 恢复 = 最后一个 checkpoint 的 snapshot + 其后追加消息
9. **读写水位对齐（问题 #1/#3）**: 磁盘「当前段」消息数 == `_written_count` == LIVE
   清洗前长度。checkpoint 后 `_written_count = len(snapshot)`；resume 时
   `load_session` 预置 `_written_count`，`begin_persistence` 只写水位之后
