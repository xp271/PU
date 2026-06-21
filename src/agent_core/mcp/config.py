"""
MCP configuration loading chain.

Priority (highest wins):
1. ``AgentConfig.mcp_servers`` (code injection)
2. ``.mcp.json`` (project root)
3. ``~/.agent/mcp.json`` (user home)

``${ENV_VAR}`` placeholders in string values are expanded at load time.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from ..models import McpServerConfig

logger = logging.getLogger("agent_core.mcp.config")

_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_vars(obj: Any) -> Any:
    """Recursively expand ``${ENV_VAR}`` placeholders.

    A missing variable raises ``KeyError`` (fail fast) rather than
    silently expanding to an empty string.
    """

    def _sub(m: re.Match) -> str:
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


def load_mcp_servers() -> dict[str, McpServerConfig]:
    """Load MCP servers from the file chain.

    Merges server entries by **name** (project-level replaces user-level
    for the same server key — not a deep field-level merge).
    """
    servers: dict[str, Any] = {}

    # 1. User-level
    user_mcp = Path.home() / ".agent" / "mcp.json"
    if user_mcp.is_file():
        try:
            raw = json.loads(user_mcp.read_text(encoding="utf-8"))
            servers.update(raw.get("mcpServers", {}))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read %s: %s", user_mcp, exc)

    # 2. Project-level (overrides same-named entries)
    project_mcp = Path(".mcp.json").resolve()
    if project_mcp.is_file():
        try:
            raw = json.loads(project_mcp.read_text(encoding="utf-8"))
            servers.update(raw.get("mcpServers", {}))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read %s: %s", project_mcp, exc)

    # Expand ${ENV_VAR} placeholders, then validate into typed configs
    result: dict[str, McpServerConfig] = {}
    for name, cfg in servers.items():
        try:
            expanded = _expand_env_vars(cfg)
            result[name] = McpServerConfig(**expanded)
        except Exception as exc:
            logger.error(
                "Failed to parse MCP server config '%s' (skipping): %s",
                name,
                exc,
            )
    return result
