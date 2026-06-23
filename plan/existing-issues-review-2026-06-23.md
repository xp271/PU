# agent-core 现存问题盘点

> 审阅日期：2026-06-23 ｜ 审阅分支：`feat/mcp-integration`
> 范围：`src/agent_core/`、`tests/`、仓库根布局、`agent-core-todolist-python.md` 与代码的一致性
> 方法：通读核心源码 + 全量运行测试（`tests/` 641 passed / 11 skipped；根目录 `pytest` 另有 11 failed，见 §3.1）

---

## 概览

代码主体（Phase 1/2：Tool 框架、多 Provider LLM 层、query 主循环、工具管线、权限引擎、压缩、MCP）实现完整且 `tests/` 全绿。

**关于当前阶段定位**：现处于 Item 7「子 Agent **定义**」阶段——`AgentDefinition` / registry / 内置 Agent 是「能力模板」数据结构，Item 8 的执行引擎（`run_agent` / `AgentTool`）**本就尚未实现，属预期进度，不是缺陷**。因此「内置 Agent 还没有消费路径」「子 Agent 测试是跳过桩」都是这一阶段的正常状态（详见 §4，仅作进度记录，不计入问题）。

真正需要处理的问题集中在两类：

1. **设计契约与实际调用路径脱节** —— 文档宣称的安全/扩展能力在公开 API 上未真正生效（§1、§2.3）。
2. **本阶段交付物的状态对齐** —— Item 7 代码已落地，但文档状态、分支归属没跟上（§2）。

按严重程度排序如下。

---

## 1. 严重问题

### 1.1 workspace 安全边界在公开 API 上形同虚设

- `tools/_io.py:102` 的 `check_workspace()` 在 `context_workspace_root is None` 时**直接返回**（no-op）。
- `query()`（`query/loop.py:61`）的 `workspace_root` 参数默认 `None`。
- `Agent.stream()`（`agent.py:56`）调用 `query()` 时**根本没有传 `workspace_root`**，并且 `AgentConfig`（`models.py:236`）里**没有 `workspace_root` 字段**。

后果：任何通过公开 `Agent(config).run()` / CLI 使用本库的调用方，`workspace_root` 恒为 `None`，于是所有文件类工具（Read/Edit/Write/Glob/Bash 的 `working_dir`）的 workspace 越界校验**完全不执行**。而 `agent-core-todolist-python.md` 第 266 行明确写着「所有文件/路径类工具在操作前必须通过 `check_workspace()` 校验……否则返回 `PermissionError`」——文档承诺的安全边界在真实路径上不存在。

建议：在 `AgentConfig` 增加 `workspace_root` 字段，并在 `Agent.stream()` 透传给 `query()`；或在文档中明确「workspace 边界仅在显式传入 `workspace_root` 时生效」，避免误导。

> 注：本节是本审阅中**唯一与子 Agent 无关的真正缺陷**，与当前 Item 7 阶段无关，独立存在于 Phase 1/2 的文件工具链上。

---

## 2. 中等问题（一致性 / 可维护性）

### 2.1 TodoList 文档状态与代码严重不同步

`agent-core-todolist-python.md`：
- 第 2197 行统计表：`Phase 3: 子Agent & 扩展 … ⬜ 待实现`；
- 第 2046–2071 行 Item 7 的复选框**全部是 `[ ]`（未完成）**。

但 Item 7 的代码（`agents/` 全模块 + 4 个测试文件）实际已经写完。文档低估了真实进度，"已实现 ~55%" 的结论也随之失真。建议在合并后同步勾选 Item 7 并更新统计表，明确区分「Item 7 已实现 / Item 8 未实现」。

### 2.2 工作处于未提交状态，且与分支主题不符

`git status` 显示当前分支 `feat/mcp-integration`（最近提交是 MCP 集成）上**混入了 agent-definition 的工作且未提交**：

```
 M src/agent_core/__init__.py          # 已改，未提交
?? src/agent_core/agents/              # 整个新模块，未跟踪
?? tests/unit/test_agent_definition.py
?? tests/unit/test_agent_loader.py
?? tests/unit/test_agent_registry.py
?? tests/unit/test_builtin_agents.py
?? plan/item-7-agent-definition-plan.md
```

问题：
- MCP 功能分支里夹带了 Item 7 的实现，分支语义混乱，不利于 review 和回滚。
- 这批改动尚未纳入版本控制，存在丢失风险。

建议：将 Item 7 的改动迁出到独立分支（如 `feat/agent-definition`）并提交。

### 2.3 配置面宣传了"无声失效"的能力

`AgentConfig` 暴露了两个目前**没有任何消费者**的字段：

- `hooks: list[Any]`（`models.py:258`）—— `query/loop.py:584` 的 PreToolUse 步骤只是注释占位，Hooks 系统（Item 9）未实现。
- `skills_dir: str | None`（`models.py:254`）—— 没有任何 skill 加载器读取它。

用户填了这两个字段不会报错也不会有任何效果（silent no-op）。在 Item 9 落地前，建议要么在字段 `description` 中标注「尚未生效（Item 9）」，要么在 `Agent.__init__` 里对非空值发出一次 warning，避免使用者误以为已接线。

---

## 3. 轻微问题（仓库 / 代码卫生）

### 3.1 参考项目 `cc-haha-main` 污染根目录测试

仓库根提交了一整个 TS 参考项目 `cc-haha-main/`，其中 `cc-haha-main/cc-haha-main/runtime/test_helpers.py` 在**从仓库根运行 `pytest`** 时被收集，产生 **11 个失败**（`UnicodeDecodeError: 'gbk' codec...` —— Windows 默认 GBK 编码读取含非 GBK 字节的文件）。这些失败与 `agent_core` 无关，但会让 CI / 本地全量跑测试时误报红。

建议：在 `pyproject.toml` 的 `[tool.pytest.ini_options]` 中设 `testpaths = ["tests"]`（或 `norecursedirs`/`--ignore`），将参考项目排除出测试收集。同时考虑该参考项目（及 `page-excel-2.png` 等无关二进制）是否应留在主仓库。

### 3.2 未使用的 import

- `agents/loader.py:13` `from typing import Any` —— 模块内未使用 `Any`。
- `agents/definition.py:12` `from typing import Any, Literal` —— `Literal` 未使用（`Any` 有用）。

### 3.3 README 为空

`README.md` 为 0 字节，但 `pyproject.toml` 将其声明为 `readme`。打包/发布（Item 11）前需补内容，否则 PyPI 页面将空白。

### 3.4 测试遗留告警

`tests/unit/test_context.py::test_run_git_status_missing_binary` 触发 `RuntimeWarning: coroutine 'Process.communicate' was never awaited`——mock 替身没有正确 await，属测试写法问题，应修正 mock 以消除噪音。

### 3.5 已知功能占位（设计内 TODO，仅作记录）

- `FileReadTool` 对 PDF/图片仅返回占位信息（todolist 第 327 行：待接入 pymupdf/Pillow）。
- `mcp/_transport.py` 的 WebSocket transport 未实现（`connect` 抛 `NotImplementedError`）。

---

## 4. 当前阶段的预期状态（非问题，仅作进度记录）

以下都是 Item 7「子 Agent 定义」阶段的**正常中间态**，会随 Item 8 执行引擎一并完成，**不计入缺陷**：

- **内置 Agent 暂无消费路径**：`agents/`（definition / registry / loader + GeneralPurpose / Explore / Plan）已实现并在 import 时注册，但 `run_agent()` / `AgentTool` 尚未实现，故 `AgentDefinition` 目前是「已定义、未被执行引擎读取」的能力模板数据。这正是「先定义、后执行」的预期顺序。
- **权限继承函数暂无调用者**：`permissions/inheritance.py` 的三个纯函数已实现且有单测，等待 Item 8 接入。
- **子 Agent 测试是跳过桩**：`tests/integration/test_subagent.py`、`test_fork.py` 为 `pytest.skip("not yet implemented")`，是为 Item 8 预留的占位，届时回填。

唯一建议：在 todolist / 代码注释里把这些标注为「已定义，执行引擎见 Item 8」，避免被误读为「子 Agent 功能已可用」。

---

## 建议优先级

| 优先级 | 问题 | 动作 |
|--------|------|------|
| P0 | §1.1 workspace 边界失效 | 给 `AgentConfig` 加 `workspace_root` 并在 `Agent.stream()` 透传，或更正文档承诺（与子 Agent 无关的独立缺陷） |
| P0 | §2.2 改动未提交、分支错位 | 迁出独立分支并提交，避免丢失 |
| P1 | §2.1 文档状态不同步 | 更新 todolist Item 7 状态与统计表 |
| P1 | §3.1 参考项目污染测试 | `pyproject.toml` 设 `testpaths = ["tests"]` |
| P2 | §2.3 hooks/skills_dir 无声失效 | 字段说明标注或加 warning |
| P2 | §3.2 / §3.3 / §3.4 代码与仓库卫生 | 清理 import、补 README、修 mock 告警 |
| —  | §4 子 Agent 定义/执行 | 本阶段预期状态，随 Item 8 推进 |
