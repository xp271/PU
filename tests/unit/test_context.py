"""Unit tests for context enrichment."""

from __future__ import annotations

import os

import pytest

from agent_core.query.context import enrich_context


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
