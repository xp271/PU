"""SWE-Bench prompt templates — formats instances into Agent-understandable prompts."""

from __future__ import annotations

SWE_BENCH_SYSTEM_TEMPLATE = """You are an expert software engineer tasked with fixing an issue in a codebase.

## Repository
{repo} at commit {base_commit}

## Issue
{problem_statement}

## Instructions
1. Understand the issue by reading relevant files with Read, Grep, and Glob
2. Identify the root cause
3. Make necessary edits to fix the issue using the Edit tool
4. Use Bash to run tests and verify your changes
5. When you are done, summarize what you changed and stop calling tools

{hint_section}

Important: Only make changes directly related to fixing the issue. Do not refactor unrelated code.
Do not create new files unless absolutely necessary — prefer editing existing files.
Do not run destructive git commands (git reset --hard, git clean, etc.).
"""


def build_system_prompt(
    repo: str,
    base_commit: str,
    problem_statement: str,
    *,
    hint_text: str | None = None,
) -> str:
    """Build the system prompt for a SWE-Bench instance.

    The returned string is passed as ``system`` to ``Agent.run()``.
    ``enrich_context()`` appends environment context (date/platform/workspace)
    after this template, per the ``query()`` system-prompt composition logic.

    Args:
        repo: e.g. "django/django"
        base_commit: Starting commit SHA
        problem_statement: The issue description to fix
        hint_text: Optional hint from the dataset
    """
    hint_section = ""
    if hint_text:
        hint_section = f"## Hint\n{hint_text}"

    return SWE_BENCH_SYSTEM_TEMPLATE.format(
        repo=repo,
        base_commit=base_commit,
        problem_statement=problem_statement,
        hint_section=hint_section,
    )
