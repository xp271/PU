"""共享配置加载 — 供 _main.py（REPL）和 wechat.py（微信客服）共同使用。

提取自 cli.py，保持原有逻辑不变。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from ..models import AgentConfig, ModelId, ModelProvider

_ENV_VAR_MAP: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "llamacpp": "LLAMACPP_API_KEY",
}

_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _find_config_file() -> Path | None:
    """Locate the first existing config file in priority order.

    1. ``$AGENT_CORE_CONFIG`` environment variable (absolute path)
    2. ``./agent-core.config.json`` (current working directory)
    3. ``{package_root}/agent-core.config.json``
    4. ``~/.agent-core/config.json`` (user home)
    """
    candidates: list[Path] = []

    override = os.environ.get("AGENT_CORE_CONFIG")
    if override:
        candidates.append(Path(override).expanduser().resolve())

    candidates.append(Path("agent-core.config.json").resolve())

    _pkg_root = Path(__file__).resolve().parent.parent.parent.parent
    candidates.append(_pkg_root / "agent-core.config.json")

    candidates.append(Path.home() / ".agent-core" / "config.json")

    for p in candidates:
        if p.is_file():
            return p
    return None


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

    user_mcp = Path.home() / ".agent" / "mcp.json"
    if user_mcp.is_file():
        try:
            raw = json.loads(user_mcp.read_text(encoding="utf-8"))
            servers.update(raw.get("mcpServers", {}))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[config] failed to read {user_mcp}: {exc}", file=sys.stderr)

    project_mcp = Path(".mcp.json").resolve()
    if project_mcp.is_file():
        try:
            raw = json.loads(project_mcp.read_text(encoding="utf-8"))
            servers.update(raw.get("mcpServers", {}))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[config] failed to read {project_mcp}: {exc}", file=sys.stderr)

    try:
        expanded = _expand_env_vars(servers)
        if isinstance(expanded, dict):
            servers = expanded
    except KeyError as exc:
        print(f"[config] {exc}", file=sys.stderr)

    return servers


def _load_config(config_path_override: str | None = None) -> AgentConfig:
    """Build AgentConfig from config-file + env-var fallback.

    When *config_path_override* is given, it is used directly (auto-find
    skipped).  Callers that already know the path (e.g. qqbot CLI) should
    use this parameter instead of polluting ``AGENT_CORE_CONFIG``.
    """
    config_path = Path(config_path_override) if config_path_override else _find_config_file()
    raw: dict[str, Any] = {}

    if config_path is not None:
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            print(f"[config] {config_path}")
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[config] failed to read {config_path}: {exc}", file=sys.stderr)

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
    permission_mode: str = raw.get("permission_mode", "ask")

    mcp_servers: dict[str, Any] = {}
    try:
        mcp_servers = _load_mcp_servers_raw()
    except Exception as exc:
        print(f"[config] MCP config load failed: {exc}", file=sys.stderr)
    mcp_servers.update(raw.get("mcp_servers", {}))

    _NO_AUTH_PROVIDERS = {"llamacpp"}
    if not any(api_keys.values()):
        provider = ModelId.parse(model).provider.value
        if provider not in _NO_AUTH_PROVIDERS and not api_keys.get(provider):
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
        allow_unknown_model=raw.get("allow_unknown_model", False),
        base_urls=raw.get("base_urls", {}),
        context_size=raw.get("context_size"),
        sampling=raw.get("sampling"),
        llamacpp=raw.get("llamacpp", {}),
    )
