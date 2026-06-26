# Item 11: 测试补齐、文档搭建、CI/CD — 实现计划书

> **状态** (2026-06-25)：部分完成。单元测试 ~957 passed / 10 skipped，mock 集成测试 14 文件全绿。剩余：vcrpy e2e 录制回放、4 个集成测试占位清理、MkDocs 文档、GitHub Actions CI/CD、examples 补齐。
>
> **预估工时**：3–4 天（实际约 2 天）
>
> **修订** (2026-06-25，对照实际代码库审查)：修正以下硬伤——① e2e 测试全部补 `@pytest.mark.asyncio`（项目无 `asyncio_mode=auto`，否则假通过）；② 去掉无插件支撑的 `@pytest.mark.vcr`，统一用手写 `vcr` fixture，并对缺失 cassette 明确 skip；③ `mkdocs.yml` 移到仓库根；④ 新增 `docs` 依赖组与 dev 组 `mypy`；⑤ `session_resume.py` 改用真实的 `SessionState` + `persistence.session` API（`AgentResult` 无 `session_id`）；⑥ `test_permission_denied` 改用破坏性工具 Write 并断言消息流中的被拒 ToolResult（而非 `stop_reason`）；⑦ 修 `test_compression_with_tool_calls` 的 f-string bug 并统一压缩软/硬断言策略；⑧ 增加 vcrpy SSE 可行性 spike 前置步骤。

---

## 0. 当前实际状态 vs Todo 描述

| 子项 | Todo 标记 | 实际状态 |
|------|----------|----------|
| vcrpy e2e 测试 | `[ ]` 桩已创建未录制 | **conftest + 2 个测试文件全 `pytest.skip`**，零测试函数，零录制 |
| 集成测试占位清理 | `[~]` 可删占位 | **4 文件全 `pytest.skip`**（覆盖面已由其他文件承接） |
| MkDocs 文档 | `[ ]` 未搭建 | **仅 2 页**：`docs/api/hooks.md` + `docs/guides/skills.md`，无 mkdocs.yml |
| pyproject.toml | `[ ]` 未勾选 | **已完成**（55 行，含依赖/scripts/build-system/pytest 配置） |
| README | `[x]` 已完成 | **119 行**，覆盖全部主题 |
| .github/workflows | 未提及 | **不存在** |
| examples/ | 未提及 | **仅 1 个**：`examples/skill_example.py` |

---

## 1. vcrpy 录制回放测试 (e2e)

### 1.1 目标

覆盖两条端到端路径，使用 vcrpy 录制真实 LLM API 调用，后续离线回放保证确定性回归。

### 1.1a 前置可行性验证（spike，必须先做）

> **风险**：Agent 走 `stream()` / SSE，anthropic SDK 基于 httpx。vcrpy 对 **httpx 异步 + 流式（SSE）响应**的支持历来不稳定，可能无法正确录制/回放分块响应。**在铺开 9 个用例前，必须先用 1 个最小流式请求验证 vcrpy 能录能放。**

```bash
# 录一条最简单的流式请求，再断网回放，确认 cassette 可用
VCR_MODE=record uv run pytest tests/e2e/test_turn_flow.py::TestTurnFlow::test_single_text_turn -v
# 断网/清空 key 后回放
unset ANTHROPIC_API_KEY DEEPSEEK_API_KEY OPENAI_API_KEY  # 仍能回放说明走的是 cassette
uv run pytest tests/e2e/test_turn_flow.py::TestTurnFlow::test_single_text_turn -v
```

若 vcrpy 无法处理 SSE：备选方案 A —— 改用 `httpx` 的 `MockTransport` 录制原始字节流；备选方案 B —— 退化为 MockModel 集成测试（已有覆盖），e2e 仅保留"真实 API 冒烟测试"且默认 `skip`（无 key 时）。**spike 失败则本节范围相应收缩，不强行推进。**

### 1.1b 异步测试约定（关键）

> 本项目 **未配置** `asyncio_mode = "auto"`，现有所有异步测试均**显式**标注 `@pytest.mark.asyncio`（见 `tests/unit/test_agent.py`）。因此本节**每个** `async def test_*` 方法都必须加 `@pytest.mark.asyncio`，否则 pytest 不会 await 协程，测试会"假通过"（仅产生 `coroutine never awaited` 警告，断言根本不执行）。下方代码已统一补齐该标记。
>
> 另：**不使用** `@pytest.mark.vcr` 标记 —— 该标记属于 `pytest-recording` 插件，本项目仅依赖裸 `vcrpy`，未装 `pytest-recording`。录制/回放统一通过 §1.3 的手写 `vcr` fixture 注入。

### 1.2 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `tests/e2e/__init__.py` | **保留** | 已存在（空文件） |
| `tests/e2e/conftest.py` | **重写** | vcrpy 配置 + fixtures + markers |
| `tests/e2e/test_turn_flow.py` | **重写** | 单轮文本 / 工具调用 / 多轮 / max_turns 路径 |
| `tests/e2e/test_compression.py` | **重写** | 压缩触发 → LLM 摘要 → 一致性验证路径 |
| `tests/e2e/cassettes/` | **新建目录** | vcrpy 录制存储（gitignore 排除 API key 泄露风险，但保留 cassettes 以支持离线回放） |

### 1.3 `tests/e2e/conftest.py` 设计

```python
"""E2E test fixtures with vcrpy recording/replay."""
import os
import pytest
from pathlib import Path

CASSETTE_DIR = Path(__file__).parent / "cassettes"

# ---- vcrpy 配置 ----
# 录制模式由环境变量 VCR_MODE 控制:
#   - 未设置 (默认): "none" — 只用已有 cassette，不发起网络请求
#   - "record": 重录指定 cassette（删除旧文件重新录制）
#   - "new_episodes": 追加新交互到已有 cassette

VCR_MODE = os.environ.get("VCR_MODE", "none")


@pytest.fixture(scope="module")
def vcr_config():
    """vcrpy 全局配置。

    过滤 Authorization header 防止 API key 泄露到 cassette 文件。
    """
    return {
        "filter_headers": ["authorization", "x-api-key", "api-key"],
        "filter_query_parameters": ["key", "api_key"],
        "decode_compressed_response": True,
    }


@pytest.fixture
def real_config():
    """构建使用真实 API 的 AgentConfig。

    从环境变量读取 API key，必须设置至少一个 provider 的 key。
    """
    from agent_core import AgentConfig

    api_keys = {}
    for env_var, provider in [
        ("ANTHROPIC_API_KEY", "anthropic"),
        ("DEEPSEEK_API_KEY", "deepseek"),
        ("OPENAI_API_KEY", "openai"),
    ]:
        key = os.environ.get(env_var)
        if key:
            api_keys[provider] = key

    if not api_keys:
        pytest.skip("No API key configured (set ANTHROPIC_API_KEY, DEEPSEEK_API_KEY, or OPENAI_API_KEY)")

    return AgentConfig(
        model="anthropic/claude-haiku-4-5",  # 最便宜模型，录制成本最低
        max_turns=5,
        api_keys=api_keys,
        permission_mode="bypassPermissions",
    )


@pytest.fixture
def vcr(request, vcr_config):
    """每个测试函数一个 cassette，自动以测试函数名命名。

    回放模式 (VCR_MODE=none) 下若 cassette 不存在，vcrpy 会在请求时抛异常。
    为了让"尚未录制"的测试给出明确 skip 而非晦涩报错，这里提前探测。
    """
    import vcr as vcrpy_lib

    cassette_name = f"{request.node.name}.yaml"
    cassette_path = CASSETTE_DIR / cassette_name

    # 回放模式且 cassette 缺失 → 明确 skip（首次需用 VCR_MODE=record 录制）
    if VCR_MODE == "none" and not cassette_path.exists():
        pytest.skip(
            f"cassette not recorded: {cassette_name} "
            "(run with VCR_MODE=record and an API key to record)"
        )

    my_vcr = vcrpy_lib.VCR(
        record_mode=VCR_MODE,
        cassette_library_dir=str(CASSETTE_DIR),
        match_on=["method", "scheme", "host", "port", "path", "query"],
        **vcr_config,
    )
    with my_vcr.use_cassette(cassette_name):
        yield
```

### 1.4 `tests/e2e/test_turn_flow.py` 测试用例

测试 6 条端到端路径（**每个方法都加 `@pytest.mark.asyncio` + `@pytest.mark.e2e`，并显式声明 `vcr` fixture 参数**；不使用 `@pytest.mark.vcr`，理由见 §1.1b）：

| # | 测试函数 | 场景 | 断言点 |
|---|---------|------|--------|
| 1 | `test_single_text_turn` | 纯文本问答，无工具调用 | `AgentResult.stop_reason == "end_turn"`，`content` 非空，`tool_uses == 0` |
| 2 | `test_read_file_tool` | 单轮工具调用：Read 工具读取文件 | `tool_uses >= 1`，`ToolResult` 正常返回 |
| 3 | `test_multi_turn` | 多轮交互：先 Glob 再 Read | `tool_uses >= 2`，`content` 非空 |
| 4 | `test_max_turns_exhaustion` | max_turns=1 但 LLM 持续要求工具调用 | `stop_reason == "max_turns"` |
| 5 | `test_streaming` | stream() 路径：逐条消费 AgentMessage | 至少产出 1 条 `AssistantText` + 1 条 `DoneMessage` |
| 6 | `test_permission_denied` | permission_mode="ask" + 无回调 + **破坏性工具(Write)** → 工具被拒 | 消息流中出现 `ToolResult(is_error=True, content 含 "Permission denied")` |

> **#6 说明（两处都要改对）**：
> 1. 原计划用 Read 工具触发拒绝是**错的** —— Read 是只读工具，权限链 step 6 自动放行，永远不会被拒。必须用**破坏性工具**（Write/Edit/Bash）：`permission_mode="ask"` 且无权限回调时，破坏性工具被默认拒绝（见 `permissions/engine.py:_ask`）。
> 2. 原断言 `stop_reason == "permission_denied"` 也**错** —— 工具被拒只是返回一个 `is_error=True` 的 `ToolResult` 喂回 LLM，回合仍以 `end_turn` 结束（`query/loop.py:752`）；`permission_denied` 这个 StopReason 来自 **provider 级**的停止原因归一化，**不由本地权限引擎产生**。因此正确做法是用 `stream()` 在消息流里查找被拒的 `ToolResult`，而非断言 `stop_reason`。

```python
"""End-to-end tests with vcrpy recording/replay for Agent turn flow."""
import pytest


@pytest.mark.e2e
class TestTurnFlow:
    """Core turn flow scenarios with real LLM API."""

    @pytest.mark.asyncio
    async def test_single_text_turn(self, real_config, vcr):
        """Pure text Q&A — no tool calls, immediate end_turn."""
        from agent_core import Agent

        agent = Agent(real_config)
        result = await agent.run("Hello! Reply with just 'Hello, world!' in English.")

        assert result.stop_reason == "end_turn"
        assert len(result.content) > 0
        assert "hello" in result.content.lower()

    @pytest.mark.asyncio
    async def test_read_file_tool(self, real_config, vcr, tmp_path):
        """Agent uses Read tool to read a temp file."""
        from agent_core import Agent

        # 创建测试文件
        test_file = tmp_path / "hello.txt"
        test_file.write_text("Hello from e2e test!", encoding="utf-8")

        config = real_config.model_copy(update={"max_turns": 5})
        agent = Agent(config)

        prompt = f"Read the file at {test_file} and tell me what it says. Just give me the content."
        result = await agent.run(prompt)

        assert result.tool_uses >= 1
        assert "Hello from e2e test" in result.content

    @pytest.mark.asyncio
    async def test_multi_turn(self, real_config, vcr, tmp_path):
        """Multi-turn: Glob to find files, then Read one."""
        from agent_core import Agent

        # 创建测试文件结构
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("print('hello')")

        config = real_config.model_copy(update={"max_turns": 10})
        agent = Agent(config)

        prompt = (
            f"List all Python files in {tmp_path} using Glob, "
            f"then read one of them and tell me what it does."
        )
        result = await agent.run(prompt)

        assert result.tool_uses >= 2
        assert "print" in result.content.lower() or "hello" in result.content.lower()

    @pytest.mark.asyncio
    async def test_max_turns_exhaustion(self, real_config, vcr):
        """max_turns=1 with tool-use request → max_turns stop."""
        from agent_core import Agent

        config = real_config.model_copy(update={"max_turns": 1})
        agent = Agent(config)

        # 这个 prompt 极可能触发工具调用（Read），所以 1 turn 不够
        result = await agent.run("Read the file at /nonexistent/path.txt")
        # 可能是 max_turns 或 end_turn（取决于 LLM 是否直接拒绝）
        assert result.stop_reason in ("max_turns", "end_turn")

    @pytest.mark.asyncio
    async def test_streaming(self, real_config, vcr):
        """Stream path yields correct AgentMessage types."""
        from agent_core import Agent, AssistantText
        from agent_core.models import DoneMessage

        agent = Agent(real_config)

        messages = []
        async for msg in agent.stream("Say 'hello' and nothing else."):
            messages.append(msg)

        assert len(messages) >= 2
        # 最后一条必须是 DoneMessage
        assert messages[-1].type == "done"
        # 至少有一条 AssistantText
        assert any(m.type == "assistant_text" for m in messages)

    @pytest.mark.asyncio
    async def test_permission_denied(self, real_config, vcr, tmp_path):
        """permission_mode=ask without callback + destructive tool → denied.

        必须用破坏性工具(Write)：Read 只读会被权限链 step 6 自动放行。
        拒绝表现为消息流里的 is_error ToolResult，而非 stop_reason —— 详见 §1.4 #6 说明。
        """
        from agent_core import Agent

        target = tmp_path / "out.txt"

        config = real_config.model_copy(update={
            "permission_mode": "ask",   # 无回调 → 破坏性工具被拒
            "max_turns": 3,
        })
        agent = Agent(config)
        # 不注册 on_permission_check → Write 在 ask 模式下被拒绝

        denied = False
        async for msg in agent.stream(
            f"Write the text 'hello' to the file at {target}."
        ):
            if (
                msg.type == "tool_result"
                and msg.is_error
                and "Permission denied" in str(msg.content)
            ):
                denied = True

        assert denied, "expected a denied ToolResult for the Write tool"
        # 文件确实没被写入
        assert not target.exists()
```

### 1.5 `tests/e2e/test_compression.py` 测试用例

测试 3 条压缩路径（**需要长对话触发压缩**；每个方法都加 `@pytest.mark.asyncio` + `@pytest.mark.e2e`）：

| # | 测试函数 | 场景 | 断言点 |
|---|---------|------|--------|
| 1 | `test_compression_triggered` | 长对话超阈值 → 压缩触发 | 若有 `CompactMessage(status="completed")` 则 `compression_ratio < 1.0`（软断言，见下） |
| 2 | `test_compression_with_tool_calls` | 含工具调用的长对话压缩 | 压缩后 `ToolResult` 可追溯，`CompactMessage` 正常（软断言） |
| 3 | `test_compression_disabled` | `CompressionConfig(enabled=False)` → 走硬截断 | 无 `CompactMessage(status="started")` |

> **断言策略统一（修原计划自相矛盾）**：压缩是否触发取决于真实 token 量，**回放阶段由 cassette 固定**，但**录制阶段不确定**。原计划 #1 用软断言、#2 用硬断言 `assert compact_events` 自相矛盾。统一规则：
> - **录制时（VCR_MODE=record）**：压缩可能不触发，所有"是否触发"一律用**软断言**（`if compact_events:` 再校验内部一致性），避免录制失败。
> - **回放时**：cassette 已固化交互，行为确定。若需要"必然触发"的强校验，应**单独**用一个**预先确认会触发**的 cassette（录制时人工确认 `compact_events` 非空后再提交），并仅在该 cassette 存在时强断言。
> - `test_compression_disabled` 是唯一可无条件硬断言的（禁用压缩 → 必无 `status="started"`），因为它不依赖 token 阈值。

```python
"""End-to-end tests with vcrpy for compression flow."""
import pytest


@pytest.mark.e2e
class TestCompressionE2E:
    """Compression flow with real LLM (needs enough turns to exceed threshold)."""

    @pytest.mark.asyncio
    async def test_compression_triggered(self, real_config, vcr, tmp_path):
        """Long conversation triggers auto-compression."""
        from agent_core import Agent, AgentConfig, CompressionConfig, CompactMessage

        # 创建大文件让对话变长
        big_file = tmp_path / "big.py"
        big_file.write_text("\n".join(
            f"# line {i}: x = {i} * 2" for i in range(500)
        ))

        config = real_config.model_copy(update={
            "max_turns": 15,
            "compression": CompressionConfig(
                enabled=True,
                keep_recent_rounds=2,
                summary_max_tokens=1024,
            ),
        })
        agent = Agent(config)

        compact_events = []
        async for msg in agent.stream(
            f"Read {big_file}, then explain what each section does. "
            "Ask multiple follow-up questions."
        ):
            if isinstance(msg, CompactMessage):
                compact_events.append(msg)

        # 可能触发压缩（取决于实际对话长度）
        if compact_events:
            completed = [e for e in compact_events if e.status == "completed"]
            if completed:
                assert completed[0].rounds_compressed > 0
                assert completed[0].compression_ratio < 1.0

    @pytest.mark.asyncio
    async def test_compression_with_tool_calls(self, real_config, vcr, tmp_path):
        """Compression with tool-call rounds preserves tool traceability."""
        from agent_core import Agent, AgentConfig, CompressionConfig, CompactMessage

        # 创建多个文件，制造多轮工具调用
        for i in range(10):
            (tmp_path / f"file_{i}.txt").write_text(f"Content of file {i}")

        config = real_config.model_copy(update={
            "max_turns": 20,
            "compression": CompressionConfig(
                enabled=True,
                keep_recent_rounds=3,
                summary_max_tokens=1024,
            ),
        })
        agent = Agent(config)

        compact_events = []
        # 注意：两行字符串都要带 f 前缀，否则第二行的 {tmp_path} 不会被插值
        async for msg in agent.stream(
            f"Read each file in {tmp_path} one by one, and after reading all of them, "
            f"write a summary to {tmp_path}/summary.txt."
        ):
            if isinstance(msg, CompactMessage):
                compact_events.append(msg)

        # 软断言：压缩是否触发取决于真实 token 量，不强制（见 §1.5 断言策略）。
        # 若触发了，则校验内部一致性。
        completed = [e for e in compact_events if e.status == "completed"]
        for e in completed:
            assert e.compression_ratio < 1.0
            assert e.rounds_compressed > 0

    @pytest.mark.asyncio
    async def test_compression_disabled(self, real_config, vcr, tmp_path):
        """Disabled compression → no CompactMessage events."""
        from agent_core import Agent, AgentConfig, CompressionConfig, CompactMessage

        big_file = tmp_path / "data.txt"
        big_file.write_text("\n".join(f"Line {i}: data" for i in range(500)))

        config = real_config.model_copy(update={
            "max_turns": 10,
            "compression": CompressionConfig(enabled=False),
        })
        agent = Agent(config)

        compact_events = []
        async for msg in agent.stream(
            f"Read {big_file} and analyze its content in detail."
        ):
            if isinstance(msg, CompactMessage):
                compact_events.append(msg)

        # 压缩禁用时应无 CompactMessage
        started = [e for e in compact_events if e.status == "started"]
        assert len(started) == 0
```

### 1.6 录制流程

```bash
# 1. 设置 API key 环境变量
export ANTHROPIC_API_KEY="sk-ant-xxx"
# 或
export DEEPSEEK_API_KEY="sk-xxx"

# 2. 录制模式执行（首次 + 后续重录）
VCR_MODE=record uv run pytest tests/e2e/ -v -m e2e

# 3. 回放模式（CI / 无网络环境）
uv run pytest tests/e2e/ -v -m e2e
# 默认 VCR_MODE=none → 只用已有 cassette

# 4. 追加模式（加新测试不重录已有）
VCR_MODE=new_episodes uv run pytest tests/e2e/ -v -m e2e
```

### 1.7 `.gitignore` 调整

Cassette 文件**提交到仓库**（不含 API key — vcrpy 已过滤 Authorization header），保证 CI 无需 API key 即可运行 e2e 测试。

```gitignore
# vcrpy cassettes — API keys already filtered by vcr config
# Keep cassettes in repo for offline replay
```

### 1.8 验证标准

- [ ] `pytest tests/e2e/ -v -m e2e` 全部 PASS（回放模式，无网络）
- [ ] 每个 cassette `.yaml` 文件不含 `authorization` / `x-api-key` / `api-key` header
- [ ] cassette 文件 < 1MB（单个测试录制体积合理）

---

## 2. 集成测试占位清理

### 2.1 目标

清理 4 个空占位文件，其覆盖面已由以下文件承接：

| 占位文件 | 覆盖面已由…承接 |
|----------|----------------|
| `tests/integration/test_tool_pipeline.py` | `tests/unit/test_loop_helpers.py` (消息构建器 + `run_tool_use`/`run_tools`) + `tests/unit/test_tools_edge_cases.py` (混合并发/持久化/设备文件/错误清理) |
| `tests/integration/test_subagent.py` | `tests/integration/test_run_agent.py` (461 行：全流程 + sidechain + drain + fork/resume) |
| `tests/integration/test_fork.py` | `tests/unit/test_agent_fork.py` (filter_incomplete_tool_calls / Fork 不可变) + `tests/integration/test_run_agent.py` (resume from sidechain) |
| `tests/integration/test_permission_inheritance.py` | `tests/unit/test_permissions.py` (子 Agent 权限继承 6 个场景) + `tests/unit/test_agent_resolve.py` (27 个 resolve 测试含权限构建) |

### 2.2 操作

**直接删除** 4 个文件（当前全是 1–2 行 `pytest.skip`）：

```
rm tests/integration/test_tool_pipeline.py
rm tests/integration/test_subagent.py
rm tests/integration/test_fork.py
rm tests/integration/test_permission_inheritance.py
```

同时清理 `tests/integration/conftest.py`（当前仅含注释，无实际 fixture）：

```
# 当前内容 (2 行):
# # Integration test fixtures (MockModel, etc.) -- to be implemented
# (空行)
```

`conftest.py` 保留为**空文件**（pytest 仍需它标记目录），但不包含注释，避免误导。

### 2.3 验证标准

- [ ] 删除后 `pytest tests/ -v` 测试总数不变（因被删文件全是 `pytest.skip`，不贡献 passed/failed 计数）
- [ ] `git status` 仅显示 4 个 deleted + conftest.py modified
- [ ] 确认删除 `test_tool_pipeline.py` 后不损失覆盖（已在 todo 中明确标注）

---

## 3. MkDocs Material API 文档

### 3.1 目标

搭建完整 MkDocs Material 文档站点，覆盖所有公开 API 和使用指南。

### 3.2 文件清单

> **依赖前置（必须先做）**：MkDocs 相关包**当前不在** `pyproject.toml`（`[dependency-groups].dev` 只有 pytest/pytest-asyncio/vcrpy）。需新增一个 `docs` 依赖组，否则 `mkdocs build` 找不到模块：
>
> ```toml
> [dependency-groups]
> # ... dev 组保持不变 ...
> docs = [
>     "mkdocs>=1.6",
>     "mkdocs-material>=9.5",
>     "mkdocstrings[python]>=0.26",
> ]
> ```
>
> 构建命令相应改为 `uv sync --group docs` 后再 `uv run mkdocs build`。

| 文件 | 操作 | 说明 |
|------|------|------|
| `mkdocs.yml` | **新建** | MkDocs 配置，置于**仓库根**（不是 `docs/` 下，见 §3.3 说明） |
| `docs/index.md` | **新建** | 首页：简介 + 快速安装 + 核心概念 |
| `docs/quickstart.md` | **新建** | 5 分钟快速开始 |
| `docs/api/agent.md` | **新建** | Agent 类 API + AgentConfig + AgentResult |
| `docs/api/tools.md` | **新建** | 6 个内置工具 + Tool 基类 + 注册表 |
| `docs/api/hooks.md` | **保留** | 已存在（85 行），适当补充 |
| `docs/api/permissions.md` | **新建** | PermissionEngine + 规则 + 继承 |
| `docs/guides/subagent.md` | **新建** | AgentDefinition + run_agent + AgentTool |
| `docs/guides/mcp.md` | **新建** | MCP 配置 + 服务端 + 工具桥接 |
| `docs/guides/skills.md` | **保留** | 已存在（121 行），适当补充 |

### 3.3 `mkdocs.yml` 设计（置于仓库根）

> **位置很重要**：MkDocs 约定 `mkdocs.yml` 在**仓库根**，`docs_dir` 默认指向 `docs/`。若按原计划放 `docs/mkdocs.yml`，`docs_dir` 会变成 `docs/docs/`，nav 里的 `api/agent.md` 全部找不到，`mkdocstrings` 的 `paths: [src]` 也会指向 `docs/src` → `mkdocs build` 直接失败。下面的 `nav` 路径（`index.md`、`api/agent.md`）和 `paths: [src]` 都是相对**仓库根**的，必须放根目录。

```yaml
site_name: agent-core
site_description: Pure Python Agent core library
repo_url: https://github.com/xp271/PU
theme:
  name: material
  features:
    - navigation.sections
    - navigation.expand
    - search.highlight
    - content.code.copy
  palette:
    - media: "(prefers-color-scheme: dark)"
      scheme: slate
      primary: indigo
      accent: indigo
    - media: "(prefers-color-scheme: light)"
      scheme: default
      primary: indigo
      accent: indigo

nav:
  - Home: index.md
  - Quickstart: quickstart.md
  - API Reference:
    - Agent: api/agent.md
    - Tools: api/tools.md
    - Permissions: api/permissions.md
    - Hooks: api/hooks.md
  - Guides:
    - Sub-Agent: guides/subagent.md
    - MCP Integration: guides/mcp.md
    - Skills: guides/skills.md

markdown_extensions:
  - admonition
  - pymdownx.highlight:
      anchor_linenums: true
  - pymdownx.superfences
  - pymdownx.inlinehilite
  - pymdownx.snippets
  - toc:
      permalink: true

plugins:
  - search
  - mkdocstrings:
      handlers:
        python:
          paths: [src]
          options:
            show_source: true
            show_root_heading: true
```

### 3.4 `docs/index.md` 内容要点

- 一句话定位："`Agent(config).run(prompt)`"
- 状态：~957 tests passed，3 provider，MCP/Skills/Hooks/SubAgent 就绪
- 安装命令 `uv add agent-core`（或 `pip install agent-core`）
- 6 个核心能力卡片：多 Provider、流式响应、工具管线、MCP、子 Agent、Hooks/Skills
- 链接到 quickstart + API

### 3.5 `docs/quickstart.md` 内容要点

- 最小 5 行代码
- 3 个示例：纯文本 / 流式 / 权限回调
- 配置说明（model / api_keys / max_turns / permission_mode）
- CLI 启动方式

### 3.6 `docs/api/agent.md` 内容要点

- Agent 类完整 API（`__init__` / `run` / `stream` / `on_permission_check`）
- AgentConfig 全部字段 + 默认值表
- AgentResult 字段
- AgentMessage union 类型（6 种变体）
- ModelProvider / ModelId 用法
- CompactMessage / CompressionConfig

### 3.7 `docs/api/tools.md` 内容要点

- Tool 基类 API（`call` / `validate_input` / `input_schema` / `output_schema`）
- 6 个内置工具（Read/Edit/Write/Glob/Grep/Bash）的 Input 模型 + 行为说明
- ToolUseContext / ToolCallResult 数据模型
- `@register_tool` / `get_tool()` / `registered_tools()` 注册表 API
- AgentTool（子 Agent 调度）+ SkillTool

### 3.8 `docs/api/permissions.md` 内容要点

- PermissionEngine 7 步判定链
- PermissionMode 三种模式 + 行为矩阵
- PermissionRule 匹配优先级
- 子 Agent 权限继承合约（3 个纯函数不变量）
- Bash AST 安全检查（危险模式表）

### 3.9 `docs/guides/subagent.md` 内容要点

- AgentDefinition 概念 + 字段表
- 内置 3 个 Agent（GeneralPurpose/Explore/Plan）属性
- 注册 + 加载（markdown/JSON/discover）
- run_agent() 执行引擎 + 隔离环境
- AgentTool 路由（同步/异步/Fork/恢复）
- 深度闸 + 权限下降

### 3.10 `docs/guides/mcp.md` 内容要点

- MCP 概念 + `.mcp.json` 配置格式
- 3 种 transport（stdio/sse/websocket）
- 工具名字空间化规则
- include_tools / exclude_tools 过滤
- OAuth 配置
- 启动预检 + 故障排查表

### 3.11 验证标准

- [ ] `pyproject.toml` 已加 `docs` 依赖组（mkdocs / mkdocs-material / mkdocstrings[python]）
- [ ] `uv sync --group docs && uv run mkdocs build` 无错误退出（`mkdocs.yml` 在仓库根）
- [ ] `uv run mkdocs serve` 本地可浏览
- [ ] 所有 API 页面含可运行代码示例
- [ ] 导航栏结构完整（首页 / Quickstart / API × 4 / Guides × 3）

---

## 4. GitHub Actions CI/CD

### 4.1 目标

CI：push/PR 触发 pytest + 类型检查。

### 4.2 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `.github/workflows/ci.yml` | **新建** | pytest + mypy 类型检查 |

### 4.3 `ci.yml` 设计

> **依赖前置**：`mypy` 当前**不在** dev 依赖里，`uv run mypy` 会报"未安装"。需把它加入 `[dependency-groups].dev`（与 ruff 不同，ruff 用 `uvx` 临时拉取即可，mypy 需要项目依赖在环境内才能正确解析类型，故应进 dev 组）：
>
> ```toml
> dev = [
>     "pytest>=9.0.3",
>     "pytest-asyncio>=1.4.0",
>     "vcrpy>=7",
>     "mypy>=1.13",
> ]
> ```
>
> **e2e 步骤顺序**：CI 的 e2e 步骤依赖已提交的 cassette。**实施顺序必须保证"先录 cassette 并提交 → 再启用 CI 的 e2e 步骤"**（见 §6）。若 cassette 尚未就绪就合入 CI，回放模式（`VCR_MODE=none`）下 §1.3 fixture 会对缺失 cassette 的用例 `skip`（不会让 CI 变红），但这意味着 e2e 实际未生效——需在 cassette 提交后确认这些用例真正 PASS 而非全 skip。

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - uses: astral-sh/setup-uv@v5
      - name: Install dependencies
        run: uv sync --dev
      - name: Install ripgrep (for GrepTool tests)
        run: sudo apt-get install -y ripgrep
      - name: Unit + Integration tests
        run: uv run pytest tests/unit tests/integration -v --tb=short
      - name: E2E tests (cassette replay, no API key)
        run: uv run pytest tests/e2e -v -m e2e --tb=short
      - name: Type check (mypy)
        run: uv run mypy src/agent_core --ignore-missing-imports

  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - name: Ruff lint
        run: uvx ruff check src/ tests/
```

### 4.4 验证标准

- [ ] CI workflow 在 push 时通过（pytest + mypy）

---

## 5. Examples 目录补齐

### 5.1 目标

提供 4 个开箱即用的示例脚本，与 README 中的代码片段一一对应但更详细。

### 5.2 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `examples/skill_example.py` | **保留** | 已存在（79 行） |
| `examples/basic_run.py` | **新建** | 最小 Agent.run() 示例 |
| `examples/streaming.py` | **新建** | Agent.stream() 逐条消费示例 |
| `examples/permission_callback.py` | **新建** | 权限回调注入示例 |
| `examples/session_resume.py` | **新建** | 会话持久化 + 恢复示例 |

### 5.3 内容要点

**`basic_run.py`** (~15 行)：
- `AgentConfig(model=..., api_keys=...)` → `Agent(config)` → `await agent.run("...")`
- 打印 `result.content` / `result.total_tokens` / `result.stop_reason`

**`streaming.py`** (~25 行)：
- `async for msg in agent.stream(prompt):` → `match msg.type:` 分支
- 展示实时打印 `assistant_text`，工具调用日志

**`permission_callback.py`** (~25 行)：
- `async def my_permission(tool_name, tool_input) -> bool:`
- `agent.on_permission_check(my_permission)`
- 展示工具批准/拒绝逻辑

**`session_resume.py`** (~45 行)：

> **原计划的 API 不存在**，必须按真实接口改写：
> - `AgentResult` **没有** `session_id` 字段 —— 不能"从 result 获取 session_id"。
> - `list_sessions()` / `load_session()` **不是顶层公开 API**，它们在 `agent_core.persistence.session` 内部模块。
> - 持久化/恢复的真实路径是：构造 `SessionState`（携带 `session_id`）→ `session.begin_persistence(path)` 落盘 → 把 `session` 传给 `Agent(config, session=session)` → 下次用 `load_session(session_id, ...)` 还原 `SessionState` 再传回 `Agent`（参见 `cli.py` 的 REPL 实现）。

真实写法（示例）：

```python
import asyncio
from agent_core import Agent, AgentConfig
from agent_core.session import SessionState
from agent_core.persistence.path import compute_project_hash, session_jsonl_path
from agent_core.persistence.session import list_sessions, load_session


async def main():
    config = AgentConfig(model="anthropic/claude-haiku-4-5")
    ph = compute_project_hash(config.workspace_root)

    # 1) 首轮：新建 SessionState 并开启持久化
    session = SessionState()
    sid = session.session_id
    await session.begin_persistence(session_jsonl_path(ph, sid))
    agent = Agent(config, session=session)
    await agent.run("My name is Ada. Remember it.")
    await session.end_persistence()

    # 2) 列出已保存会话
    for info in await list_sessions(config.workspace_root):
        print(info.session_id, getattr(info, "message_count", "?"))

    # 3) 恢复：load_session 还原 SessionState，再交给新的 Agent
    restored = await load_session(sid, workspace_root=config.workspace_root)
    assert restored is not None
    agent2 = Agent(config, session=restored)
    result = await agent2.run("What is my name?")
    print(result.content)  # 应包含 "Ada"


asyncio.run(main())
```

- 注释里要点明 `list_sessions` / `load_session` 目前是内部模块路径，未来若稳定可考虑顶层导出。

### 5.4 验证标准

- [ ] 每个脚本可独立运行：`uv run python examples/basic_run.py`
- [ ] 每个脚本有清晰的注释说明预期行为

---

## 6. 实施顺序

```
第 0 步 (0.5h)   依赖前置：pyproject 加 docs 组 + dev 组加 mypy
第 1 步 (0.5h)   清理集成测试占位 (4 文件删除 + conftest.py)
第 2 步 (0.5h)   ⚠ vcrpy SSE 可行性 spike（§1.1a）—— 录 1 条流式回放成功才继续，
                  失败则按备选方案收缩 e2e 范围
第 3 步 (2h)     vcrpy e2e 测试实现 (conftest + 9 个测试函数，全部带 @pytest.mark.asyncio + 录制)
第 4 步 (2h)     MkDocs 文档搭建 (仓库根 mkdocs.yml + 8 个页面)
第 5 步 (0.5h)   examples 补齐 (4 个脚本，session_resume 用真实 API)
第 6 步 (0.5h)   CI workflow（ci.yml）—— 必须在 cassette 已提交后再启用 e2e 步骤
第 7 步 (0.5h)   最终验证（全量 pytest + mkdocs build + CI 模拟 + 确认 e2e 真 PASS 非全 skip）
```

总计 ~7 小时（spike 顺利时；spike 失败需重新评估 e2e 范围）。

---

## 7. 风险 & 注意事项

| 风险 | 缓解 |
|------|------|
| **vcrpy 可能无法录制 httpx 异步 SSE 流** | §1.1a 前置 spike 先验证；失败则按备选方案（MockTransport 或退化为 MockModel + 冒烟 skip）收缩范围 |
| **异步测试漏标 `@pytest.mark.asyncio` → 假通过** | 本项目无 `asyncio_mode=auto`，所有 `async def test_*` 必须显式标注（§1.1b 已统一补齐）；CI 用 `-W error::RuntimeWarning` 可让"coroutine never awaited"变红 |
| vcrpy 录制需要真实 API key（产生少量费用） | 使用 `claude-haiku-4-5` 最小模型，预计总费用 < $0.50 |
| vcrpy cassette 含敏感数据 | `filter_headers` 过滤 Authorization；录制后人工抽查 |
| **MkDocs 依赖未声明 / mkdocs.yml 位置错** | 先加 `docs` 依赖组并 `uv sync --group docs`；`mkdocs.yml` 必须放仓库根（§3.3） |
| **mypy 未在 dev 依赖** | dev 组加 `mypy>=1.13`（§4.3） |
| CI e2e 步骤在 cassette 就绪前会全 skip（隐性失效） | 实施顺序保证先录后开 CI；最终验证确认 e2e 真 PASS 非全 skip |
| CI 中 ripgrep 不可用（非 ubuntu runner） | ci.yml 添加 `apt-get install ripgrep`；GrepTool 测试已有 skip-if-unavailable 逻辑 |
