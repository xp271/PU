"""Unit tests for context enrichment."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

from agent_core.query.context import enrich_context, _run_git_status


@pytest.mark.asyncio
async def test_enrich_context_includes_date(tmp_path) -> None:
    result = await enrich_context(workspace_root=str(tmp_path))
    assert "Today's date is" in result


@pytest.mark.asyncio
async def test_enrich_context_includes_environment(tmp_path) -> None:
    result = await enrich_context(workspace_root=str(tmp_path))
    assert "<environment>" in result
    assert "Platform:" in result
    assert "Shell:" in result
    assert "Workspace:" in result
    assert str(tmp_path) in result


@pytest.mark.asyncio
async def test_enrich_context_workspace_fallback() -> None:
    result = await enrich_context(workspace_root=None)
    assert "Workspace:" in result
    assert os.getcwd() in result


@pytest.mark.asyncio
async def test_enrich_context_no_git_in_non_repo(tmp_path) -> None:
    """In a non-git directory, only date + environment are returned."""
    result = await enrich_context(workspace_root=str(tmp_path))
    assert "<git_status>" not in result


@pytest.mark.asyncio
async def test_enrich_context_none_workspace() -> None:
    result = await enrich_context(workspace_root=None)
    assert "Today's date is" in result


# ---------------------------------------------------------------------------
# _run_git_status edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_git_status_in_git_repo() -> None:
    """In the project's own git repo, git status should return output."""
    # The test is run from inside a git repo (the project itself)
    result = await _run_git_status(".")
    # Could be None if repo is clean (no output), or a string if dirty
    # Either is valid — the function should not raise
    assert result is None or isinstance(result, str)


@pytest.mark.asyncio
async def test_run_git_status_non_git_directory(tmp_path) -> None:
    """In a non-git directory, should return None."""
    result = await _run_git_status(str(tmp_path))
    assert result is None


@pytest.mark.asyncio
async def test_run_git_status_timeout_handled_gracefully() -> None:
    """When git hangs, timeout should be caught and return None."""
    with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError("timeout")):
        result = await _run_git_status(".")
        assert result is None


@pytest.mark.asyncio
async def test_run_git_status_missing_binary() -> None:
    """When git is not available, should return None."""
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        result = await _run_git_status(".")
        assert result is None


@pytest.mark.asyncio
async def test_run_git_status_unexpected_error() -> None:
    """Any unexpected error is caught and returns None."""
    with patch("asyncio.create_subprocess_exec", side_effect=OSError("broken pipe")):
        result = await _run_git_status(".")
        assert result is None


@pytest.mark.asyncio
async def test_enrich_context_with_git_status() -> None:
    """When git status returns output, it appears in the context."""
    with patch(
        "agent_core.query.context._run_git_status", return_value="M foo.py\n?? bar.py"
    ):
        result = await enrich_context(workspace_root=".")
        assert "<git_status>" in result
        assert "M foo.py" in result
        assert "?? bar.py" in result
        assert "</git_status>" in result


@pytest.mark.asyncio
async def test_enrich_context_with_empty_git_status() -> None:
    """When git status returns empty string, no git_status block."""
    with patch(
        "agent_core.query.context._run_git_status", return_value=""
    ):
        result = await enrich_context(workspace_root=".")
        assert "<git_status>" not in result
