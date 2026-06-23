"""Agent-definition loaders — markdown frontmatter, JSON, and directory discovery.

All loaders return ``AgentDefinition | None`` (``None`` on parse failure);
they never raise exceptions for malformed input.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("agent_core.agents.loader")

from .definition import AgentDefinition


# ---------------------------------------------------------------------------
# Markdown (frontmatter)
# ---------------------------------------------------------------------------

def load_markdown_definition(file_path: str) -> AgentDefinition | None:
    """Parse an ``AgentDefinition`` from YAML frontmatter in a ``.md`` file.

    The body text is ignored (reserved for the Skill system).

    Returns ``None`` when the file has no frontmatter or the frontmatter
    is missing required fields — a warning is logged in that case.
    """
    try:
        import frontmatter
    except ImportError:
        logger.warning(
            "python-frontmatter is not installed — cannot load %s", file_path
        )
        return None

    try:
        post = frontmatter.load(file_path)
    except Exception as exc:
        logger.warning("Failed to read/parse '%s': %s", file_path, exc)
        return None

    # No frontmatter at all
    if not post.metadata:
        logger.debug("No frontmatter found in '%s', skipping.", file_path)
        return None

    # The frontmatter must at minimum declare agent_type
    if "agent_type" not in post.metadata:
        logger.debug(
            "Markdown file '%s' has frontmatter but no 'agent_type' key — skipping.",
            file_path,
        )
        return None

    try:
        return AgentDefinition.model_validate(post.metadata)
    except Exception as exc:
        logger.warning(
            "Invalid AgentDefinition in '%s': %s", file_path, exc
        )
        return None


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

def load_json_definition(file_path: str) -> AgentDefinition | None:
    """Parse an ``AgentDefinition`` from a ``.json`` file.

    Returns ``None`` on decode/validation errors (warning logged).
    """
    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read JSON '%s': %s", file_path, exc)
        return None

    if not isinstance(data, dict):
        logger.warning("JSON '%s' is not a dict/object — skipping.", file_path)
        return None

    try:
        return AgentDefinition.model_validate(data)
    except Exception as exc:
        logger.warning(
            "Invalid AgentDefinition in '%s': %s", file_path, exc
        )
        return None


# ---------------------------------------------------------------------------
# Directory discovery
# ---------------------------------------------------------------------------

def discover_agents(
    directory: str,
    source: str = "project",
    include_plain_md: bool = False,
) -> dict[str, AgentDefinition]:
    """Recursively scan *directory* for agent-definition files.

    By default only ``<name>.agent.md`` and ``<name>.agent.json`` are
    recognised.  Set *include_plain_md* to ``True`` to also try bare
    ``.md`` files (compatibility with SKILL.md-style files).

    When the same *agent_type* appears in multiple files the order of
    precedence is: ``.agent.md`` > ``.agent.json`` > ``.md``.
    A warning is issued on conflict.

    **Returns** a dict mapping ``agent_type`` → ``AgentDefinition``.
    Empty dict when the directory does not exist or contains no definitions.
    """
    root = Path(directory)
    if not root.is_dir():
        logger.debug("Agent discovery directory '%s' does not exist.", directory)
        return {}

    candidates: list[tuple[str, AgentDefinition, str]] = []
    # (agent_type, definition, file_stem_for_conflict_msg)

    # Collect .agent.md first (highest priority among discovered files)
    for md_path in root.rglob("*.agent.md"):
        d = load_markdown_definition(str(md_path))
        if d is not None:
            candidates.append((d.agent_type, d, f"{md_path.name} (.agent.md)"))

    # Then .agent.json
    for json_path in root.rglob("*.agent.json"):
        d = load_json_definition(str(json_path))
        if d is not None:
            candidates.append((d.agent_type, d, f"{json_path.name} (.agent.json)"))

    # Optionally bare .md (lowest priority)
    if include_plain_md:
        for md_path in root.rglob("*.md"):
            # Skip files already handled as .agent.md
            if md_path.name.endswith(".agent.md"):
                continue
            d = load_markdown_definition(str(md_path))
            if d is not None:
                candidates.append((d.agent_type, d, f"{md_path.name} (.md)"))

    # Build result dict; first-come wins (we inserted in priority order)
    result: dict[str, AgentDefinition] = {}
    for agent_type, definition, label in candidates:
        if agent_type in result:
            logger.warning(
                "Duplicate agent_type '%s' found during discovery; "
                "keeping earlier file, ignoring %s.",
                agent_type,
                label,
            )
        else:
            result[agent_type] = definition

    logger.debug(
        "Discovered %d agent(s) in '%s'.", len(result), directory
    )
    return result
