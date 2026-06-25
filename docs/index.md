# agent-core

`Agent(config).run(prompt)` — a pure Python library for building LLM-powered
agents. No CLI, no UI frameworks. Just import and run.

## Status

~957 tests passed · 3 LLM providers (Anthropic / OpenAI / DeepSeek) ·
MCP protocol · Sub-agents · Hooks · Skills · Session persistence

## Installation

```bash
uv add agent-core
# or
pip install agent-core
```

Requires Python ≥ 3.12.

## Core capabilities

| Capability | Description |
|-----------|-------------|
| **Multi-provider LLM** | Anthropic, OpenAI, DeepSeek — one API, switch with a string |
| **Streaming** | `async for msg in agent.stream(prompt):` yields typed messages |
| **Tool pipeline** | 6 built-in tools (Read/Edit/Write/Glob/Grep/Bash) with concurrent execution |
| **MCP integration** | Connect any MCP-compatible server — tools auto-register |
| **Sub-agents** | Spawn child agents (Explore, Plan, GeneralPurpose) via tool calls |
| **Hooks & Skills** | Intercept tool execution, define reusable prompt templates |

## Quick example

```python
import asyncio
from agent_core import Agent, AgentConfig

async def main():
    config = AgentConfig(model="anthropic/claude-sonnet-4-5")
    agent = Agent(config)
    result = await agent.run("What is 2 + 2?")
    print(result.content)

asyncio.run(main())
```

Next: [Quickstart](quickstart.md) · [Agent API](api/agent.md)
