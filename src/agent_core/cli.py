"""Interactive CLI for manual Agent testing.

Usage:
    agent-core               # interactive REPL
    echo "prompt" | agent-core  # pipe mode (one-shot)

Configuration (first found wins):
    1. Path in $AGENT_CORE_CONFIG
    2. ./agent-core.config.json   (current working directory)
    3. ~/.agent-core/config.json  (user home)

Config file format (JSON):

    {
        "model": "anthropic/claude-sonnet-4-5",
        "api_keys": {
            "anthropic": "sk-ant-xxx",
            "openai": "sk-xxx"
        },
        "max_turns": 20,
        "permission_mode": "bypassPermissions"
    }

Missing api_keys fall back to environment variables
(ANTHROPIC_API_KEY / OPENAI_API_KEY).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from .agent import Agent
from .mcp.client import McpSessionManager
from .models import AgentConfig, ModelId, ModelProvider

# ---------------------------------------------------------------------------
# Config file resolution
# ---------------------------------------------------------------------------

_ENV_VAR_MAP: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def _find_config_file() -> Path | None:
    """Locate the first existing config file in priority order.

    1. ``$AGENT_CORE_CONFIG`` environment variable (absolute path)
    2. ``./agent-core.config.json`` (current working directory)
    3. ``{package_root}/agent-core.config.json`` (resolved via ``__file__``;
       works for editable installs where the package lives inside the project)
    4. ``~/.agent-core/config.json`` (user home)
    """
    candidates: list[Path] = []

    override = os.environ.get("AGENT_CORE_CONFIG")
    if override:
        candidates.append(Path(override).expanduser().resolve())

    candidates.append(Path("agent-core.config.json").resolve())

    # Search relative to the package install location.
    # For editable installs (``pip install -e .``) this resolves to the
    # project root (e.g. ``C:\\PU``).  For regular installs it falls
    # through silently when no config is found.
    _pkg_root = Path(__file__).resolve().parent.parent.parent
    candidates.append(_pkg_root / "agent-core.config.json")

    candidates.append(Path.home() / ".agent-core" / "config.json")

    for p in candidates:
        if p.is_file():
            return p
    return None


# -- MCP config loading helpers ----------------------------------------------


_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_vars(obj: Any) -> Any:
    """Recursively expand ``${ENV_VAR}`` placeholders."""

    def _sub(m: Any) -> str:
        var = m.group(1)
        if var not in os.environ:
            raise KeyError(
                f"Environment variable '{var}' referenced in .mcp.json is not set"
            )
        return os.environ[var]

    if isinstance(obj, str):
        return _ENV_PLACEHOLDER.sub(_sub, obj)
    if isinstance(obj, list):
        return [_expand_env_vars(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    return obj


def _load_mcp_servers_raw() -> dict[str, Any]:
    """Load MCP servers from file chain: ~/.agent/mcp.json → .mcp.json."""
    servers: dict[str, Any] = {}

    # 1. User-level
    user_mcp = Path.home() / ".agent" / "mcp.json"
    if user_mcp.is_file():
        try:
            raw = json.loads(user_mcp.read_text(encoding="utf-8"))
            servers.update(raw.get("mcpServers", {}))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[config] failed to read {user_mcp}: {exc}", file=sys.stderr)

    # 2. Project-level (overrides same-named entries)
    project_mcp = Path(".mcp.json").resolve()
    if project_mcp.is_file():
        try:
            raw = json.loads(project_mcp.read_text(encoding="utf-8"))
            servers.update(raw.get("mcpServers", {}))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[config] failed to read {project_mcp}: {exc}", file=sys.stderr)

    # Expand ${ENV_VAR} placeholders
    try:
        expanded = _expand_env_vars(servers)
        if isinstance(expanded, dict):
            servers = expanded
    except KeyError as exc:
        print(f"[config] {exc}", file=sys.stderr)

    return servers


def _load_config() -> AgentConfig:
    """Build AgentConfig from config-file + env-var fallback."""
    config_path = _find_config_file()
    raw: dict[str, Any] = {}

    if config_path is not None:
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            print(f"[config] {config_path}")
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[config] failed to read {config_path}: {exc}", file=sys.stderr)

    # -- api_keys: file values take precedence over env vars --
    api_keys: dict[str, str] = raw.get("api_keys", {})
    for provider in ModelProvider:
        if provider.value in api_keys:
            continue
        env_var = _ENV_VAR_MAP.get(provider.value)
        if env_var:
            val = os.environ.get(env_var, "")
            if val:
                api_keys[provider.value] = val

    model: str = raw.get("model", "anthropic/claude-sonnet-4-5")
    max_turns: int = raw.get("max_turns", 20)
    permission_mode: str = raw.get("permission_mode", "bypassPermissions")

    # -- MCP servers: code injection in config > .mcp.json files --
    mcp_servers: dict[str, Any] = {}
    try:
        mcp_servers = _load_mcp_servers_raw()
    except Exception as exc:
        print(f"[config] MCP config load failed: {exc}", file=sys.stderr)
    # Code injection overrides file-based config
    mcp_servers.update(raw.get("mcp_servers", {}))

    # -- Guard: refuse to start with no credentials at all --
    if not any(api_keys.values()):
        provider = ModelId.parse(model).provider.value
        if not api_keys.get(provider):
            env_var = _ENV_VAR_MAP.get(provider, f"{provider.upper()}_API_KEY")
            print(
                f"[config] ERROR: No API key found for provider '{provider}'.\n"
                f"  Searched: {config_path or 'no config file found'}\n"
                f"  Env var:  {env_var} (not set)\n"
                f"\n"
                f"  Fix: set the {env_var} environment variable, or create\n"
                f"  ~/.agent-core/config.json with an api_keys.{provider} entry.",
                file=sys.stderr,
            )
            sys.exit(1)

    return AgentConfig(
        model=model,
        api_keys=api_keys,
        max_turns=max_turns,
        permission_mode=permission_mode,  # type: ignore[arg-type]
        mcp_servers=mcp_servers,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
Commands:
  /model <provider/name>   Switch model (e.g. /model openai/gpt-4o)
  /key <provider> <key>    Set API key for a provider
  /help                    Show this help
  /quit                    Exit"""


def _handle_command(config: AgentConfig, text: str) -> bool:
    """Process a slash-command.  Returns True if the REPL should exit."""
    parts = text.split(maxsplit=2)
    cmd = parts[0].lower()

    if cmd == "/quit":
        return True

    if cmd == "/help":
        print(_HELP_TEXT)
        return False

    if cmd == "/model":
        if len(parts) < 2:
            print(f"Current model: {config.model}")
            return False
        config.model = parts[1]
        print(f"Model set to: {config.model}")
        return False

    if cmd == "/key":
        if len(parts) < 3:
            print("Usage: /key <provider> <api-key>")
            return False
        provider = parts[1]
        key = parts[2]
        config.api_keys[provider] = key
        print(f"API key updated for: {provider}")
        return False

    print(f"Unknown command: {cmd}. Type /help for available commands.")
    return False


async def _repl(config: AgentConfig) -> None:
    """Read-eval-print loop."""
    print()
    print(_HELP_TEXT)
    print()

    while True:
        try:
            if sys.stdin.isatty():
                prompt = input("agent-core> ").strip()
            else:
                prompt = sys.stdin.readline()
                if not prompt:
                    break
                prompt = prompt.strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not prompt:
            continue

        if prompt.startswith("/"):
            if _handle_command(config, prompt):
                break
            continue

        # -- Run agent with current prompt --
        agent = Agent(config)
        stream = agent.stream(prompt)
        try:
            async for msg in stream:
                if msg.type == "assistant_text":
                    print(msg.content, end="", flush=True)
                elif msg.type == "tool_use":
                    print(f"\n[{msg.tool_name}] {_summarise(msg.tool_input)}")
                elif msg.type == "tool_result":
                    if msg.is_error:
                        # 错误结果完整展示，方便定位问题
                        preview = _ellipsis(str(msg.content), 500)
                        print(f"  -> ERR: {preview}")
                    else:
                        preview = _ellipsis(str(msg.content), 120)
                        print(f"  -> OK: {preview}")
                elif msg.type == "error":
                    print(f"\n[ERROR] {msg.message}")
                elif msg.type == "done":
                    print(f"\n--- {msg.stop_reason} | {msg.total_tokens} tokens ---")
        except KeyboardInterrupt:
            print("\n[cancelled]")
        except Exception as exc:
            print(f"\n[exception] {type(exc).__name__}: {exc}")
        finally:
            # Close the stream (and its MCP sessions) in THIS task, even if the
            # loop was abandoned via exception — never at asyncio.run() shutdown
            # in a different task (anyio cancel-scope requirement).
            await stream.aclose()


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _summarise(obj: object) -> str:
    """Compact representation of a tool input dict."""
    if isinstance(obj, dict):
        parts = []
        for k, v in obj.items():
            parts.append(f"{k}={_ellipsis(str(v), 40)}")
        return "{" + ", ".join(parts) + "}"
    return str(obj)


def _ellipsis(text: str, max_len: int) -> str:
    """Truncate *text* to *max_len* with an ellipsis if needed."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


# ---------------------------------------------------------------------------
# MCP startup preflight
# ---------------------------------------------------------------------------

async def _preflight_mcp(config: AgentConfig) -> None:
    """Connect to every configured MCP server once, at startup.

    This triggers any ``npx -y`` package download and verifies the
    handshake *before* the REPL accepts input — so the first real query
    isn't blocked on a cold download, and an unreachable/broken server
    surfaces immediately instead of mid-query.

    Sessions are closed again right away: each ``query()`` opens its own
    (the manager is query-scoped, and connect/aclose must run in the same
    task).  The win here is warming the npx cache + reporting status.
    ``connect()`` and ``aclose()`` run in this single ``asyncio.run`` task,
    satisfying the anyio same-task requirement.
    """
    if not config.mcp_servers:
        return

    print(f"[mcp] checking {len(config.mcp_servers)} server(s)…")
    mcp = McpSessionManager()
    try:
        for name, server_config in config.mcp_servers.items():
            try:
                state = await mcp.connect(name, server_config)
            except Exception as exc:  # defensive: never block startup
                print(f"[mcp]   x {name}: {exc}", file=sys.stderr)
                continue
            if state.connection_error is None:
                print(f"[mcp]   ok {name}: {len(state.tools)} tool(s)")
            else:
                print(
                    f"[mcp]   x {name}: {state.connection_error}",
                    file=sys.stderr,
                )
    finally:
        await mcp.aclose()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main(config: AgentConfig) -> None:
    """Async entry: preflight MCP servers, then run the REPL."""
    await _preflight_mcp(config)
    await _repl(config)


def main() -> None:
    """CLI entry point (agent-core console script)."""
    config = _load_config()
    asyncio.run(_main(config))


if __name__ == "__main__":
    main()
