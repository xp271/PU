"""
Optional: MCP Resource & Prompt fetching.

Resources and prompts are not automatically registered as Agent tools.
Instead they are available for direct access via the MCP client handle
and for future mapping to the Skill system (Phase 3 — Item 9).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("agent_core.mcp.resources")


async def fetch_resources(session) -> list[dict[str, Any]]:
    """Fetch all resources from an MCP server."""
    try:
        result = await session.list_resources()
        return [
            {
                "uri": str(r.uri),
                "name": r.name,
                "description": r.description or "",
            }
            for r in result.resources
        ]
    except Exception as exc:
        logger.warning("Failed to fetch resources: %s", exc)
        return []


async def read_resource(session, uri: str) -> list[Any]:
    """Read a specific resource by URI."""
    result = await session.read_resource(uri)
    return result.contents
