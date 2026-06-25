# Sub-Agent

A sub-agent is a child agent spawned by the parent via the `Agent` tool. Each
sub-agent runs in an isolated environment with its own tool set, permission
mode, and system prompt.

## Quick example

The parent LLM decides when to spawn a sub-agent. You just ask:

```python
result = await agent.run(
    "Explore the codebase to find how authentication works, "
    "then write a summary to auth_overview.md."
)
# The Agent may spawn an Explore sub-agent to search for auth code,
# then use the Write tool itself to produce the summary.
```

## `AgentDefinition`

A configuration template for a sub-agent type:

```python
from agent_core import AgentDefinition, AgentMemory

definition = AgentDefinition(
    agent_type="code-reviewer",               # unique identifier
    when_to_use="When code changes need review",
    tools=["Read", "Glob", "Grep"],           # tool whitelist
    disallowed_tools=["Bash"],                # tool blacklist
    auto_approve_tools=["Read"],              # pre-authorized tools
    system_prompt="You are a code reviewer...",
    model=None,                               # None = inherit parent's model
    permission_mode=None,                     # None = inherit parent's mode
    memory=AgentMemory.LOCAL,                 # scope: USER | PROJECT | LOCAL | NONE
)
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `agent_type` | `str` | **(required)** | Unique ID, `[a-zA-Z0-9_-]` only |
| `when_to_use` | `str \| None` | `None` | Description for LLM routing decisions |
| `tools` | `list[str]` | `["*"]` | Tool whitelist; `["*"]` = parent's allowed tools |
| `disallowed_tools` | `list[str]` | `[]` | Tool blacklist; merged as ∪ across priority layers |
| `auto_approve_tools` | `list[str]` | `[]` | Pre-authorized tools (still filtered by parent allowlist) |
| `system_prompt` | `str` | `""` | Injected into the sub-agent's system prompt |
| `model` | `str \| None` | `None` | Model ID; None = inherit parent |
| `permission_mode` | `PermissionMode \| None` | `None` | None = inherit parent; actual = min(parent, declared) |
| `mcp_servers` | `dict` | `{}` | Sub-agent MCP servers; child overrides parent on key conflict |
| `hooks` | `list` | `[]` | Sub-agent hook instances |
| `skills` | `list[str]` | `[]` | Pre-loaded skill names |
| `memory` | `AgentMemory` | `NONE` | Persistence mode |
| `background` | `bool` | `False` | Default to async background execution |

## Built-in agents

Three agents are registered automatically when `agent_core.agents` is imported:

### `GeneralPurpose`

| Property | Value |
|----------|-------|
| tools | `["*"]` (all tools, intersected with parent) |
| permission_mode | `None` (inherit) |
| purpose | Default agent, no specialisation |

### `Explore`

| Property | Value |
|----------|-------|
| tools | `["Read", "Glob", "Grep"]` (read-only) |
| permission_mode | `None` (inherit) |
| purpose | Codebase exploration — search, read, find patterns |

### `Plan`

| Property | Value |
|----------|-------|
| tools | `["Read", "Glob", "Grep"]` (read-only) |
| permission_mode | `None` (inherit) |
| purpose | Upfront planning — produce text plans before implementation |

## Registration & loading

```python
from agent_core import register_agent, get_agent, list_agents

# Programmatic registration
register_agent(my_definition, source="project")

# Lookup (returns merged definition across priority layers)
explore = get_agent("Explore")

# List all
for name, defn in list_agents().items():
    print(f"{name}: {defn.when_to_use}")
```

Four priority layers (low → high): `builtin` < `plugin` < `project` < `user`.

### Loading from files

```python
from agent_core.agents.loader import discover_agents, load_markdown_definition

# Discover from .agent/agents/
agents = discover_agents(".agent/agents/", source="project")
```

Markdown frontmatter format:

```markdown
---
agent_type: code-reviewer
when_to_use: "When code changes need review"
tools:
  - Read
  - Glob
  - Grep
system_prompt: |
  You are a code reviewer...
---
```

## Security invariants

1. Child can **never** upgrade permission mode (`min(parent, child)`)
2. Child can **never** use parent-disallowed tools (∪ union)
3. Child can **only** use tools allowed by both parent and child (∩ intersection)
4. Recursion depth capped at `MAX_AGENT_DEPTH = 3`

## Behind the scenes: `run_agent()`

When a sub-agent launches, the engine:

1. Resolves model / tools / permissions / MCP from the definition
2. Creates an isolated `ToolUseContext` (own file cache, abort signal)
3. Opens a sidechain transcript at `~/.agent/projects/{hash}/{session_id}/{agent_id}.jsonl`
4. Calls `query()` with the sub-agent's system prompt and initial messages
5. Collects results, writes sidechain, cleans up

## `AgentTool` input schema

When the LLM decides to use `Agent`, it supplies:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `description` | `str` | **(required)** | 3–5 word task summary |
| `prompt` | `str` | **(required)** | Task for the sub-agent |
| `subagent_type` | `str` | `"GeneralPurpose"` | `"Explore"` / `"Plan"` / `"GeneralPurpose"` |
| `model` | `str \| None` | `None` | Shorthand: `"sonnet"` / `"opus"` / `"haiku"` |
| `run_in_background` | `bool` | `False` | Async background execution |
