# SWE-Bench Pro 集成计划

## 1. 背景分析

### 1.1 SWE-Bench Pro 是什么

SWE-Bench Pro 是 Scale AI 发布的 LLM/AI Agent 评测基准，用于评估 AI 在**长周期软件工程任务**上的能力。给定一个代码仓库和 issue 描述，模型需要生成修复问题的 patch。它是原始 SWE-Bench 的升级版，任务更难、更长。

- 仓库地址: `https://github.com/scaleapi/SWE-bench_Pro-os`
- 数据集: HuggingFace `ScaleAI/SWE-bench_Pro`
- Docker 镜像: Docker Hub `jefzda/sweap-images`
- License: MIT

### 1.2 SWE-Bench Pro 的工作流程

```
┌─────────────┐    ┌──────────────┐    ┌──────────────┐    ┌─────────────┐
│ 加载数据集   │ -> │ Agent 修补   │ -> │ 收集 patches │ -> │ 评测 patches │
│ (HuggingFace)│    │ (Docker 内)  │    │ (JSON)       │    │ (Docker 内)  │
└─────────────┘    └──────────────┘    └──────────────┘    └─────────────┘
```

**步骤 1 — 加载数据集:**
```python
from datasets import load_dataset
swebench = load_dataset('ScaleAI/SWE-bench_Pro', split='test')
```
每个实例包含: `instance_id`, `repo`, `problem_statement`, `base_commit`, `dockerhub_tag` 等字段。

**步骤 2 — Agent 生成 patches:**
在 Docker 容器内（或本地 checkout），给 Agent 提供 `problem_statement`，Agent 通过编辑文件来修复问题。最终需要从修改中提取 unified diff。

**步骤 3 — 收集 patches:**
```bash
python helper_code/gather_patches.py --directory <pred_dir> --prefix <model_name> --output patches.json
```
输出 JSON 格式:
```json
[
  {"instance_id": "repo-123", "patch": "diff --git ...", "prefix": "my-model"}
]
```

**步骤 4 — 评测:**
```bash
python swe_bench_pro_eval.py --csv_data <dataset.csv> --patch_file patches.json \
  --output_dir results/ --run_scripts_dir run_scripts/ --workers 4 --dockerhub_username jefzda
```

### 1.3 当前项目能力清单

| 能力 | 当前状态 | 说明 |
|------|---------|------|
| 多 LLM Provider | 已完成 | Anthropic / OpenAI / DeepSeek / llamacpp |
| File 工具 (Read/Write/Edit) | 已完成 | Edit 工具已生成 unified diff |
| Bash 工具 | 已完成 | Shell 命令执行 |
| Glob/Grep 工具 | 已完成 | 代码搜索 |
| workspace_root 安全边界 | 已完成 | 限制工具操作范围 |
| Permission 系统 | 已完成 | 支持 bypassPermissions 模式 |
| SDK 编程接口 | 已完成 | `Agent(config).run(prompt)` |
| 流式消息 | 已完成 | `Agent.stream(prompt)` |
| SWE-Bench 评测能力 | **缺失** | 需要从零构建 |

---

## 2. 差距分析

### 2.1 需要新增的能力

| # | 能力 | 优先级 | 说明 |
|---|------|--------|------|
| 1 | **SWE-Bench Runner** | P0 | 核心 runner，负责遍历数据集、启动 Agent、收集 patch |
| 2 | **Patch 收集与导出** | P0 | 在 Agent 执行完成后，从工作区提取完整 diff |
| 3 | **Prompt 模板** | P0 | 将 SWE-Bench 的 problem_statement 格式化为 Agent prompt |
| 4 | **Docker 环境管理** | P1 | 对每个 task 实例创建/使用 Docker 容器作为工作环境 |
| 5 | **评测集成** | P1 | 调用 swe_bench_pro_eval.py 进行评测 |
| 6 | **结果报告** | P2 | 生成评测报告、统计信息 |

### 2.2 当前架构的优势

1. **Edit 工具已输出 unified diff** — `file_edit.py:157-163` 使用 `difflib.unified_diff()` 生成标准 diff，无需额外开发
2. **SDK 接口完善** — `Agent(config).run(prompt)` 可直接在 runner 中调用
3. **workspace_root 机制** — 天然适合限制在每个 task 的代码仓库内运行
4. **permission_mode="bypassPermissions"** — 评测时可以跳过交互式权限确认
5. **多 Provider 支持** — 可以测试不同模型的 SWE-Bench 表现

### 2.3 当前架构的不足

1. **无批量运行能力** — Agent 设计为单次交互，缺少批量实例遍历
2. **无 patch 聚合机制** — 当前 Edit 工具只输出单个编辑的 diff，需要通过 `git diff` 汇总整个修复
3. **无 Docker 集成** — 需要在 Docker 容器内启动 Agent，或让 Agent 在宿主机上操作容器内的代码
4. **无 SWE-Bench 数据集加载** — 需要添加 `datasets` 依赖
5. **agent 没有 "stop and produce final patch" 的机制** — Agent 循环没有明确的"我修好了，这是 patch"的终止信号；Agent 会在不再调用工具时自然终止（`stop_reason="end_turn"`），但需要 prompt 引导其输出修复摘要
6. **System prompt 注入路径缺失** — 底层 `query()` (`loop.py:66`) 已支持 `system: str | None = None` 参数，但 `Agent.stream()` (`agent.py:141`) 和 `Agent.run()` (`agent.py:109`) 均未暴露此参数也未传递给 `query()`，runner 无法通过公 API 注入 SWE-Bench 专用 system prompt。需同步修改 `Agent.run()` 和 `Agent.stream()` 两个方法增加 `system` 参数并透传
7. **Edit 工具强制要求先 Read** — `file_edit.py:104-111` 要求文件在读过后才能编辑，SWE-Bench 场景下 Agent 需要额外消耗 turn 来 Read 关键文件，`max_turns` 需适度提高
8. **CLI 无子命令体系** — 当前 `cli.py` 是简单的 REPL（`--resume`/`--continue`），不支持 `agent-core swebench run` 这类子命令，需要选择 CLI 架构方案
9. **Agent 无内置超时机制** — `Agent.run()` 是无限运行的异步方法，runner 需要在外层用 `asyncio.wait_for()` 实现超时控制

---

## 3. 实施方案

### 3.1 推荐架构：宿主机 + Docker 容器模式

```
┌────────────────────────────────────────────────────────┐
│                      宿主机 (PU)                        │
│  ┌──────────────────────────────────────────────────┐  │
│  │              swe_bench_runner.py                  │  │
│  │  - 加载数据集                                      │  │
│  │  - 遍历 instance                                  │  │
│  │  - 管理 Docker 容器生命周期                        │  │
│  │  - 调用 Agent SDK                                 │  │
│  │  - 收集 & 导出 patch                              │  │
│  └────────────┬─────────────────────────────────────┘  │
│               │  Agent(config).run(problem_statement)   │
│               │  workspace_root = /workspace/<repo>     │
│               ▼                                         │
│  ┌──────────────────────────────────────────────────┐  │
│  │              Agent (agent_core)                   │  │
│  │  - Read / Edit / Write / Bash / Glob / Grep      │  │
│  │  - 操作宿主机上的 repo checkout                    │  │
│  └──────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────┘
```

**选择宿主机模式而非 Docker 内执行的原因:**
1. 当前 Agent 通过 `workspace_root` 已经可以安全限制文件操作范围
2. 无需重构 Agent 核心来支持 Docker 内执行
3. 可以通过 `git clone` + `git checkout` 在本地准备每个 instance 的代码
4. 评测阶段仍然使用 SWE-Bench Pro 的 Docker 评测（`swe_bench_pro_eval.py`）

**需要修改的现有代码:**
- `Agent.stream()` 签名需增加 `system: str | None = None` 参数，透传给 `query()`，以便 runner 注入 SWE-Bench 专用 system prompt
- `Agent.run()` 同上，增加 `system` 参数并透传给 `self.stream(prompt, system=system)`，确保 runner 无论用 `run()` 还是 `stream()` 都能注入 system prompt

### 3.2 实现步骤

#### Phase 1: 最小可行方案 (MVP) — 本地模式

**目标:** 在本地代码仓库 checkout 上运行单个 SWE-Bench 任务，验证 Agent 能否生成有效 patch。

**新增文件:**

| 文件 | 作用 |
|------|------|
| `src/agent_core/benchmarks/__init__.py` | benchmarks 模块入口 |
| `src/agent_core/benchmarks/swebench/__init__.py` | swebench 子模块入口 |
| `src/agent_core/benchmarks/swebench/dataset.py` | 数据集加载与实例管理 |
| `src/agent_core/benchmarks/swebench/runner.py` | 核心 runner |
| `src/agent_core/benchmarks/swebench/prompt.py` | SWE-Bench 专用 prompt 模板 |
| `src/agent_core/benchmarks/swebench/patch.py` | Patch 提取、收集、导出 |
| `src/agent_core/benchmarks/swebench/config.py` | SWE-Bench 相关配置 |
| `src/agent_core/benchmarks/swebench/cli.py` | 独立 CLI（通过 `python -m` 运行，不与主 CLI 耦合） |
| `scripts/swe_bench/run.sh` | 一键运行脚本 |
| `scripts/swe_bench/eval.sh` | 评测脚本 |

**修改文件:**

| 文件 | 改动 |
|------|------|
| `src/agent_core/agent.py` | `Agent.run()` 和 `Agent.stream()` 增加 `system` 参数，透传给 `query()`（底层 `query()` 已在 loop.py:66 支持此参数，无需改动） |
| `pyproject.toml` | 添加 `datasets` 可选依赖 |

#### Phase 2: Docker 集成

**目标:** 使用 SWE-Bench Pro 官方 Docker 镜像作为执行环境。

**新增文件:**

| 文件 | 作用 |
|------|------|
| `src/agent_core/benchmarks/swebench/docker_env.py` | Docker 容器生命周期管理 |
| `src/agent_core/benchmarks/swebench/eval.py` | 调用 swe_bench_pro_eval.py 评测 |

#### Phase 3: 完整评测 pipeline

**目标:** 端到端自动化：加载数据集 → 批量运行 → 收集 patches → 评测 → 报告。

**新增文件:**

| 文件 | 作用 |
|------|------|
| `src/agent_core/benchmarks/swebench/report.py` | 评测结果分析与报告 |
| `CI workflow` (可选) | GitHub Actions 自动化评测 |

---

## 4. 详细设计

### 4.1 数据集模块 (`dataset.py`)

```python
"""SWE-Bench Pro 数据集加载。

Usage:
    from agent_core.benchmarks.swebench.dataset import load_swebench_dataset

    instances = load_swebench_dataset()
    # 或从本地 CSV 加载:
    instances = load_swebench_dataset(local_csv="swe_bench_pro.csv")
"""
```

**核心数据结构:**

```python
class SweBenchInstance(BaseModel):
    """单条 SWE-Bench 评测实例（使用 Pydantic，与项目模型风格一致）。"""
    instance_id: str          # e.g. "django__django-12345"
    repo: str                 # e.g. "django/django"
    problem_statement: str    # Issue 描述
    base_commit: str          # 起始 commit SHA
    hint_text: str | None = None  # 可选提示
    dockerhub_tag: str        # Docker 镜像 tag
    version: str              # 版本号
    # ... 其他字段

    def to_prompt(self) -> str:
        """将 instance 转为 Agent 可以理解的 prompt。"""
```

**关键实现:**
- 优先从 HuggingFace datasets 库加载 (`datasets.load_dataset`)
- 降级方案: 从本地 CSV/JSONL 文件加载
- 支持过滤（按 repo、按 instance_id、按 token 数）

### 4.2 Prompt 模板 (`prompt.py`)

```python
SWE_BENCH_PROMPT_TEMPLATE = """You are an expert software engineer tasked with fixing an issue in a codebase.

## Repository
{repo} at commit {base_commit}

## Issue
{problem_statement}

## Instructions
1. Understand the issue by reading relevant files with Read, Grep, and Glob
2. Identify the root cause
3. Make necessary edits to fix the issue using the Edit tool
4. Use Bash to run tests and verify your changes
5. When you are done, summarize what you changed and stop calling tools

{hint_section}

Important: Only make changes directly related to fixing the issue. Do not refactor unrelated code.
"""
```

**与 `enrich_context()` 的集成:**

当前 `query()` 会自动调用 `enrich_context()` (`context.py:22-54`) 生成 system prompt，包含日期、平台、git 状态等信息。SWE-Bench runner 需要通过 `Agent.run()` 或 `Agent.stream()` 的 `system` 参数注入上述模板。

**`system` 参数的实际行为是追加而非替换**（`loop.py:186-189`）：

```python
env_ctx = await enrich_context(workspace_root)
if system is not None:
    system = (system + "\n\n" + env_ctx) if env_ctx else system  # SWE-Bench 模板在前，env_ctx 追加在后
else:
    system = env_ctx
```

当 runner 提供 `system=SWE_BENCH_TEMPLATE` 时，最终 system prompt 为：
```
[SWE-Bench 专用模板]
\n\n
[enrich_context() 输出: 日期 / 平台 / workspace / git status]
```

这是合理的行为——环境上下文（日期、平台、workspace 路径）始终对 Agent 可见。`git status` 在 runner 从 `base_commit` 全新 checkout 后通常是干净的（只有 Agent 自身的修改），不会形成干扰。

**Edit 工具 Read-First 约束:**

`FileEditTool` (`file_edit.py:104-111`) 要求文件必须先被 Read 过才能 Edit。Prompt 中需要引导 Agent 先用 Glob/Grep 定位文件 → Read 关键文件 → 再 Edit。这会额外消耗 turn，建议 `max_turns` 设为 50（而非默认 20），给 Agent 足够的探勘空间。

### 4.3 Runner 核心 (`runner.py`)

```python
"""SWE-Bench Pro Runner — 核心执行引擎。

Usage:
    import asyncio
    from agent_core.benchmarks.swebench.runner import SweBenchRunner
    from agent_core.models import AgentConfig

    config = AgentConfig(
        model="anthropic/claude-sonnet-4-5",
        permission_mode="bypassPermissions",
        api_keys={"anthropic": "sk-ant-xxx"},
        max_turns=50,  # 建议 50，给 Agent 足够的探勘+修复轮次
    )
    runner = SweBenchRunner(config)
    results = asyncio.run(runner.run_all(instances))
"""

class SweBenchRunner:
    """遍历 SWE-Bench 实例，每个实例启动一个 Agent 尝试修复。"""

    def __init__(self, config: AgentConfig, *, work_dir: str | None = None):
        self.config = config
        self.work_dir = Path(work_dir or "./swe_bench_work")

    async def run_single(self, instance: SweBenchInstance) -> SweBenchResult:
        """对单个 instance 运行 Agent，返回 patch 和元数据。"""
        # 1. 准备代码仓库 (clone + checkout)
        # 2. 构建 prompt → 作为 system 参数注入
        # 3. 设置 workspace_root = repo checkout 路径
        #    （AgentConfig 是 Pydantic BaseModel，直接赋值即可：
        #     ``config.workspace_root = str(repo_path)``）
        # 4. 创建全新 Agent 实例（不传 session= 参数），确保各 instance 完全隔离
        # 5. asyncio.wait_for(agent.run(prompt, system=system_prompt), timeout) — 外层超时控制
        # 6. 从工作区提取 git diff <base_commit>
        # 7. 根据 stop_reason 分类处理结果
        # 8. 返回 SweBenchResult

    async def run_all(
        self, instances: list[SweBenchInstance],
        *, max_parallel: int = 1, skip_existing: bool = True,
    ) -> list[SweBenchResult]:
        """批量运行所有 instance，支持并行和断点续传。"""
```

**关于超时控制：**

`Agent.run()` 自身没有超时机制，runner 需要用 `asyncio.wait_for()` 在外层包裹。
runner 统一使用 `Agent.run()` 而非 `Agent.stream()`——runner 不需要细粒度流式控制，
`Agent.run()` 封装了流式消息的聚合逻辑（将 `AgentMessage` 流转为 `AgentResult`），
且透传 `system` 参数给 `stream()` / `query()`，runner 只需处理最终的 `AgentResult`：

```python
try:
    result = await asyncio.wait_for(
        agent.run(prompt, system=system_prompt),
        timeout=timeout_per_instance,
    )
except asyncio.TimeoutError:
    # 超时后仍尝试提取已有的工作区改动
    patch = extract_git_diff(repo_path, instance.base_commit)
    return SweBenchResult(
        instance_id=instance.instance_id,
        patch=patch,
        stop_reason="timeout",
        ...
    )
```

**关于 stop_reason 分类处理：**

当前 Agent 有 4 种 `stop_reason`（定义于 `models.py:17`），runner 应对每种情况区别处理：

| stop_reason | 含义 | Patch 处理 |
|---|---|---|
| `end_turn` | Agent 主动结束，认为修复完成 | 提取完整 patch，标记 success |
| `max_turns` | 超过 `max_turns` 轮次限制 | 仍提取 working tree diff 作为 partial patch，标记 incomplete |
| `error` | LLM 调用或其他运行时错误 | 尝试提取现有改动，标记 error |
| `permission_denied` | 权限检查拒绝 | 通常无有效改动，标记 failed |

**运行流程（单个 instance）:**

```
prepare_workspace(instance)
     │
     ├─ git clone <repo> <work_dir>/<instance_id>
     ├─ git checkout <base_commit>
     │  （Agent 直接在 base_commit 的 working tree 上编辑，不创建新分支）
     │
     ▼
build_prompt(instance) → system_prompt
     │
     ├─ 注入 repo 信息、problem_statement、hint
     │
     ▼
config.workspace_root = str(repo_path)   # AgentConfig 是 Pydantic BaseModel，直接赋值
     │
     ▼
Agent(config).run(prompt, system=system_prompt)  # 不传 session=，各 instance 完全隔离
     │
     ├─ 通过新增的 system 参数注入 SWE-Bench 专用 prompt
     ├─ Agent 通过 Read/Edit/Bash 修复代码
     ├─ 外层 asyncio.wait_for() 控制超时
     │
     ▼
handle_stop_reason(result.stop_reason)
     │
     ├─ end_turn → 提取完整 patch
     ├─ max_turns → 提取 partial patch，标记 incomplete
     ├─ error → 尝试提取现有改动，标记 error
     ├─ timeout → 提取现有改动，标记 timeout
     │
     ▼
extract_patch(repo_path, base_commit)
     │
     ├─ git diff <base_commit>  （比较 base_commit tree 与当前 working tree）
     ├─ 包括未暂存 + 已暂存的全部改动（Agent 不会执行 git commit）
     │
     ▼
SweBenchResult(instance_id, patch, stop_reason, tokens_used, tool_uses, ...)
```

### 4.4 Patch 提取 (`patch.py`)

```python
"""Patch 提取与导出。

Usage:
    from agent_core.benchmarks.swebench.patch import extract_git_diff, export_patches_json

    diff = extract_git_diff("/path/to/repo", base_commit="abc123")
    export_patches_json(results, "patches.json", prefix="my-model")
"""

def extract_git_diff(repo_path: str, base_commit: str) -> str:
    """从 repo 提取 unified diff。
    
    使用 ``git diff <base_commit>`` 比较 base_commit 的 tree 与当前 working tree。
    注意：不能使用 ``git diff base_commit..HEAD``（只比较 commit），因为 Agent
    直接编辑文件而不执行 git commit，所有改动都在 working tree 中。
    
    对于 Agent 新建的文件（untracked），默认 ``git diff`` 不包含它们。
    在 SWE-Bench 场景中 Agent 极少创建全新文件（更常见的是编辑已有文件），
    因此首先尝试标准 diff；如果需要覆盖新文件，可补充：
    
        # 对新文件生成 /dev/null → file 的 diff
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, cwd=repo_path,
        ).stdout.strip().splitlines()
        for f in untracked:
            diff = subprocess.run(
                ["git", "diff", "--no-index", "/dev/null", f],
                capture_output=True, text=True, cwd=repo_path,
            ).stdout
            full_diff += diff
    """
    result = subprocess.run(
        ["git", "diff", base_commit],
        capture_output=True, text=True, cwd=repo_path,
    )
    return result.stdout

def export_patches_json(
    results: list[SweBenchResult], output_path: str, prefix: str
) -> None:
    """将结果导出为 SWE-Bench Pro 评测脚本期望的 JSON 格式。"""
```

### 4.5 配置 (`config.py`)

```python
class SweBenchConfig(BaseModel):
    """SWE-Bench 运行配置（与 AgentConfig 分离，关注 benchmark 层面）。"""

    # 工作目录
    work_dir: str = "./swe_bench_work"

    # 数据集配置
    dataset_name: str = "ScaleAI/SWE-bench_Pro"
    dataset_split: str = "test"
    local_csv: str | None = None  # 使用本地 CSV（离线模式）

    # 执行配置
    max_turns: int = 50            # 建议 50，给 Agent 探勘+编辑空间
    permission_mode: str = "bypassPermissions"
    max_parallel: int = 1
    timeout_per_instance: int = 3600  # 秒，通过 asyncio.wait_for() 在外层实现

    # 环境配置
    use_docker: bool = False
    docker_keep_containers: bool = False

    # 输出配置
    output_dir: str = "./swe_bench_results"
    model_prefix: str = "agent-core"

    # 断点续传
    resume: bool = True

    # 清理策略
    cleanup_work_dirs: bool = False   # 运行后是否清理 repo clone
```

### 4.6 Docker 环境管理 (`docker_env.py`) — Phase 2

```python
"""Docker 环境管理。

当 use_docker=True 时，有以下几种方案：

方案 A（推荐）— Volume Mount 模式:
  1. docker run -d --name swe_<instance_id> \
       -v <host_work_dir>:/workspace \
       jefzda/sweap-images:<dockerhub_tag>
  2. Agent 在宿主机运行，workspace_root 指向 host_work_dir
  3. 容器提供 build/test 环境，代码通过 volume mount 共享
  优点: Agent 的文件工具 (Read/Edit/Write/Glob) 正常工作
  缺点: 宿主机需要有 repo 依赖的编译工具

方案 B — Agent-in-Docker 模式:
  1. docker run 容器，在容器内启动 Agent 进程
  2. Agent 全部在 Docker 内运行
  优点: 环境完全隔离，与 SWE-Bench 官方评测一致
  缺点: 需要在容器内安装 agent-core 包，架构变动大

方案 C（不推荐）— docker exec 模式:
  Agent 通过 Bash 工具执行 docker exec 在容器内操作。
  **问题:** Agent 的文件工具(Read/Edit/Write/Glob/Grep)操作宿主机文件系统，
  无法访问容器内文件。Agent 只能用 Bash + sed/awk 编辑代码，失去完整工具链。
  建议避免此方案。
"""
```

**Phase 1 MVP 不需要 Docker**，直接使用本地 `git clone` + `git checkout` 即可。Phase 2 再根据实际需要选择方案 A 或 B。

### 4.7 评测集成 (`eval.py`) — Phase 2

```python
"""调用 SWE-Bench Pro 官方评测脚本。

Usage:
    python -m agent_core.benchmarks.swebench eval \
        --csv_data swe_bench_pro.csv \
        --patch_file patches.json \
        --output_dir results/
"""

def run_evaluation(
    csv_data: str, patch_file: str, output_dir: str,
    *, workers: int = 4, use_local_docker: bool = True,
) -> dict[str, Any]:
    """调用 swe_bench_pro_eval.py 进行评测，返回结果摘要。"""
```

---

## 5. 依赖变更

### 5.1 pyproject.toml 新增依赖

```toml
[project]
dependencies = [
    # ... 现有依赖 ...
]

# 新增可选依赖组（与现有 dev/docs 平级）
[dependency-groups]
swebench = [
    "datasets>=2.14",       # HuggingFace datasets 库（加载 SWE-Bench 数据集）
    "docker>=7.0",          # Docker SDK for Python（Phase 2 起需要）
]
```

注意：Phase 1 不需要 `pandas`，数据集加载用 `datasets` 库从 HuggingFace 或本地 CSV/JSONL 读取。`docker` 包 Phase 2 才需要。

### 5.2 外部依赖

| 依赖 | 用途 | 是否必需 |
|------|------|---------|
| Git | 代码仓库 clone/checkout/diff | **必需** (已安装) |
| Docker | 评测环境和容器化执行 | Phase 2 起必需 |
| SWE-bench Pro 仓库 | 评测脚本 `swe_bench_pro_eval.py` | 评测阶段必需 |
| 模型 API Key | 调用 LLM | **必需** |

---

## 6. CLI 设计

**设计决策：** 当前 `cli.py` 是简单 REPL，没有 argparse 子命令体系。为避免干扰主 CLI 的稳定性，SWE-Bench CLI 使用 `python -m` 方式独立运行，不注册为 `agent-core swebench` 子命令。后续如需统一 CLI 体验，可在 Phase 3 考虑重构。

```bash
# 1. 下载数据集
python -m agent_core.benchmarks.swebench download --output swe_bench_data/

# 2. 运行 benchmark（单个 instance 测试）
python -m agent_core.benchmarks.swebench run --single "django__django-12345" \
    --model anthropic/claude-sonnet-4-5

# 3. 运行全部 instance
python -m agent_core.benchmarks.swebench run --all --max-parallel 2 --output results/

# 4. 运行指定范围
python -m agent_core.benchmarks.swebench run --ids ids.txt

# 5. 从上次中断处继续
python -m agent_core.benchmarks.swebench run --resume --output results/

# 6. 收集 patches
python -m agent_core.benchmarks.swebench gather --results results/ \
    --prefix my-model --output patches.json

# 7. 评测
python -m agent_core.benchmarks.swebench eval --csv swe_bench_pro.csv \
    --patches patches.json --output eval_results/
```

---

## 7. 文件结构总览

```
PU/
├── src/agent_core/
│   ├── agent.py                       # 修改: run() 和 stream() 增加 system 参数
│   ├── benchmarks/                    # 新增: benchmarks 模块
│   │   ├── __init__.py               # 模块入口
│   │   └── swebench/                 # SWE-Bench Pro 实现
│   │       ├── __init__.py
│   │       ├── config.py             # SweBenchConfig (Pydantic BaseModel)
│   │       ├── dataset.py            # 数据集加载
│   │       ├── prompt.py             # Prompt 模板
│   │       ├── runner.py             # 核心 runner
│   │       ├── patch.py              # Patch 提取与导出
│   │       ├── cli.py                # 独立 CLI（python -m 运行）
│   │       ├── docker_env.py         # Docker 环境管理 (Phase 2)
│   │       ├── eval.py               # 评测集成 (Phase 2)
│   │       └── report.py             # 结果报告 (Phase 3)
│   └── ...                           # 现有模块不变
├── scripts/
│   └── swe_bench/
│       ├── run.sh                    # 一键运行
│       ├── eval.sh                   # 评测
│       └── setup.sh                  # 环境准备
├── plan/
│   └── swe-bench-pro-integration-plan.md  # 本文档
└── pyproject.toml                    # 修改: 添加 swebench 依赖组
```

---

## 8. 风险与注意事项

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| 网络访问 GitHub/HuggingFace 受限 | 无法下载数据集和代码仓库 | 支持本地 CSV 和预下载的 repo mirror |
| 单个 instance 运行时间过长 | Agent 耗尽 max_turns | 设置合理的 max_turns（建议 50）和 `asyncio.wait_for()` 超时 |
| API 调用成本高 | 大规模评测费用高 | 先用少量 instance 验证，再扩展；支持本地模型 (llamacpp) |
| Docker 镜像大 | 磁盘空间不足 | 按需拉取，支持 --keep-images=false 清理 |
| Agent 生成无效 patch | 评测结果为 0 | 增加 prompt 工程、hint 注入、多轮重试 |
| 评测与生成环境不一致 | 评测失败 | Phase 2 统一使用 Docker 保证环境一致性 |
| **Agent 在 repo 内执行 git 命令破坏基准环境** | checkout 损坏，后续任务受影响 | 限制 Agent 不使用 `git reset --hard`/`git clean`；runner 始终从 base_commit 恢复 |
| **Clone 大 repo（django ~500MB）耗时长** | 单个 instance 准备数分钟 | 预置常用 repo mirror；支持 `--skip-clone` 复用已有 checkout |
| **Agent 趁机做无关重构** | patch 过大，评测无法通过 | Prompt 中强调 "only fix this issue, do not refactor" |
| **problem_statement 过长超出 context window** | Agent 无法读入完整 issue | 按 token 数预处理，过滤超大 instance |
| **work_dir 积累大量 repo clone** | 磁盘空间耗尽 | `SweBenchConfig.cleanup_work_dirs` 选项；运行后自动清理 |
| **FileEditTool 要求先 Read 文件** | 额外消耗探勘 turn | max_turns 设为 50；Prompt 引导 Agent 先 Glob/Grep 定位再 Read 关键文件 |
| **Bash 安全检查在 bypassPermissions 模式下仍生效** | Agent 无法执行 `rm -rf /` 等危险命令（包括合法的清理操作） | 这是安全特性，非缺陷。Agent 可正常使用 `rm` 删除工作区文件，极端危险命令会被 `analyze_bash_command()` (loop.py:736) 拦截 |
| **`enrich_context()` 的 git status 仍会出现在 system prompt 末尾** | 噪音信息可能占用 context window | 影响小：runner 从 base_commit 全新 checkout 后 git status 通常是干净的（只有 Agent 自身改动）；且 `system` 参数采用追加模式，SWE-Bench 模板在前，env_ctx 在后 |
| **Agent.run() 和 Agent.stream() 不暴露 system 参数** | Runner 无法注入 SWE-Bench prompt | 已修改 `Agent.run()` 和 `Agent.stream()` 签名，增加 `system: str \| None = None` 参数并透传给 `query()` |

---

## 9. 实施时间线

| Phase | 内容 | 预计工作量 | 产出 |
|-------|------|-----------|------|
| **Phase 1** | MVP 本地模式: 修改 Agent.run()/Agent.stream()(增加 system 参数) + dataset + runner + prompt + patch + CLI | ~4-6 天 | 可在本地 checkout 上运行单个/批量 SWE-Bench 任务 |
| **Phase 2** | Docker 集成 + 评测集成（重新设计 Docker 方案） | ~3-4 天 | 可跑完整的 benchmark 并得到评测分数 |
| **Phase 3** | 自动化 + CI + 报告 | ~1-2 天 | 一键运行 + 结果分析 + 可视化 |

---

## 10. 快速验证方案

完成 Phase 1 后，可通过以下步骤快速验证:

```bash
# 1. 安装依赖
pip install datasets

# 2. 准备一个测试用 SWE-Bench instance（从 HuggingFace 获取）
python -c "
from datasets import load_dataset
ds = load_dataset('ScaleAI/SWE-bench_Pro', split='test')
print(ds[0])
"

# 3. Clone 对应 repo 并 checkout 到 base_commit
git clone <repo_url> /tmp/swe_test
cd /tmp/swe_test && git checkout <base_commit>

# 4. 运行单个 instance
python -m agent_core.benchmarks.swebench run \
    --single <instance_id> \
    --repo /tmp/swe_test \
    --model anthropic/claude-sonnet-4-5

# 5. 检查 patch（git diff <base_commit>）
cat results/<instance_id>/patch.diff
```

注意：`Agent.run()` 和 `Agent.stream()` 均已增加 `system` 参数并透传给 `query()`，验证前确认相关修改已生效。

---

## 11. 与 SWE-Bench Pro 官方评测的对接点

最终评测使用 SWE-Bench Pro 官方脚本时，需要对接以下接口:

**输入（由我们的 runner 生成）:**
- `patches.json`: `[{instance_id, patch, prefix}]` 格式
- 每个 instance 的 Docker 镜像已准备就绪

**调用评测脚本:**
```bash
python swe_bench_pro_eval.py \
    --csv_data swe_bench_pro.csv \
    --patch_file patches.json \
    --output_dir results/ \
    --run_scripts_dir run_scripts/ \
    --workers 4 \
    --dockerhub_username jefzda
```

**输出（由评测脚本生成）:**
- 每个 instance 的评测日志
- 总体通过率 / 分数

我们的 runner 和评测脚本之间的关系是 **松耦合** 的 — runner 负责生成 patches.json，评测脚本独立运行。
