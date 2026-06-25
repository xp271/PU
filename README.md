# agent-core

纯 Python 的 Agent 核心库——`Agent(config).run(prompt)` 即可调用，不绑定任何 UI/CLI。

支持多 LLM Provider（Anthropic / OpenAI / DeepSeek）、内置文件与 Shell 工具、权限引擎、上下文自动压缩、MCP 协议集成、子 Agent（`Agent` 工具）、Hooks / Skills / Plugins 扩展系统，以及会话持久化与恢复。

> 状态：核心已全部落地（Phase 1–4）——工具框架、多 Provider LLM 层、Agent 主循环、工具管线 + 权限、自动压缩、MCP、子 Agent 执行引擎、Hooks/Skills/Plugins、会话持久化（JSONL 落盘 + `--resume`/`--continue`）。全套测试 ~957 passed。剩余收尾项：vcrpy 录制回放、MkDocs 文档、PyPI 发布。详见 `agent-core-todolist-python.md`。

## 安装

需要 Python >= 3.12。本项目使用 [uv](https://github.com/astral-sh/uv) 管理依赖：

```bash
uv sync
```

## 快速开始

```python
import asyncio
from agent_core import Agent, AgentConfig

async def main():
    config = AgentConfig(
        model="anthropic/claude-sonnet-4-5",
        max_turns=20,
        workspace_root=".",          # 文件/Shell 工具的安全边界（None 时不限制）
        permission_mode="ask",
    )
    agent = Agent(config)
    result = await agent.run("读取 README.md 并总结其内容")
    print(result.content)

asyncio.run(main())
```

### 流式输出

```python
async for message in agent.stream("帮我重构这个函数"):
    if message.type == "assistant_text":
        print(message.content, end="", flush=True)
    elif message.type == "tool_use":
        print(f"\n[调用工具] {message.tool_name}")
```

### 权限回调

`permission_mode="ask"` 时，破坏性工具会经过回调征求批准：

```python
async def approve(tool_name: str, tool_input: dict) -> bool:
    return tool_name != "Bash"      # 返回 True 允许 / False 拒绝

agent.on_permission_check(approve)
```

未注册回调时：只读工具放行，破坏性工具默认拒绝。

### 子 Agent / Skills / Hooks

- **子 Agent**：内置 `Agent` 工具让 LLM 通过 tool_use 派生子 Agent（`GeneralPurpose` / `Explore` / `Plan`），权限不可提权、递归深度有界（`MAX_AGENT_DEPTH`），支持同步与后台（`run_in_background`）两种模式。
- **Skills**：`<workspace>/.agent/skills/<name>/SKILL.md`（YAML frontmatter + 正文模板）定义可复用任务，经内置 `Skill` 工具以 inline / fork 方式执行。REPL 中 `/skills` 列出。
- **Hooks / Plugins**：`PreToolUse` / `PostToolUse` / `Stop` / `SubagentStart` 四类 Hook 可拦截/改写/终止；Plugin 经 entry points 热插拔贡献工具、Hook、Skill、Agent 定义（`AgentConfig.auto_enable_plugins`）。

## 会话持久化与恢复

会话以 append-only JSONL 落盘到 `~/.agent/projects/{project_hash}/{session_id}.jsonl`（崩溃时最多丢失最后一行）；压缩采用 checkpoint 分段链，**保留压缩前完整 transcript** 供审计。

```bash
agent-core --continue            # 恢复本项目最近一次会话
agent-core --resume <session_id> # 按 id 恢复指定会话
```

REPL 内：`/sessions` 列出已保存会话，`/clear` 清空当前历史。

> v1 仅恢复对话历史；会话级权限记忆（"始终允许"）不落盘，恢复后需重新授权。

## 配置要点

`AgentConfig` 常用字段：

| 字段 | 默认 | 说明 |
|------|------|------|
| `model` | `anthropic/claude-sonnet-4-5` | `provider/model-name`，省略 provider 默认 anthropic |
| `max_turns` | `20` | 最大 turn 迭代次数 |
| `permission_mode` | `"ask"` | `bypassPermissions` / `acceptEdits` / `ask` |
| `tools` | `["*"]` | 可用工具白名单 |
| `workspace_root` | `None` | 文件/Shell 工具的安全边界；`None` 时不做越界校验 |
| `mcp_servers` | `{}` | MCP 服务端配置 |
| `api_keys` | `{}` | Provider → API Key，缺省回退到环境变量 |

API Key 缺省时从环境变量读取：`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `DEEPSEEK_API_KEY`。

## CLI（交互式测试入口）

```bash
agent-core                       # 交互式 REPL
agent-core --continue            # 恢复最近会话
agent-core --resume <id>         # 恢复指定会话
echo "prompt" | agent-core       # 管道模式（一次性）
python -m agent_core             # 模块启动（无需安装）
```

REPL 内置命令：`/sessions`（会话列表）、`/skills`（可用 skill）、`/clear`（清空历史）。

配置查找顺序（先找到的生效）：`$AGENT_CORE_CONFIG` → `./agent-core.config.json` → `~/.agent-core/config.json`。

## 测试

```bash
uv run pytest          # 仅收集 tests/（见 pyproject.toml 的 testpaths）
```

当前 ~957 passed / 10 skipped，覆盖单元 + mock 集成，含各层 fallback / 边界（Hook 异常、Plugin 隔离、Provider 重试、工具异常、持久化分段链与恢复等）。

## 许可证

详见 LICENSE。
