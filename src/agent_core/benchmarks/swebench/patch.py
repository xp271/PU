"""Patch extraction and export for SWE-Bench Pro.

Extracts unified git diffs from agent-modified working trees and exports
them in the JSON format expected by the official evaluation script.

Usage::

    from agent_core.benchmarks.swebench.patch import extract_git_diff, export_patches_json

    diff = extract_git_diff("/path/to/repo", base_commit="abc123")
    export_patches_json(results, "patches.json", prefix="my-model")
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger("agent_core.benchmarks.swebench.patch")


def extract_git_diff(repo_path: str, base_commit: str) -> str:
    """Extract unified diff from a git working tree.

    Uses ``git diff <base_commit>`` to compare the base_commit tree with the
    current working tree. This captures all agent edits (including staged and
    unstaged changes) without requiring a git commit.

    The ``diff`` tool is also used for detecting renames, and we never
    include untracked (new) files as SWE-Bench agents rarely create them.

    Args:
        repo_path: Absolute path to the git repository.
        base_commit: The base commit SHA to diff against.

    Returns:
        Unified diff string (empty if no changes).
    """
    repo = Path(repo_path)
    if not (repo / ".git").is_dir():
        logger.warning("%s is not a git repository", repo)
        return ""

    try:
        result = subprocess.run(
            ["git", "diff", base_commit],
            capture_output=True,
            text=True,
            cwd=str(repo),
            timeout=30,
        )
        diff = result.stdout

        # Include diffs for untracked (new) files — rare in SWE-Bench but
        # harmless to check.
        untracked_result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            cwd=str(repo),
            timeout=10,
        )
        untracked = untracked_result.stdout.strip().splitlines()
        for f in untracked:
            file_path = repo / f
            if not file_path.is_file():
                continue
            try:
                new_file_diff = subprocess.run(
                    ["git", "diff", "--no-index", "/dev/null", str(file_path)],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if new_file_diff.stdout:
                    diff += new_file_diff.stdout
            except Exception as exc:
                logger.warning("Failed to diff new file %s: %s", f, exc)

        return diff

    except subprocess.TimeoutExpired:
        logger.warning("git diff timed out in %s", repo_path)
        return ""
    except Exception as exc:
        logger.warning("git diff failed in %s: %s", repo_path, exc)
        return ""


def export_patches_json(
    results: list[dict[str, Any]],
    output_path: str,
    prefix: str = "agent-core",
) -> None:
    """Export results as a patches.json file for SWE-Bench eval.

    Format matches the official evaluation script's expectations::

        [
          {"instance_id": "django__django-12345", "patch": "diff --git ...", "prefix": "my-model"},
          ...
        ]

    Args:
        results: List of result dicts, each with at least ``instance_id`` and ``patch``.
        output_path: Path to write the JSON file.
        prefix: Model prefix string.
    """
    patches: list[dict[str, str]] = []
    for r in results:
        patch = r.get("patch", "")
        if patch:
            patches.append({
                "instance_id": r["instance_id"],
                "patch": patch,
                "prefix": prefix,
            })

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(patches, f, indent=2, ensure_ascii=False)

    logger.info(
        "Exported %d patches (out of %d results) to %s",
        len(patches),
        len(results),
        output_path,
    )
