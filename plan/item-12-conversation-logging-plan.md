# Item 12: 对话日志 & 数据采集层 — 详细实现计划

> **状态**: ✅ 已实现（2026-06-26）。全部 10 个 Step 落地，新增 47 个 logging
> 单元/集成测试，全套 1004 passed（默认关闭无回归）。CLI `/logs` 命令 + 文档
> （`docs/guides/logging.md`）+ 示例（`examples/conversation_logging.py`）已附。
> 子 Agent 日志 v1 仅预留字段（`agent_depth`/`is_sidechain`），完整实现留 Phase 2。
> **目标**: 一个**独立的、旁路式（read-only）的对话日志/数据采集层**，把 Agent
> 每次对话的完整 trace（用户输入、LLM 文本响应、工具调用与结果、token、延迟、
> 错误、停止原因）以 append-only JSONL 落盘，**同时服务于「数据采集/分析」与
> 「运行审计/可观测」两类用途**。
> **预估**: 2.5 天（~20 工时，含异步写入队列、脱敏、读取/导出工具、TurnTracer
> sink 改造与全套测试）。
> **设计定位（已与需求方确认）**:
>   - **新建独立日志层**，**不复用、不改动** Item 10 的 `persistence/`（resume 用）。
>   - **JSONL 文件**存储，零外部依赖（不引入数据库）。
>   - 用途 = 数据采集/分析 + 运行审计/可观测（**不**承担会话归档/回放——那是
>     `persistence/` 的职责）。
> **参考实现**: 现有 `logging/tracer.py`（turn 级 metrics）、`persistence/`（路径与
> JSONL writer 风格可借鉴，但不共享存储）。
> **关联计划**: [item-10-session-persistence-plan.md](./item-10-session-persistence-plan.md)、
> [agent-core-llm-multi-provider-plan.md](./agent-core-llm-multi-provider-plan.md) §3.8。

---

## 0. 为什么需要这一层（现状 gap 分析）

项目已有两套相邻但**都不满足**本需求的基础设施：

| 现有组件 | 文件 | 记录什么 | 落盘? | 缺口 |
|---------|------|---------|------|------|
| `TurnTracer` | `logging/tracer.py` | turn 级 metrics（provider/model/token/latency/tool_names/error_type） | ❌ 仅 `logging.getLogger("agent_core.turn").info()`，**持久化由用户自配 handler** | 不含**对话文本内容**；无持久化；无统一文件布局 |
| `persistence/` (Item 10) | `persistence/*.py` | 完整 message 列表 + 压缩 checkpoint | ✅ `~/.agent/projects/{hash}/{session}.jsonl` | 为 **resume/恢复**设计，格式是「喂回 LLM 的 message」，**不含** per-turn token/latency/provider 等可观测指标；压缩会改写 LIVE 视图，不适合做「不可变的分析事实源」 |

**本层填补的空白**：一个**不可变、append-only、自包含**的事件流日志，**横跨对话
内容与运行指标**，专为「事后分析 / 数据集导出 / 审计排查」而生，**与运行时的
resume 行为彻底解耦**（即使 `persistence` 关闭或压缩重写了历史，日志依然是完整、
不可变的事实源）。

### 设计原则

1. **旁路只读（zero-intrusion）**：日志层只**消费**已有事件流（`AgentMessage` /
   `TurnTracer` 的 turn record），**绝不**修改消息、不参与控制流、不影响 token
   预算或 resume。关掉它，Agent 行为逐字节不变。
2. **不可变事实源**：纯 append-only，永不重写/截断/压缩。一行一个事件。
3. **存储独立**：写到 `~/.agent/logs/`，**与 `~/.agent/projects/`（persistence）
   物理隔离**，互不读写。
4. **失败不致命**：日志 sink 任何异常都被吞掉并降级（记一条内部 warning），
   **绝不**让日志故障中断用户对话。
5. **零额外依赖**：复用已有 `aiofiles`，不引入数据库/pandas 等。

---

## 1. 架构总览

```
┌──────────────────────────────────────────────────────────────────┐
│                          Agent.stream()                          │
│  消费 AgentMessage 流（user_prompt → assistant_text → tool_use    │
│   → tool_result → done/error）—— 对话内容的唯一公开出口           │
└───────────┬───────────────────────────────────┬──────────────────┘
            │ (旁路) record(message)             │ (已有) session.flush
            ▼                                     ▼
┌────────────────────────────┐         ┌────────────────────────────┐
│  logging/recorder.py       │         │  persistence/ (Item 10)    │
│  ConversationRecorder      │         │  resume 用，互不干扰         │
│  AgentMessage → LogRecord  │         └────────────────────────────┘
└───────────┬────────────────┘
            │ emit(record)
            ▼
┌────────────────────────────┐     ┌──────────────────────────────────┐
│  logging/sink.py           │◄────│  logging/tracer.py (改造)        │
│  LogSink (ABC)             │     │  TurnTracer 新增可选 sink 投递     │
│  JsonlLogSink (异步队列)    │     │  turn record → emit(turn_metric) │
│  NullSink / 可插拔          │     └──────────────────────────────────┘
└───────────┬────────────────┘
            │ append-only JSONL
            ▼
┌────────────────────────────────────────────────────────────────────┐
│  ~/.agent/logs/{project_hash}/{session_id}.jsonl                    │
│  （或按天滚动：{project_hash}/{YYYY-MM-DD}/{session_id}.jsonl）       │
│  一行一个 LogRecord：conversation event ∪ turn metric ∪ session meta │
└───────────┬────────────────────────────────────────────────────────┘
            │ 读取 / 导出
            ▼
┌────────────────────────────────────────────────────────────────────┐
│  logging/reader.py                                                 │
│  load_log() → list[LogRecord]                                      │
│  iter_conversations() → 按 trace_id/turn 重建结构化对话             │
│  export_dataset() → 扁平化为「分析友好」记录（dict/CSV 行）          │
│  aggregate_metrics() → 按 session/tool/provider 汇总成本与延迟       │
└────────────────────────────────────────────────────────────────────┘
```

**数据双源、单流**：日志层从**两个层级**取数，归并到**同一条 JSONL 流**：

| 数据源 | 取自 | 提供 | 服务用途 |
|--------|------|------|---------|
| 对话内容事件 | `Agent.stream()` 出口的 `AgentMessage` | user_prompt / assistant_text / tool_use / tool_result / error / done | **数据采集/分析** |
| turn 级指标 | `TurnTracer` 已采集的 `_TurnRecord` | provider/model/input_tokens/output_tokens/cache/latency_ms/tool_names | **运行审计/可观测** |

二者共享 `trace_id` + `turn_index`，分析时可 join。

---

## 2. 详细文件设计

新增子模块全部位于 `src/agent_core/logging/`（与现有 `tracer.py` 平级）。

```
src/agent_core/logging/
├── __init__.py        # 公开导出（现有，补充新符号）
├── tracer.py          # 现有 TurnTracer（小改：新增可选 sink 投递）
├── events.py          # NEW: LogRecord 数据模型（discriminated union）
├── config.py          # NEW: LoggingConfig（开关/路径/滚动/采样/脱敏）
├── path.py            # NEW: 日志文件路径计算（独立于 persistence/path）
├── sink.py            # NEW: LogSink ABC + JsonlLogSink（异步队列）+ NullSink
├── recorder.py        # NEW: ConversationRecorder（AgentMessage → LogRecord）
├── redact.py          # NEW: 脱敏/截断（可选）
└── reader.py          # NEW: 读取 + 重建对话 + 导出数据集 + 指标汇总
```

### 2.1 `logging/events.py` — 日志记录数据模型

每行 JSONL = 一个 `LogRecord`。采用 Pydantic discriminated union（与项目 `AgentMessage`
风格一致），用 `kind` 区分变体。所有变体共享一个 envelope。

```python
class _LogEnvelope(BaseModel):
    ts: str                     # ISO 8601 (UTC, 含毫秒)
    seq: int                    # 单会话内单调递增序号（从 0），保证全序 + 去重
    trace_id: str               # Agent._trace_id，贯穿一次 Agent 生命周期
    session_id: str | None      # SessionState.session_id（无会话时为 None）
    project_hash: str           # compute_project_hash(workspace_root)
    turn_index: int | None = None   # 关联到具体 turn（content 事件尽量带上）
    agent_depth: int = 0        # 0=主 Agent，>0=子 Agent（sidechain）
    is_sidechain: bool = False  # 子 Agent 事件标记

# ---- 变体（kind 判别）----

class SessionStartRecord(_LogEnvelope):
    kind: Literal["session_start"] = "session_start"
    model: str                  # config.model
    permission_mode: str
    workspace_root: str | None
    resumed: bool = False       # 是否由 --resume 恢复而来

class UserPromptRecord(_LogEnvelope):
    kind: Literal["user_prompt"] = "user_prompt"
    text: str                   # 原始用户输入（可被 redact 截断）

class AssistantTextRecord(_LogEnvelope):
    kind: Literal["assistant_text"] = "assistant_text"
    text: str

class ToolUseRecord(_LogEnvelope):
    kind: Literal["tool_use"] = "tool_use"
    tool_use_id: str
    tool_name: str
    tool_input: dict[str, Any]  # 可被 redact 脱敏/截断

class ToolResultRecord(_LogEnvelope):
    kind: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    tool_name: str
    content: Any                # 可被 redact 截断
    is_error: bool = False
    truncated: bool = False     # 取自 ToolCallResult（若可得）
    latency_ms: float | None = None  # 工具执行耗时（若 recorder 计时）

class TurnMetricRecord(_LogEnvelope):
    kind: Literal["turn_metric"] = "turn_metric"
    provider: str
    model: str
    status: Literal["success", "error"]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    tool_uses: int = 0
    tool_names: list[str] = []
    latency_ms: float | None = None
    error_type: str | None = None
    # ↑ 字段与 logging/tracer.py:_TurnRecord.to_dict() 一一对应（单一事实源）

class ErrorRecord(_LogEnvelope):
    kind: Literal["error"] = "error"
    message: str
    error_type: str | None = None

class SessionEndRecord(_LogEnvelope):
    kind: Literal["session_end"] = "session_end"
    stop_reason: str            # DoneMessage.stop_reason
    total_tokens: int           # DoneMessage.total_tokens（累计）
    tool_uses_total: int = 0
    error_detail: str | None = None

LogRecord = Annotated[
    SessionStartRecord | UserPromptRecord | AssistantTextRecord
  | ToolUseRecord | ToolResultRecord | TurnMetricRecord
  | ErrorRecord | SessionEndRecord,
    Field(discriminator="kind"),
]
```

**设计要点**：
- `seq` 是**单会话内**单调递增整数，由 sink 维护（线程/任务安全的计数器）。它使
  日志即便 ts 相同也有全序，且支持「断点续读 / 去重」。
- `turn_metric` 字段刻意与 `tracer.py:_TurnRecord.to_dict()` 对齐——见 §3.3，
  TurnTracer 复用同一份字段定义，不另造一套。
- envelope 与 persistence 的 `TranscriptEntry` 字段**故意不同名**（这里是 `kind`
  事件流、那里是 `message`/`compaction` 恢复语义），避免被误当作同类文件互读。

### 2.2 `logging/config.py` — LoggingConfig

```python
RollingPolicy = Literal["per_session", "per_day"]

class LoggingConfig(BaseModel):
    enabled: bool = False              # 默认关闭（opt-in，不偷偷写盘）
    log_dir: str | None = None         # None → 默认 ~/.agent/logs
    rolling: RollingPolicy = "per_session"

    # ---- 内容范围（数据采集 vs 纯指标）----
    record_content: bool = True        # False → 只记 turn_metric/error/session_*（纯审计/可观测，不落对话文本）
    record_tool_io: bool = True        # 是否记录 tool_input / tool_result 内容

    # ---- 脱敏/截断 ----
    max_text_chars: int = 50_000       # 单条 text/content 截断上限（0=不截断）
    redact_keys: list[str] = []        # tool_input 中需打码的键（如 "api_key","password","token"）

    # ---- 采样（高频场景降本）----
    sample_rate: float = 1.0           # 0.0–1.0，按 trace_id 哈希采样整条会话

    # ---- 写入行为 ----
    flush_every: int = 1               # 每 N 条 flush 一次（1=最强持久性）
    queue_maxsize: int = 10_000        # 异步写队列上限，满则丢弃最旧并计 dropped
```

接入 `AgentConfig`（`models.py`）：

```python
class AgentConfig(BaseModel):
    # ... 现有字段 ...
    logging: LoggingConfig = Field(default_factory=LoggingConfig)   # NEW
```

> **向后兼容**：`logging.enabled` 默认 `False`。不显式开启时，本层完全不实例化、
> 不写盘、零开销，所有现有测试与行为不受影响（不变量 #3）。

### 2.3 `logging/path.py` — 日志路径计算

纯函数，无副作用。**独立于** `persistence/path.py`（不同根目录），但**复用其**
`compute_project_hash`（避免两套哈希实现），仅 import 该函数。

```python
from ..persistence.path import compute_project_hash  # 复用，单一实现源

AGENT_LOGS_ROOT = "~/.agent/logs"      # 与 ~/.agent/projects 物理隔离

def logs_root(log_dir: str | None) -> str:
    """log_dir 覆盖 > 默认 ~/.agent/logs（expanduser）。"""

def log_file_path(
    project_hash: str,
    session_id: str | None,
    *,
    rolling: RollingPolicy = "per_session",
    log_dir: str | None = None,
    now: datetime | None = None,
) -> str:
    """
    per_session: {root}/{project_hash}/{session_id or trace_id}.jsonl
    per_day:     {root}/{project_hash}/{YYYY-MM-DD}/{session_id or trace_id}.jsonl
    无 session_id 时回退用 trace_id 作文件名（独立 run 也能落日志）。
    """

def list_log_files(project_hash: str, *, log_dir: str | None = None) -> list[str]:
    """按 mtime 降序返回该项目下所有日志文件（供 reader/CLI 列举）。"""
```

> **隔离不变量（#2）**：日志根 `~/.agent/logs/` 与 persistence 根
> `~/.agent/projects/` 永不交叉。`delete_session`（persistence）**不**触碰日志，
> 日志的清理走本层独立的 `cleanup_old_logs()`。

### 2.4 `logging/sink.py` — 日志输出端

```python
class LogSink(ABC):
    """日志记录的可插拔输出端。"""
    @abstractmethod
    async def emit(self, record: LogRecord) -> None: ...
    @abstractmethod
    async def aclose(self) -> None: ...
    @property
    @abstractmethod
    def dropped(self) -> int: ...   # 因队列满/错误被丢弃的条数（可观测）


class NullSink(LogSink):
    """禁用态使用；emit 为 no-op。让 recorder 无需到处判 None。"""


class JsonlLogSink(LogSink):
    """Append-only JSONL，后台异步写，绝不阻塞调用方（turn loop）。

    机制：
      - emit() 仅把 record 投进 asyncio.Queue（非阻塞 put_nowait）；
        队列满 → 丢弃并 dropped += 1（绝不反压阻塞对话）。
      - 后台 _drain task 从队列取出，序列化 model_dump_json()+'\n'，
        aiofiles append + 按 flush_every 周期 flush。
      - seq 计数器在此维护（单调递增）。
      - open() 复用 persistence storage 的「补尾换行守卫」思路：若文件非空且
        末字节非 '\n'，先补一个 '\n' 隔离崩溃残行（崩溃最多坏最后一行）。
    """
    def __init__(self, path: str, *, flush_every: int = 1, queue_maxsize: int = 10_000): ...
    async def open(self) -> None: ...
    async def emit(self, record: LogRecord) -> None: ...   # put_nowait，吞掉 QueueFull
    async def aclose(self) -> None: ...   # 排空队列 + flush + 关句柄；幂等
    @property
    def dropped(self) -> int: ...
    @property
    def next_seq(self) -> int: ...
```

**为什么用后台队列而非同步 append**：turn loop（`Agent.stream`）每条消息都过
recorder，若每条都 `await aiofiles.write()+flush()`，在大量小 token 流式块下会把
磁盘 I/O 串进对话热路径。队列 + 单后台 drain task 把 I/O 移出热路径，并天然把
「失败不致命」收敛到一处（drain task 内 try/except → warning + dropped）。

> **持久性权衡**：`flush_every=1` 时每条都 flush（强持久，崩溃最多丢队列中未写的
> 部分 + 最后半行）。高频场景可调大 `flush_every` 换吞吐。

### 2.5 `logging/recorder.py` — ConversationRecorder

把 `AgentMessage` 流翻译成 `LogRecord` 并投递给 sink。**这是 §3.1 在 `Agent.stream`
里挂载的旁路对象**。

```python
class ConversationRecorder:
    """旁路消费 AgentMessage，生成 LogRecord。无状态副作用泄漏到 Agent。"""

    def __init__(
        self,
        sink: LogSink,
        *,
        trace_id: str,
        session_id: str | None,
        project_hash: str,
        config: LoggingConfig,
        redactor: "Redactor",
    ) -> None: ...

    async def record_session_start(self, agent_config: AgentConfig, *, resumed: bool) -> None:
        """会话首个 prompt 前调用一次，写 session_start。"""

    async def record_user_prompt(self, prompt: str) -> None:
        """stream() 开头写 user_prompt（经 redact/截断）。"""

    async def record_message(self, message: AgentMessage) -> None:
        """对 stream 产出的每条 AgentMessage 分发：
          assistant_text → AssistantTextRecord
          tool_use       → ToolUseRecord（tool_input 经 redact；turn_index 维护）
          tool_result    → ToolResultRecord（content 经 redact/截断）
          error          → ErrorRecord
          done           → SessionEndRecord（+ 同步累计 total_tokens / tool_uses）
        若 config.record_content=False，跳过纯内容类（仅放行 error/done）。
        若 config.record_tool_io=False，tool_use/tool_result 仅记元数据（名称/ID）。
        """

    def current_turn_index(self) -> int:
        """供 TurnTracer sink 投递 turn_metric 时对齐 turn_index（见 §3.3）。"""
```

**turn_index 归属**：`AgentMessage` 流本身不直接带 turn_index（它在 InternalEvent
层）。recorder 维护一个本地计数：每见到「一组 assistant 输出后跟随的 tool_result
批次」即认为进入下一 turn——但更稳妥的做法是**让 query 在 DoneMessage/事件上不变，
turn_index 由 TurnTracer 侧的 turn_metric 权威给出**，content 事件用「自上次
turn_metric 以来的 turn 序号」近似对齐。§3.3 给出精确方案：在 `_run_turns` 把
当前 `turn_index` 通过 recorder 暴露的轻量回调同步，使 content 与 metric 对齐。
（v1 可接受 content 事件 turn_index 为「最近一次 metric 的 turn_index+0」近似，
metric 事件 turn_index 精确。）

### 2.6 `logging/redact.py` — 脱敏/截断

```python
class Redactor:
    def __init__(self, config: LoggingConfig) -> None: ...
    def text(self, s: str) -> str:
        """按 max_text_chars 截断，超出加 '…[+N chars]' 标记。"""
    def tool_input(self, data: dict[str, Any]) -> dict[str, Any]:
        """深拷贝后对 redact_keys 命中的键替换为 '***'；对长 value 截断。"""
    def tool_result(self, content: Any) -> Any:
        """对字符串 content 截断；非字符串原样（或 repr 截断）。"""
```

### 2.7 `logging/reader.py` — 读取 + 重建 + 导出

服务「数据采集/分析」与「审计」两类下游消费。

```python
async def load_log(path: str) -> list[LogRecord]:
    """逐行解析 JSONL → LogRecord 列表；坏行 skip + warning；文件不存在 → []。"""

@dataclass
class ConversationView:
    trace_id: str
    session_id: str | None
    turns: list["TurnView"]      # 按 turn_index 聚合
    total_tokens: int
    tool_uses_total: int
    stop_reason: str | None

@dataclass
class TurnView:
    turn_index: int
    assistant_text: str
    tool_calls: list[tuple[str, dict, Any]]   # (name, input, result)
    metric: TurnMetricRecord | None           # token/latency/provider

def iter_conversations(records: list[LogRecord]) -> list[ConversationView]:
    """按 trace_id 分组、按 seq 排序，将 content 事件与 turn_metric 对齐成
    结构化对话视图。供人工审计 / 回放 / 调试。"""

def export_dataset(records: list[LogRecord], *, granularity: Literal["turn","message","session"]="turn") -> list[dict[str, Any]]:
    """扁平化为「一行一记录」的分析友好 dict 列表（可直接喂 csv.DictWriter
    或 pandas.DataFrame）。granularity 决定每行粒度。"""

def aggregate_metrics(records: list[LogRecord]) -> dict[str, Any]:
    """按 session / provider / tool_name 汇总：总 token、总成本近似、平均/分位
    latency、错误率、工具调用频次。供可观测面板。"""

async def cleanup_old_logs(project_hash: str | None = None, *, max_age_days: int = 90,
                           log_dir: str | None = None, dry_run: bool = False) -> list[str]:
    """清理超过 max_age_days 的日志文件（独立于 persistence 清理）。"""
```

### 2.8 `logging/__init__.py` — 公开导出

```python
from .tracer import TurnTracer
from .config import LoggingConfig
from .events import LogRecord  # + 各变体
from .sink import LogSink, JsonlLogSink, NullSink
from .recorder import ConversationRecorder
from .reader import load_log, iter_conversations, export_dataset, aggregate_metrics, cleanup_old_logs

__all__ = [
    "TurnTracer", "LoggingConfig", "LogRecord",
    "LogSink", "JsonlLogSink", "NullSink", "ConversationRecorder",
    "load_log", "iter_conversations", "export_dataset",
    "aggregate_metrics", "cleanup_old_logs",
]
```

并在 `agent_core/__init__.py` 顶层补充导出 `LoggingConfig`（用户配置入口）+
`load_log` / `export_dataset` / `aggregate_metrics`（下游分析入口）。

---

## 3. 集成变更（最小侵入）

### 3.1 `Agent` 挂载 recorder（`agent.py`）

唯一改动点，且全部包在 `config.logging.enabled` 守卫内（关闭时零行为变化）。

```python
class Agent:
    def __init__(self, config, session=None) -> None:
        # ... 现有 ...
        self._recorder: ConversationRecorder | None = None   # NEW，惰性创建
        self._sink: LogSink | None = None

    async def _ensure_recorder(self) -> None:
        """首次 stream 时按需创建 sink+recorder（仅当 logging.enabled）。"""
        if not self._config.logging.enabled or self._recorder is not None:
            return
        # 采样：按 trace_id 哈希决定整条会话是否记录
        if not _sampled(self._trace_id, self._config.logging.sample_rate):
            return
        project_hash = compute_project_hash(self._config.workspace_root)
        session_id = self._session.session_id if self._session else None
        path = log_file_path(project_hash, session_id,
                             rolling=self._config.logging.rolling,
                             log_dir=self._config.logging.log_dir)
        self._sink = JsonlLogSink(path, flush_every=self._config.logging.flush_every,
                                  queue_maxsize=self._config.logging.queue_maxsize)
        await self._sink.open()
        self._recorder = ConversationRecorder(self._sink, trace_id=self._trace_id,
            session_id=session_id, project_hash=project_hash,
            config=self._config.logging, redactor=Redactor(self._config.logging))
        await self._recorder.record_session_start(self._config,
            resumed=bool(self._session and self._session.messages))

    async def stream(self, prompt):
        await self._ensure_recorder()                       # NEW
        if self._recorder:                                  # NEW
            await self._recorder.record_user_prompt(prompt)
        # ... 现有 aclosing(query(...)) ...
        async with aclosing(query(..., recorder=self._recorder)) as stream:  # 透传给 TurnTracer
            async for message in stream:
                if session is not None and message.type == "done":
                    await session.flush_new_messages(message.final_messages)
                    session.record_turn_result(message.final_messages)
                if self._recorder:                          # NEW: 旁路记录
                    await self._recorder.record_message(message)
                yield message

    async def aclose(self) -> None:                         # NEW（可选）
        """显式释放日志 sink（排空队列）。REPL/SDK 退出时调用。"""
        if self._sink is not None:
            await self._sink.aclose()
```

> **生命周期**：sink 后台 drain task 需在进程退出前排空。提供 `Agent.aclose()`；
> CLI 的 `_repl` 在 `finally` 里调用（与现有 `session.end_persistence()` 并列）。
> SDK 用户可 `async with` 包装（可加 `__aenter__/__aexit__`，v1 先提供 `aclose`）。

### 3.2 `query()` 透传 recorder 到 TurnTracer（`query/loop.py`）

`query()` / `_run_turns()` 新增**可选**参数 `recorder: ConversationRecorder | None
= None`，仅向下传给 `TurnTracer`。不传时（直接调用 query / 测试 / fork）行为不变。

```python
# loop.py:414 现有
tracer = TurnTracer(trace_id=trace_id)
# → 改为
tracer = TurnTracer(trace_id=trace_id, recorder=recorder)   # recorder 可为 None
```

### 3.3 `TurnTracer` 新增可选 sink 投递（`logging/tracer.py`）

**唯一改动**：`TurnTracer` 在每 turn 结束、现有 `_logger.info(...)` 之外，**若持有
recorder**，把同一份 `_TurnRecord.to_dict()` 包成 `TurnMetricRecord` 投给 sink。
**单一事实源**：metric 字段定义不重复——`TurnMetricRecord` 直接由 `to_dict()` 构造。

```python
class TurnTracer:
    __slots__ = ("_trace_id", "_recorder")

    def __init__(self, trace_id: str, recorder=None) -> None:
        self._trace_id = trace_id
        self._recorder = recorder

    async def wrap(self, stream):
        turn = _TurnRecord(trace_id=self._trace_id)
        try:
            async for event in stream:
                turn.consume(event)
                yield event
        finally:
            turn.finalize()
            payload = turn.to_dict()
            _logger.info("turn_complete", extra=payload)     # 现有，不变
            if self._recorder is not None:                   # NEW
                await self._recorder.emit_turn_metric(payload)  # → TurnMetricRecord → sink
```

> `recorder.emit_turn_metric(payload)`：在 recorder 内把 dict 适配为
> `TurnMetricRecord`（补 envelope：seq/ts/session_id/project_hash/turn_index）。
> 因为 `to_dict()` 已含 `turn_index`，content 事件与 metric 由此对齐（§2.5）。

### 3.4 CLI 集成（`cli.py`，可选但推荐）

- 配置文件 / 环境变量开启 `logging.enabled`（如 `AGENT_LOG=1` 或 config TOML 段）。
- `_repl` 的 `finally` 中 `await agent.aclose()`（或显式关 sink），与
  `session.end_persistence()` 并列，保证后台队列排空。
- 新增 `/logs` 命令：调用 `list_log_files` + `aggregate_metrics` 打印当前会话的
  token/成本/工具频次小结（可观测自查）。

### 3.5 子 Agent（sidechain）日志（`agents/run.py`，v1 可选）

`ToolUseContext` 已带 `session_id` / `project_hash`（loop.py:232-233）。v1 可先**只记
主 Agent**；子 Agent 事件通过 `agent_depth>0` / `is_sidechain=True` 标记是 Phase 2。
若启用：子 Agent 复用父 sink（同一文件，靠 `is_sidechain` 区分），或写
`{project_hash}/{session_id}/{agent_id}.jsonl`——与 persistence sidechain 布局呼应
但在 `logs/` 根下。**v1 范围内仅预留字段，不强制实现**（见 §5 Step 9）。

---

## 4. 测试计划

### 4.1 单元测试（`tests/unit/`）

| 测试文件 | 测试项 | 数量 |
|---------|--------|------|
| `test_logging_events.py` (新建) | 各 LogRecord 变体序列化/反序列化；discriminator 路由；envelope 字段必填校验 | ~8 |
| `test_logging_path.py` (新建) | `log_file_path` per_session/per_day 布局；log_dir 覆盖；无 session_id 回退 trace_id；`list_log_files` 排序/空目录；**与 ~/.agent/projects 隔离断言** | ~8 |
| `test_logging_sink.py` (新建) | `JsonlLogSink` open/emit/aclose；**后台队列不阻塞 emit**；**队列满丢弃 + dropped 计数**；**open() 补尾换行守卫**；seq 单调递增；aclose 幂等；NullSink no-op | ~12 |
| `test_logging_recorder.py` (新建) | 各 AgentMessage → 对应 Record；`record_content=False` 跳过内容类；`record_tool_io=False` 仅元数据；done → SessionEndRecord 累计正确；user_prompt 经 redact | ~12 |
| `test_logging_redact.py` (新建) | `max_text_chars` 截断标记；`redact_keys` 命中打码；嵌套 dict 深拷贝不改原值 | ~6 |
| `test_logging_tracer.py` (修改现有/新建) | TurnTracer 透明 re-yield 不变；**recorder=None 时仅 _logger.info（无 sink 投递）**；**recorder 存在时投递 TurnMetricRecord 字段与 to_dict 对齐** | ~6 |
| `test_logging_reader.py` (新建) | `load_log` 坏行跳过/空文件；`iter_conversations` 按 trace_id+seq 重建、content↔metric 对齐；`export_dataset` 三种 granularity；`aggregate_metrics` token/延迟/错误率汇总 | ~12 |

### 4.2 集成测试（`tests/integration/` 或 `tests/e2e/`，走 vcrpy 录制）

| 测试文件 | 测试项 |
|---------|--------|
| `test_logging_flow.py` (新建) | `logging.enabled=True` 跑一次带工具调用的对话 → 日志文件存在 → load_log 行数/kind 序列正确（session_start → user_prompt → assistant_text* → tool_use → tool_result → turn_metric → done）→ trace_id/session_id 贯穿 |
| `test_logging_disabled.py` (新建) | `enabled=False`（默认）→ **不创建任何文件、零行为变化**（与无 logging 基线 diff 一致） |
| `test_logging_persistence_isolation.py` (新建) | 同时开 persistence + logging → 两套文件在不同根目录；persistence `delete_session` 不删日志；压缩重写 LIVE 不影响日志（日志仍含压缩前完整事件） |
| `test_logging_resume.py` (新建) | resume 一个已有 session → 新 prompt 的事件 append 到**同一** per_session 日志、seq 接续递增、session_start.resumed=True |

### 4.3 回归

- 全量 `pytest` 确认 `enabled=False` 默认下 ~957 passed 无回归。
- 性能 smoke：开启 logging 后单 prompt 端到端延迟增量 < 5%（队列旁路验证）。

---

## 5. 实现步骤（按依赖顺序）

### Step 1: `logging/events.py` + `logging/config.py`（1.5h）
- LogRecord discriminated union 全部变体 + `_LogEnvelope`
- `LoggingConfig`；接入 `AgentConfig.logging` 字段（`models.py`）
- 单元测试 `test_logging_events.py`

### Step 2: `logging/path.py`（0.5h）
- 复用 `persistence.path.compute_project_hash`；`log_file_path` / `list_log_files`
- 单元测试 `test_logging_path.py`（含隔离断言）

### Step 3: `logging/redact.py`（0.75h）
- `Redactor.text / tool_input / tool_result`
- 单元测试 `test_logging_redact.py`

### Step 4: `logging/sink.py`（2.5h）
- `LogSink` ABC + `NullSink` + `JsonlLogSink`（asyncio.Queue + 后台 drain task）
- 补尾换行守卫、seq 计数、队列满丢弃 + dropped、aclose 幂等、失败吞掉降级
- 单元测试 `test_logging_sink.py`

### Step 5: `logging/recorder.py`（2h）
- `ConversationRecorder`：session_start / user_prompt / record_message 分发 /
  emit_turn_metric；record_content / record_tool_io 守卫；redact 接线
- 单元测试 `test_logging_recorder.py`

### Step 6: `TurnTracer` 改造（0.75h，问题：单一事实源对齐）
- `__init__(recorder=None)` + finally 中可选投递 `emit_turn_metric`
- 不破坏现有 `_logger.info` 行为
- 单元测试 `test_logging_tracer.py`

### Step 7: `query()` 透传 + `Agent` 挂载（1.5h）
- `query`/`_run_turns` 新增可选 `recorder` 参数 → 传给 TurnTracer
- `Agent._ensure_recorder` / `record_user_prompt` / 循环内 `record_message` / `aclose`
- 全部在 `logging.enabled` 守卫内；采样逻辑
- 集成测试 `test_logging_flow.py` / `test_logging_disabled.py`

### Step 8: `logging/reader.py`（2h）
- `load_log` / `iter_conversations` / `export_dataset` / `aggregate_metrics` /
  `cleanup_old_logs`
- 单元测试 `test_logging_reader.py`

### Step 9: 子 Agent / CLI / 隔离测试（1.5h，部分可选）
- CLI：`enabled` 开关接线、`_repl` finally `aclose`、`/logs` 命令
- 隔离 & resume 集成测试
- 子 Agent 日志 v1 仅预留字段（`agent_depth`/`is_sidechain`），完整实现留 Phase 2

### Step 10: 公开导出 + 回归 + 文档（1.5h）
- `logging/__init__.py` + 顶层 `agent_core/__init__.py` 导出
- 全量 pytest 回归（默认关闭无回归）+ 性能 smoke
- 更新 README / docs：`LoggingConfig` 用法、日志 schema、`export_dataset` 分析示例

---

## 6. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 日志写入阻塞对话热路径（流式小块高频） | 中 | 高 | 后台 asyncio.Queue + 单 drain task，emit 仅 put_nowait；I/O 移出热路径（§2.4） |
| 日志 sink 异常中断用户对话 | 低 | 高 | drain task 内全 try/except → warning + dropped；emit 永不抛；NullSink 兜底 |
| 进程退出时后台队列未排空丢日志 | 中 | 中 | `Agent.aclose()` 排空 + flush；CLI finally 调用；flush_every=1 强持久 |
| 与 persistence 文件混淆/互读 | 低 | 中 | 物理隔离 `~/.agent/logs` vs `~/.agent/projects`；envelope 字段不同名；隔离测试 |
| 敏感数据（密钥/PII）落盘 | 中 | 中 | `redact_keys` + `max_text_chars` 截断；`record_tool_io=False` 可整体关；默认 opt-in（enabled=False） |
| 长会话日志文件膨胀 | 低 | 低 | per_day 滚动选项；`cleanup_old_logs`（默认 90 天）；采样 sample_rate |
| content 事件 turn_index 与 metric 对齐偏差 | 低 | 低 | metric turn_index 权威；content 取最近 metric 近似；v1 可接受，Phase 2 精确化 |
| 默认开启偷偷写盘惊扰用户 | 低 | 中 | **默认 enabled=False**，必须显式开启（不变量 #3） |

---

## 7. 依赖关系图

```
Step 1 (events + config)
  ├── Step 2 (path)         [+ 复用 persistence.path.compute_project_hash]
  ├── Step 3 (redact)
  ├── Step 4 (sink) ──────────────┐
  │     └── Step 5 (recorder) ────┤
  │           ├── Step 6 (TurnTracer 改造)
  │           └── Step 7 (query 透传 + Agent 挂载)
  └── Step 8 (reader)  [依赖 Step 1 的 events]
Step 9 (CLI / 子Agent / 隔离测试) → after Step 7
Step 10 (导出 + 回归 + 文档)     → after Steps 1–9
```

Steps 2 / 3 / 8 可与 4–5 并行；Step 6 与 Step 7 紧邻。

---

## 8. 不变量（跨模块保证）

1. **旁路只读**：日志层从不修改 `AgentMessage` / messages / token 预算 / resume；
   移除日志层后 Agent 行为逐字节不变。
2. **存储隔离**：日志写 `~/.agent/logs/`，与 persistence 的 `~/.agent/projects/`
   物理隔离，互不读写删。
3. **默认关闭 / 零开销**：`logging.enabled=False`（默认）时不实例化 sink/recorder、
   不写盘、对现有测试与行为无任何影响。
4. **不可变 append-only**：日志永不重写/截断/压缩；一行一事件；`seq` 单调递增。
5. **失败不致命**：sink/recorder 任何异常被吞掉降级为 warning + dropped 计数，
   绝不向对话传播。
6. **崩溃语义**：`open()` 补尾换行守卫，崩溃最多坏最后一行；`flush_every=1` 时
   已 emit 且已 drain 的记录均已落盘。
7. **单一事实源**：turn metric 字段定义只在 `_TurnRecord.to_dict()` 一处，
   `TurnMetricRecord` 由它构造，不另立一套。
8. **可关联**：所有记录带 `trace_id`（+ 可选 `session_id`）与 `turn_index`，
   content 事件与 turn_metric 可在分析期 join。
```

