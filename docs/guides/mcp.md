# MCP Integration

The [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) lets your
agent discover and use tools from external servers — no glue code required.

## Quick setup

Create a `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["-y", "@playwright/mcp"]
    },
    "duckduckgo-search": {
      "command": "npx",
      "args": ["-y", "duckduckgo-mcp-server"]
    }
  }
}
```

When the agent starts, it auto-connects to each server, lists tools, and
registers them. You can then use them directly:

```python
result = await agent.run("Search the web for latest Python 3.13 release notes")
# Agent calls mcp__duckduckgo-search__search automatically
```

## Configuration

### `McpServerConfig`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `transport` | `"stdio"` / `"sse"` / `"websocket"` | `"stdio"` | Transport method |
| `command` | `str \| None` | `None` | stdio: executable path |
| `args` | `list[str]` | `[]` | stdio: startup arguments |
| `env` | `dict[str, str]` | `{}` | stdio: child process environment |
| `url` | `str \| None` | `None` | sse/websocket: server endpoint URL |
| `headers` | `dict[str, str]` | `{}` | Additional HTTP headers |
| `cwd` | `str \| None` | `None` | stdio: child process working directory |
| `auth` | `McpOAuthConfig \| None` | `None` | OAuth authentication |
| `include_tools` | `list[str] \| None` | `None` | Regex whitelist for tool names |
| `exclude_tools` | `list[str] \| None` | `None` | Regex blacklist for tool names |
| `connect_timeout` | `float` | `30.0` | Connection timeout (seconds) |
| `request_timeout` | `float` | `60.0` | Per-request timeout (seconds) |
| `max_reconnect_attempts` | `int` | `3` | Retry attempts on failure |
| `reconnect_delay` | `float` | `2.0` | Retry backoff base (seconds) |

### Transport types

| Transport | Use when | Requires |
|-----------|----------|----------|
| `stdio` (default) | Local CLI tools, `npx` packages | `command` + optionally `args` / `env` |
| `sse` | Remote HTTP MCP servers | `url` |
| `websocket` | Full-duplex remote servers (reserved) | `url` |

### Environment variable expansion

The config loader expands `${ENV_VAR}` placeholders in all string values:

```json
{
  "env": {
    "BRAVE_API_KEY": "${BRAVE_API_KEY}"
  }
}
```

Missing env vars cause a `KeyError` (fail fast).

## Config loading chain

Priority (high overwrites low):

1. `AgentConfig.mcp_servers` (code-injected)
2. `.mcp.json` (project root)
3. `~/.agent/mcp.json` (user home)

Same server name: project-level **replaces** user-level (not deep merge).

## Tool naming

MCP tools are namespaced to avoid collisions with built-in tools:

```
mcp__{sanitized_server}__{tool_name}
```

Examples:

| Server | Tool | Registered as |
|--------|------|---------------|
| `playwright` | `browser_navigate` | `mcp__playwright__browser_navigate` |
| `duckduckgo-search` | `search` | `mcp__duckduckgo-search__search` |
| `excel` | `read_data_from_excel` | `mcp__excel__read_data_from_excel` |

Characters outside `[a-zA-Z0-9_-]` (like `@` and `/`) are replaced with `_`:

```
@playwright/mcp → _playwright_mcp → mcp___playwright_mcp__browser_navigate
```

If an MCP tool name collides with a built-in tool, the **built-in** wins and
the MCP tool is only available under its namespaced name.

## Filtering tools

Use `include_tools` / `exclude_tools` with regex patterns:

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["-y", "@playwright/mcp"],
      "include_tools": ["browser_.*"],
      "exclude_tools": ["browser_close"]
    }
  }
}
```

## OAuth authentication

For MCP servers requiring OAuth 2.0/2.1:

```json
{
  "mcpServers": {
    "github": {
      "url": "https://mcp.github.com/sse",
      "transport": "sse",
      "auth": {
        "client_id": "${GITHUB_CLIENT_ID}",
        "client_secret": "${GITHUB_CLIENT_SECRET}",
        "token_url": "https://github.com/login/oauth/access_token",
        "scopes": ["repo", "read:org"]
      }
    }
  }
}
```

Supported flows: client_credentials, authorization_code (with PKCE), device_code.
Token refresh happens automatically when the token enters its last 20% lifetime.

## Startup preflight (CLI)

The CLI REPL runs a preflight check before accepting input: connects to every
configured server, calls `list_tools`, then disconnects. This:

1. **Warms** the npm cache (first `npx -y` download won't block the first real query)
2. **Health-checks** servers — broken ones print `[mcp] x <name>: <reason>` to stderr immediately

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `NotImplementedError` on every query | Dynamic `Tool` subclass missing `input_schema()` | Check `registry.py` classmethod wiring |
| `KeyError: Environment variable` at startup | `.mcp.json` uses `${X}` but X not set | Set the env var or hardcode the value |
| `RuntimeError: exit cancel scope in a different task` | `mcp.aclose()` called from wrong task | Ensure `query()` uses `try/finally` wrapping |
| `npx -y` download timeout | First download exceeds `connect_timeout` (30s) | Increase `connect_timeout` in `.mcp.json` |
