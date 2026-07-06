# Ctrl+C 优雅退出：进程中断时自动持久化（v1）

> **目标**：CLI 和 QQBot 在收到 SIGINT（Ctrl+C）或 SIGTERM 时，自动将当前会话状态保存到磁盘，下次启动可恢复。

---

> **状态**：📋 计划阶段，尚未实现。
>
> **审查记录**：
> - v1（初稿）— 2026-07-05，待审查

---

## 0. 背景与现状

### 0.1 当前 Ctrl+C 行为

**CLI (`_repl`)**：

```
_repl()
├── while True:
│   ├── input("agent-core> ")         ← Ctrl+C ① → break → finally 保存 ✅
│   └── Agent.stream()                ← Ctrl+C ② → "[cancelled]" → 继续 ❌
└── finally:
    ├── save_checkpoint()             ← 只在 break 或正常退出时执行
    └── end_persistence()
```

| 中断时机 | 当前行为 | checkpoint 保存 |
|----------|---------|----------------|
| `input()` 中 Ctrl+C | break → finally | ✅ |
| Agent 推理中 Ctrl+C | 取消本轮 → 返回 prompt | ❌ (需要再 Ctrl+C 一次退出) |
| `kill -9` | 立即终止 | ❌ |

**QQBot (`_cmd_start` → `channel.start`)**：

```python
# cli/qqbot.py
async def _cmd_start(...):
    channel = QQBotChannel(config)
    try:
        await channel.start()     # 阻塞在 asyncio.to_thread(botpy_loop)
    except KeyboardInterrupt:
        logger.info(...)
    # ❌ 没有 finally，没有 shutdown 调用
```

| 中断时机 | 当前行为 | checkpoint 保存 |
|----------|---------|----------------|
| Ctrl+C | 主 coroutine 被 cancel → 子线程 botpy 继续跑 | ❌ 全部丢失 |
| `kill` | 立即终止 | ❌ 全部丢失 |

### 0.2 关键架构约束

QQBot 和 CLI 的运行时结构不同，影响 shutdown 设计：

```
CLI:  asyncio.run(_main(...))
      └── 单 event loop，单线程
      └── finally 块天然执行

QQBot: asyncio.run(_cmd_start(...))
       └── 主 event loop (main thread)
           └── await channel.start()
               └── asyncio.to_thread(_run_bot)
                   └── 子线程 new_event_loop()  ← botpy 在这里面跑
                       └── client.start() 阻塞
       └── 主线程和子线程的 event loop 是隔离的
       └── botpy 的 on_*_message 回调在子线程 event loop 中执行
```

**核心问题**：QQBot 的 `SessionState` 对象、`asyncio.Lock` 都在 botpy 子线程的 event loop 中。主 event loop 被 cancel 后，不能跨 event loop 操作这些对象。需要在 shutdown 路径中**绕过 asyncio**，直接用同步 I/O 写 checkpoint 文件。

### 0.3 目标体验

**CLI**：
```
agent-core> 帮我分析这个bug    ← 用户发送消息
[Agent 推理中...]

^C                              ← ① 第一次 Ctrl+C：取消本轮
[cancelled]
agent-core> 帮我分析这个bug     ← ② 第二次 Ctrl+C（<2s）：紧急退出
[checkpoint saved — 12 messages]
(session saved, goodbye)
                                ← 退出，下次启动可从会话选择器恢复
```

**QQBot**：
```
$ agent-core-qqbot start
[qqbot] starting QQ bot...

^C                              ← Ctrl+C
[qqbot] shutting down...
[qqbot] saved 3 active sessions
[qqbot] goodbye
```

---

## 1. 设计决策

### 1.1 shutdown 的两层职责

| 层 | 位置 | 职责 |
|----|------|------|
| `AgentBridge.shutdown()` | `bridge/agent_bridge.py` | 遍历活跃 session，逐个调 evict 回调（持久化到磁盘） |
| 入口层的 `finally` 块 | `cli/_main.py`、`cli/qqbot.py` | 捕获信号 → 调 `bridge.shutdown()` |

**关键设计**：`AgentBridge.shutdown()` 使用**同步 I/O** 写 checkpoint 文件，不依赖 asyncio。原因：QQBot 的 `_fire_on_evicted` callbacks 是 async 的（调 `save_checkpoint` 用了 `aiofiles`），而 shutdown 可能在任意 event loop 上下文中被调用。改用一个 sync 版本的 checkpoint 写入即可。

### 1.2 为什么不用 async shutdown + signal handler

- `loop.add_signal_handler` 只在当前 event loop 有效。QQBot 有两个 event loop，主 loop 被 signal → 但 botpy 的 loop 在子线程收不到通知
- `asyncio.ensure_future(evict_callback)` 需要有效的 event loop，主 loop cancel 后无法调度
- **结论**：shutdown 路径全程同步 I/O，不依赖任何 event loop 状态

### 1.3 shutdown 中做什么、不做什么

| 做什么 | 不做什么 |
|--------|---------|
| 遍历 `_sessions` 字典 | 不尝试获取 `asyncio.Lock`（没意义，线程可能已死） |
| 有消息的 session → `json.dumps(checkpoint_dict())` → `open().write()` | 不尝试停止子线程（botpy/kill 自然终止） |
| 错误容忍：单个 session 写失败不阻塞其他 | 不发 `end_persistence`（JSONL writer 是 async 的，不能同步调） |

> **注意**：QQBot 的 JSONL 不会在 shutdown 时写入（需要 async writer）。但 checkpoint 文件（`.checkpoint.json`）包含 `workspace_override`、`permission_rules`、`title` 等恢复关键信息。对话转录的丢失是已知限制（计划书 6 节），不在此次改动范围内。

### 1.4 CLI 的双 Ctrl+C 策略

```
一次 Ctrl+C (Agent 执行中) → 取消本轮，返回 prompt
两次 Ctrl+C (< 2秒内)     → 紧急退出 + 保存 checkpoint
Ctrl+C (等待输入时)        → 直接退出 + 保存 (现有行为不变)
```

时间窗口用 `time.monotonic()` 差值判断。窗口只在这个 while 迭代的小局部生效——不影响其他正常的 Ctrl+C 处理。

---

## 2. 详细设计

### 2.1 `bridge/agent_bridge.py` — 新增 `shutdown()`

```python
# bridge/agent_bridge.py

import json
import os

class AgentBridge:
    # ... 现有代码不变 ...

    def shutdown(self) -> int:
        """同步关闭：遍历所有活跃 session，用同步 I/O 写入 checkpoint 文件。

        不依赖 asyncio event loop，可在任意线程中被 signal handler 调用。
        只保存有消息的 session（空 session 跳过）。

        Returns:
            成功保存的 session 数量。
        """
        from agent_core.persistence.path import compute_project_hash, checkpoint_json_path

        saved = 0
        project_hash = compute_project_hash(self._base.workspace_root)

        for sid, session in list(self._sessions.items()):
            if not session.messages:
                continue
            try:
                ckpt_key = sid.replace(":", "_").replace("\\", "_").replace("/", "_")
                ckpt_path = checkpoint_json_path(project_hash, ckpt_key)
                os.makedirs(os.path.dirname(ckpt_path), exist_ok=True)

                # 设置时间戳（如果尚未设置）
                from datetime import datetime
                now = datetime.now().isoformat()
                if session._created_at is None:
                    session._created_at = now
                session._last_active_at = now

                data = session.checkpoint_dict()
                with open(ckpt_path, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
                saved += 1
            except Exception:
                logger.exception("shutdown: failed to save checkpoint for %s", sid)

        self._sessions.clear()
        return saved
```

**设计要点**：
- 纯同步代码：`json.dump` + `open().write()`，无 `aiofiles`
- 对 `session.session_id` 做 sanitize（含 `:` 时替换为 `_`）— 因为 QQBot 的 session_id 格式是 `"channel_id:user_id"`
- 异常隔离：某个 session 写失败不影响其他
- 返回 saved 计数，方便 CLI 层打印日志

### 2.2 `cli/_main.py` — CLI 双 Ctrl+C

改动集中在 `_repl()` 函数：

```python
import time as _time_module

async def _repl(config, *, resume_id=None, continue_last=False):
    # ... 现有 setup 代码不变 ...

    _last_cancel_ts: float = 0.0  # 上一次 Ctrl+C (Agent 中) 的时间戳

    try:
        while True:
            # ... input() 逻辑不变 ...
            # Ctrl+C 在 input() 时：
            # except (EOFError, KeyboardInterrupt): break → finally 保存 ✅

            # ... 指令处理不变 ...

            agent = Agent(config, session=session)
            agent.on_permission_check(_permission_callback)
            stream = agent.stream(prompt)
            try:
                async for msg in stream:
                    # ... 消息处理不变 ...
                    pass
            except KeyboardInterrupt:
                now = _time_module.monotonic()
                if now - _last_cancel_ts < 2.0:
                    # 两次 Ctrl+C 间隔 < 2s → 紧急退出
                    print("\n[cancelled — exiting]")
                    break
                else:
                    _last_cancel_ts = now
                    print("\n[cancelled]")
            except Exception as exc:
                print(f"\n[exception] {type(exc).__name__}: {exc}")
            finally:
                await stream.aclose()
                await agent.aclose()

            # 正常完成一轮 → 重置计时器
            _last_cancel_ts = 0.0
    finally:
        await save_checkpoint(
            session, workspace_root=config.workspace_root,
        )
        await session.end_persistence()
```

**设计要点**：
- `_last_cancel_ts` 只在 Agent 推理中被 Ctrl+C 中断时设置
- 正常完成一轮后重置为 0.0（不累积跨轮次）
- 如果 Agent 在 2s 内被再次 Ctrl+C → break → finally 保存
- `input()` 中的 Ctrl+C 不受影响（直接 break）
- `/quit` 路径不受影响（直接 break + 保存）

### 2.3 `cli/qqbot.py` — QQBot 优雅退出

```python
import signal as _signal

async def _cmd_start(config_path, cli_overrides):
    config = _load_qqbot_config(config_path, cli_overrides)
    _setup_logging()
    logger = logging.getLogger("agent_core.qq_bot")

    base = _load_agent_config(config_path)
    logger.info(f"base agent config: model={base.model}")

    from ..channels.qq_bot.channel import QQBotChannel
    channel = QQBotChannel(config)

    # 注册信号处理器（用 signal.signal 而非 loop.add_signal_handler
    # 因为 botpy 在子线程有自己的 event loop）
    _shutting_down = False

    def _on_signal(signum, frame):
        nonlocal _shutting_down
        if _shutting_down:
            return
        _shutting_down = True
        logger.info("received signal %d, shutting down...", signum)

    _signal.signal(_signal.SIGINT, _on_signal)
    _signal.signal(_signal.SIGTERM, _on_signal)

    logger.info(f"starting QQ bot (app_id={config.app_id[:8]}...)")

    try:
        await channel.start()
    except KeyboardInterrupt:
        logger.info("received KeyboardInterrupt")
    except Exception:
        logger.exception("server crashed")
        sys.exit(1)
    finally:
        saved = channel._bridge.shutdown()
        if saved > 0:
            logger.info("shutdown: saved %d active session(s)", saved)
        else:
            logger.info("shutdown: no active sessions to save")
```

**设计要点**：
- 用 `signal.signal()`（Python 原生，跨线程生效），不用 `loop.add_signal_handler`
- `_shutting_down` 防重复：避免信号风暴导致多次 shutdown
- `_on_signal` 只是设置标志 + 打日志，不做重量操作。实际 shutdown 在 `finally` 中执行
- `finally` 保证退出路径一定走 shutdown
- `bridge.shutdown()` 是同步方法，可以在主 event loop cancel 之后安全调用

### 2.4 `channels/qq_bot/channel.py` — 无需改动

当前 `QQBotChannel` 的 checkpoint 相关代码（`_on_qqbot_session_evicted`、`_checkpoint_key`）无需修改。`AgentBridge.shutdown()` 方法直接写 checkpoint 文件，不经过这些回调。

### 2.5 `session/state.py` — 无需改动

`checkpoint_dict()` 已有，`shutdown()` 直接调用它拿到 JSON 数据。

---

## 3. 文件改动清单

| # | 文件 | 操作 | 说明 |
|---|------|------|------|
| 1 | `bridge/agent_bridge.py` | **修改** | `+shutdown()` 同步方法 |
| 2 | `cli/_main.py` | **修改** | `_repl()` 加双 Ctrl+C 检测 |
| 3 | `cli/qqbot.py` | **修改** | `_cmd_start()` 加 signal handler + `finally` shutdown |

**总计 3 个文件，无新增文件。**

---

## 4. 边界条件与测试覆盖

### 4.1 shutdown 数据完整性

| 场景 | 预期行为 |
|------|----------|
| 空 session | 跳过，不写文件 |
| 有消息的 session | 写入 `.checkpoint.json` |
| session_id 含 `:` | sanitize 为 `_` |
| checkpoint 目录不存在 | `os.makedirs` 自动创建 |
| 写文件失败（磁盘满等） | 记录 logger.exception，继续下一个 session |
| `_sessions` 为空 | shutdown 返回 0 |

### 4.2 CLI 双 Ctrl+C

| 场景 | 预期行为 |
|------|----------|
| Agent 中 Ctrl+C ×1 | 取消本轮，返回 prompt |
| Agent 中 Ctrl+C ×2 (<2s) | break → finally → save_checkpoint + 退出 |
| Agent 中 Ctrl+C ×2 (>2s) | 两次都是"取消本轮" |
| input() 中 Ctrl+C | 直接 break → finally 保存（不受双按逻辑影响） |
| 正常完成一轮后 Ctrl+C | 重置计时器，`_last_cancel_ts = 0.0` |

### 4.3 QQBot signal 处理

| 场景 | 预期行为 |
|------|----------|
| Ctrl+C 一次 | shutdown 保存所有活跃 session |
| Ctrl+C 两次（快速连按） | `_shutting_down` 防止重复，只 shutdown 一次 |
| SIGTERM | 同 SIGINT，shutdown 保存 |
| 正常退出（无异常） | `finally` 中的 shutdown 仍执行 |

### 4.4 测试计划

#### `tests/unit/bridge/test_agent_bridge.py`（+3）

```python
class TestShutdown:
    def test_shutdown_empty_sessions(self):
        """无活跃 session 时 shutdown 返回 0。"""

    def test_shutdown_saves_checkpoints(self, tmp_path, monkeypatch):
        """shutdown 为有消息的 session 写入 checkpoint 文件。"""

    def test_shutdown_sanitizes_session_id(self, tmp_path, monkeypatch):
        """session_id 含 ':' 时被替换为 '_'。"""
```

#### `tests/unit/test_shutdown.py`（新增，+4）

```python
class TestCliDoubleCtrlC:
    """通过模拟 KeyboardInterrupt 验证双 Ctrl+C 逻辑。
    不需要集成测试——只需验证 _last_cancel_ts 的计时逻辑。"""
    # 用 monkeypatch 替换 input()，用 mock Agent 触发 KeyboardInterrupt

class TestQqBotSignalHandler:
    """验证 _cmd_start 的 signal handler 注册和 finally 路径。"""
```

---

## 5. 实现顺序

| 阶段 | 内容 | 预估 |
|------|------|------|
| **Phase 1** | `bridge/agent_bridge.py` + `shutdown()` 同步方法 | 0.5h |
| **Phase 2** | `cli/_main.py` 双 Ctrl+C + `cli/qqbot.py` signal handler | 0.5h |
| **Phase 3** | 测试：bridge shutdown ×3 + 集成测试 ×4 | 1h |
| **合计** | | **2h** |

---

## 6. 不做的

- ❌ **不尝试在 shutdown 时写入 JSONL** — `SessionTranscriptWriter` 是 async 的，需要有效的 event loop
- ❌ **不尝试优雅停止 QQBot 子线程** — botpy 的 `client.start()` 是阻塞的，没有 clean stop API。线程随进程退出自然终止
- ❌ **不在 CLI 加 `SIGTERM` 处理** — CLI 本身运行时间短，且 `finally` 块已覆盖正常/异常退出。`kill` 时 Python 的 atexit 不足以可靠执行 finally
- ❌ **不做轮询式 shutdown** — `shutdown()` 是一次性快照遍历，不轮询等待 "botpy 线程结束"
- ❌ **不在 shutdown 中加锁** — process 即将退出，不存在并发，加锁无意义且可能死锁

---

## 7. 风险与缓解

| 风险 | 缓解 |
|------|------|
| shutdown 时 botpy 子线程仍在写 `self._sessions` | `list(self._sessions.items())` 先做快照，避免迭代中修改字典的 RuntimeError。数据竞争（读到一半写入的 session）可接受——这时进程正在退出，不要求完全一致 |
| `signal.signal` 全局注册，和其他库冲突 | `_on_signal` 内部幂等（`_shutting_down` guard），只在 `_cmd_start` 中注册，非全局常驻 |
| 同步 `open().write()` 阻塞主 event loop | shutdown 在 `finally` 中执行，此时 event loop 已 cancel 或即将退出，阻塞无影响 |
| checkpoint 文件写入中途被 SIGKILL | JSON 写入是原子的（先构建完整 dict 再 `json.dump`）。文件可能不完整但不会产生错误数据——下次 `load_checkpoint()` 捕获 `JSONDecodeError` 返回 `None` |
