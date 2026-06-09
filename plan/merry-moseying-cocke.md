# 测试目录重构方案

## Context

当前测试目录只有 2 个文件（`test_agent.py` 341 行, `test_tools.py` 1363 行），所有测试类混在一起。改造目标：
- 对齐 todo 文档中的 `unit/` / `integration/` / `e2e/` 三层结构
- 拆分大文件为按工具/领域粒度的小文件，方便用文件名或 marker 筛选
- 为未来的 integration 和 e2e 测试预留 scaffolding

---

## 1. 目标目录结构

```
tests/
├── __init__.py                         # 保留不变
├── conftest.py                         # 共享 fixtures（保留不变）
│
├── unit/
│   ├── __init__.py                     # 新建：空文件
│   ├── conftest.py                     # 新建：单元测试 fixtures（初始空）
│   │
│   ├── test_models.py                  # 从 test_agent.py 拆分：TestClaudeModel, TestAgentConfig
│   ├── test_messages.py                # 从 test_agent.py 拆分：TestAgentResult, TestAgentMessage
│   ├── test_agent.py                   # 从 test_agent.py 拆分：TestAgent
│   │
│   ├── test_tools_registry.py          # 从 test_tools.py 拆分：TestToolRegistry, TestToolBase
│   ├── test_tools_context.py           # 从 test_tools.py 拆分：TestToolUseContext, TestToolCallResult
│   ├── test_tools_read.py              # 从 test_tools.py 拆分：TestFileReadTool, TestSafeDecodeBytes
│   ├── test_tools_write.py             # 从 test_tools.py 拆分：TestFileWriteTool
│   ├── test_tools_edit.py              # 从 test_tools.py 拆分：TestFileEditTool
│   ├── test_tools_glob.py              # 从 test_tools.py 拆分：TestGlobTool
│   ├── test_tools_grep.py              # 从 test_tools.py 拆分：TestGrepTool
│   ├── test_tools_bash.py              # 从 test_tools.py 拆分：TestBashTool
│   ├── test_tools_workspace.py         # 从 test_tools.py 拆分：TestWorkspaceBoundary
│   ├── test_tools_attributes.py        # 从 test_tools.py 拆分：TestToolClassAttributes
│   │
│   ├── test_permissions.py             # 新建：stub，待 LLM 权限引擎实现
│   ├── test_hooks.py                   # 新建：stub，待 Hooks 系统实现
│   └── test_normalize.py               # 新建：stub，待消息规范化实现
│
├── integration/
│   ├── __init__.py                     # 新建：空文件
│   ├── conftest.py                     # 新建：MockModel fixture
│   ├── mock_model.py                   # 新建：MockModel 实现（stub）
│   ├── test_tool_pipeline.py           # 新建：stub
│   ├── test_subagent.py                # 新建：stub
│   ├── test_fork.py                    # 新建：stub
│   └── test_permission_inheritance.py  # 新建：stub
│
└── e2e/
    ├── __init__.py                     # 新建：空文件
    ├── conftest.py                     # 新建：vcrpy fixtures
    ├── cassettes/                      # 新建：vcrpy 录制目录
    ├── test_turn_flow.py               # 新建：stub
    └── test_compression.py             # 新建：stub
```

---

## 2. 拆分映射表

### test_agent.py → unit/

| 原 class | 目标文件 | 行数 |
|----------|----------|------|
| TestClaudeModel | unit/test_models.py | ~28 |
| TestAgentConfig | unit/test_models.py | ~68 |
| TestAgentResult | unit/test_messages.py | ~48 |
| TestAgentMessage | unit/test_messages.py | ~143 |
| TestAgent | unit/test_agent.py | ~42 |

### test_tools.py → unit/

| 原 class | 目标文件 | 行数 |
|----------|----------|------|
| TestToolRegistry | unit/test_tools_registry.py | ~63 |
| TestToolBase | unit/test_tools_registry.py | ~32 |
| TestToolUseContext | unit/test_tools_context.py | ~22 |
| TestToolCallResult | unit/test_tools_context.py | ~23 |
| TestWorkspaceBoundary | unit/test_tools_workspace.py | ~107 |
| TestFileReadTool | unit/test_tools_read.py | ~259 |
| TestSafeDecodeBytes | unit/test_tools_read.py | ~10 |
| TestGlobTool | unit/test_tools_glob.py | ~121 |
| TestFileEditTool | unit/test_tools_edit.py | ~189 |
| TestFileWriteTool | unit/test_tools_write.py | ~135 |
| TestBashTool | unit/test_tools_bash.py | ~86 |
| TestGrepTool | unit/test_tools_grep.py | ~183 |
| TestToolClassAttributes | unit/test_tools_attributes.py | ~41 |

---

## 3. Marker 登记

在 `pyproject.toml` 的 `[tool.pytest.ini_options]` 下新增 `markers` 列表：

```toml
markers = [
    "model: tests for AgentConfig / ClaudeModel / ModelProvider / ModelId",
    "message: tests for AgentMessage discriminated union and serialization",
    "agent: tests for Agent class lifecycle (run/stream/permission)",
    "read: FileReadTool tests (validate_input / call / dedup / encoding)",
    "write: FileWriteTool tests",
    "edit: FileEditTool tests (replace / delete / permission / stale detection)",
    "glob: GlobTool tests",
    "grep: GrepTool tests (ripgrep dependent, skipped if rg unavailable)",
    "bash: BashTool tests (echo / failure / timeout / abort / exit code)",
    "workspace: workspace boundary security tests",
    "registry: tool registry tests",
    "attributes: tool class attribute tests (is_read_only / is_destructive)",
    "slow: tests taking more than 1s (skip with -m 'not slow')",
    "integration: mock model integration tests (no real API key needed)",
    "e2e: end-to-end tests requiring real API keys (vcrpy)",
]
```

---

## 4. Marker 分配给现有类

| 测试类 | Marker | 说明 |
|--------|--------|------|
| TestClaudeModel | `@pytest.mark.model` | |
| TestAgentConfig | `@pytest.mark.model` | |
| TestAgentResult | `@pytest.mark.message` | |
| TestAgentMessage | `@pytest.mark.message` | |
| TestAgent | `@pytest.mark.agent` | |
| TestToolRegistry | `@pytest.mark.registry` | |
| TestToolBase | `@pytest.mark.registry` | |
| TestToolUseContext | `@pytest.mark.registry` | |
| TestToolCallResult | `@pytest.mark.registry` | |
| TestWorkspaceBoundary | `@pytest.mark.workspace` | |
| TestFileReadTool | `@pytest.mark.read` | |
| TestSafeDecodeBytes | `@pytest.mark.read` | |
| TestGlobTool | `@pytest.mark.glob` | |
| TestFileEditTool | `@pytest.mark.edit` | |
| TestFileWriteTool | `@pytest.mark.write` | |
| TestBashTool | `@pytest.mark.bash` | also `@pytest.mark.slow` (含 timeout 测试) |
| TestGrepTool | `@pytest.mark.grep` | |
| TestToolClassAttributes | `@pytest.mark.attributes` | |

---

## 5. stub 文件策略

新建文件用 `pytest.skip` 占位，避免 pytest 对空文件报 warning：

```python
import pytest

pytest.skip("not yet implemented", allow_module_level=True)
```

---

## 6. 执行顺序

1. 创建目录结构：`tests/unit/`, `tests/integration/`, `tests/e2e/`, `tests/e2e/cassettes/`
2. 创建空 `__init__.py` 文件
3. 拆分 `test_agent.py` → 3 个 unit 文件，添加 marker 装饰器
4. 拆分 `test_tools.py` → 10 个 unit 文件，添加 marker 装饰器
5. 删除旧 `test_agent.py` 和 `test_tools.py`
6. 创建 `tests/unit/conftest.py`（初始空或导入共享 fixtures）
7. 在 `pyproject.toml` 注册 markers
8. 创建 integration/ 和 e2e/ 的 stub 文件
9. 运行全量测试确认无回归
10. 运行 `pytest -m model` / `-m read` / `-m "not slow"` 等组合验证 marker 生效

---

## 7. 验证方法

```bash
# 全量测试
pytest tests/unit/ -v

# 按 marker 筛选
pytest -m model -v           # 仅模型测试
pytest -m read -v             # 仅文件读取
pytest -m "not slow" -v       # 排除慢测试
pytest -m "model or message"  # 模型+消息

# 按文件筛选
pytest tests/unit/test_tools_bash.py -v

# 验证旧文件已删除
ls tests/test_agent.py tests/test_tools.py  # 应报文件不存在
```
