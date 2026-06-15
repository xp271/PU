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
import sys
from pathlib import Path
from typing import Any

from .agent import Agent
from .models import AgentConfig, ModelProvider

# ---------------------------------------------------------------------------
# Config file resolution
# ---------------------------------------------------------------------------

_ENV_VAR_MAP: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
}


def _find_config_file() -> Path | None:
    """Locate the first existing config file in priority order."""
    candidates: list[Path] = []

    override = os.environ.get("AGENT_CORE_CONFIG")
    if override:
        candidates.append(Path(override))

    candidates.append(Path("agent-core.config.json"))
    candidates.append(Path.home() / ".agent-core" / "config.json")

    for p in candidates:
        if p.is_file():
            return p
    return None


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

    return AgentConfig(
        model=model,
        api_keys=api_keys,
        max_turns=max_turns,
        permission_mode=permission_mode,  # type: ignore[arg-type]
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
        try:
            async for msg in agent.stream(prompt):
                if msg.type == "assistant_text":
                    print(msg.content, end="", flush=True)
                elif msg.type == "tool_use":
                    print(f"\n[{msg.tool_name}] {_summarise(msg.tool_input)}")
                elif msg.type == "tool_result":
                    status = "ERR" if msg.is_error else "OK"
                    preview = _ellipsis(str(msg.content), 120)
                    print(f"  -> {status}: {preview}")
                elif msg.type == "error":
                    print(f"\n[ERROR] {msg.message}")
                elif msg.type == "done":
                    print(f"\n--- {msg.stop_reason} | {msg.total_tokens} tokens ---")
        except KeyboardInterrupt:
            print("\n[cancelled]")
        except Exception as exc:
            print(f"\n[exception] {type(exc).__name__}: {exc}")


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
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point (agent-core console script)."""
    config = _load_config()
    asyncio.run(_repl(config))


if __name__ == "__main__":
    main()
