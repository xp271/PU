# agent-core

纯 Python 的 Agent 核心库——`Agent(config).run(prompt)` 即可调用，不绑定任何 UI/CLI。

支持多 LLM Provider（Anthropic / OpenAI / DeepSeek）、内置文件与 Shell 工具、权限引擎、上下文自动压缩，以及 MCP 协议集成。

> 状态：Phase 1/2 已实现（工具框架、多 Provider LLM 层、Agent 主循环、工具管线 + 权限、自动压缩、MCP）。Phase 3 子 Agent 处于「定义」阶段（`AgentDefinition` 已落地，执行引擎 Item 8 开发中）。详见 `agent-core-todolist-python.md`。

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

推荐通过 uv 在项目 `.venv` 中启动，确保依赖（如 `ddgs`、`html2text`）齐全：

```bash
uv run agent-core                # 交互式 REPL（推荐）
echo "prompt" | uv run agent-core   # 管道模式（一次性）
uv run python -m agent_core      # 模块启动
```

> **注意命名**：运行用连字符 `agent-core`（`[project.scripts]` 入口名）；`python -m` 后跟的是包名 `agent_core`（下划线）。`uv run agent_core` 会报 `program not found`。
>
> **不要用 `.venv` 之外的解释器跑**（如全局 conda / 系统 Python）。即便包做了 editable 安装，那些环境也缺 `ddgs` 等依赖，WebSearch 会报 “ddgs 库未安装”。`uv sync` 只同步 `.venv`，修不了外部解释器——直接 `uv run` 最稳妥。

配置查找顺序（先找到的生效）：`$AGENT_CORE_CONFIG` → `./agent-core.config.json` → `~/.agent-core/config.json`。

## 测试

```bash
uv run pytest          # 仅收集 tests/（见 pyproject.toml 的 testpaths）
```

## 许可证

详见 LICENSE。
