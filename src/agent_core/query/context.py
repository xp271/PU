"""
Context enrichment for the agent query loop.

Gathers environment information (git status, current date) once at
the start of ``query()`` and returns it as a string suitable for the
``system`` parameter of the LLM call.

Context enrichment runs *once* per ``query()`` invocation — not every
turn of the loop.  This mirrors the cc-haha-main pattern where
``getSystemContext()`` / ``getUserContext()`` are memoized for the
lifetime of a conversation.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date


async def enrich_context(workspace_root: str | None = None) -> str:
    """Collect environment context and return a plain-text block.

    Currently collects:
    - Current date (ISO format)
    - ``git status --porcelain`` output (if cwd is inside a git repo)

    Returns an empty string when workspace_root is ``None`` and there is
    no git repository to report on.
    """
    parts: list[str] = []

    # Current date
    parts.append(f"Today's date is {date.today().isoformat()}.")

    # System environment
    shell = os.environ.get("COMSPEC", os.environ.get("SHELL", "unknown"))
    wd = workspace_root or os.getcwd()
    parts.append(
        "<environment>\n"
        f"  Platform: {sys.platform}\n"
        f"  Shell: {shell}\n"
        f"  Workspace: {wd}\n"
        "</environment>"
    )

    # Git status (best-effort)
    cwd = workspace_root or "."
    git_output = await _run_git_status(cwd)
    if git_output:
        parts.append("<git_status>\n" + git_output + "\n</git_status>")

    return "\n\n".join(parts)


async def _run_git_status(cwd: str) -> str | None:
    """Run ``git status --porcelain`` in *cwd*, returning stdout or ``None``.

    Failures (non-git directory, timeout, missing binary) are silently
    swallowed — git status is best-effort enrichment, never a hard error.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, _stderr = await asyncio.wait_for(
            proc.communicate(), timeout=5.0
        )
        if proc.returncode == 0:
            output = stdout.decode("utf-8", errors="replace").strip()
            if output:
                return output
    except Exception:
        pass
    return None
