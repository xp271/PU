# Quickstart

Get an agent running in under 5 minutes.

## 1. Set your API key

```bash
export ANTHROPIC_API_KEY="sk-ant-xxx"
# or
export DEEPSEEK_API_KEY="sk-xxx"
export OPENAI_API_KEY="sk-xxx"
```

## 2. Run your first prompt

```python
import asyncio
from agent_core import Agent, AgentConfig

async def main():
    config = AgentConfig(model="anthropic/claude-haiku-4-5")
    agent = Agent(config)
    result = await agent.run("What is the capital of France?")
    print(result.content)          # "The capital of France is Paris."
    print(result.total_tokens)     # e.g. 45
    print(result.tool_uses)        # 0

asyncio.run(main())
```

## 3. Give the agent tools

The agent auto-discovers built-in tools. Just ask it to do something that
requires a tool:

```python
result = await agent.run("Read the file at /tmp/hello.txt")
# Agent uses the Read tool, returns the file content
```

## 4. Stream responses

```python
from agent_core import AssistantText, ToolUse, ToolResult

async for msg in agent.stream("List all Python files in src/"):
    match msg:
        case AssistantText(content=text):
            print(text, end="", flush=True)
        case ToolUse(tool_name=name, tool_input=inp):
            print(f"\n[Using {name}: {inp}]")
        case ToolResult(tool_name=name):
            print(f"[{name} done]")
```

## 5. Choose your model

| Identifier | Provider | Best for |
|-----------|----------|----------|
| `anthropic/claude-sonnet-4-5` | Anthropic | General purpose |
| `anthropic/claude-opus-4-7` | Anthropic | Complex reasoning |
| `anthropic/claude-haiku-4-5` | Anthropic | Fast / cheap |
| `openai/gpt-4o` | OpenAI | General purpose |
| `deepseek/deepseek-v4-pro` | DeepSeek | Value / throughput |

## 6. Configuration

```python
from agent_core import AgentConfig, CompressionConfig

config = AgentConfig(
    model="anthropic/claude-sonnet-4-5",
    max_turns=20,                # max conversation rounds
    permission_mode="ask",        # bypassPermissions | acceptEdits | ask
    tools=["*"],                 # tool whitelist, ["*"] = all
    api_keys={"anthropic": "sk-..."},  # or use env vars
    compression=CompressionConfig(enabled=True),
)
```

## CLI

```bash
# Interactive REPL
agent-core

# Or via module
python -m agent_core

# Pipe mode
echo "Read README.md" | python -m agent_core

# Resume a session
python -m agent_core --resume <session-id>

# Continue last session
python -m agent_core --continue
```

Next: [Agent API reference](api/agent.md) · [Tools](api/tools.md)
