"""SWE-Bench Pro dataset loading.

Supports:
- HuggingFace datasets (primary)
- Local CSV/JSONL (offline fallback)

Usage::

    from agent_core.benchmarks.swebench.dataset import load_swebench_dataset, SweBenchInstance

    instances = load_swebench_dataset()
    # Or from local file:
    instances = load_swebench_dataset(local_path="swe_bench_pro.csv")
    # Or with filters:
    instances = load_swebench_dataset(repo_filter="django/django", max_instances=5)
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger("agent_core.benchmarks.swebench")


class SweBenchInstance(BaseModel):
    """A single SWE-Bench task instance."""

    instance_id: str = Field(description="Unique task identifier, e.g. 'django__django-12345'")
    repo: str = Field(description="Repository name, e.g. 'django/django'")
    problem_statement: str = Field(description="Issue description to fix")
    base_commit: str = Field(description="Starting commit SHA")
    hint_text: str | None = Field(default=None, description="Optional hint from dataset")
    version: str = Field(default="", description="Dataset version string")

    # Additional metadata from the dataset (stored but not used by runner directly)
    extra_fields: dict = Field(
        default_factory=dict,
        description="Any additional fields from the dataset row",
        exclude=True,
    )


def load_swebench_dataset(
    *,
    dataset_name: str = "ScaleAI/SWE-bench_Pro",
    split: str = "test",
    local_path: str | None = None,
    instance_ids: list[str] | None = None,
    repo_filter: str | None = None,
    max_instances: int | None = None,
) -> list[SweBenchInstance]:
    """Load SWE-Bench Pro dataset instances.

    Precedence: *local_path* > HuggingFace datasets library.

    Args:
        dataset_name: HuggingFace dataset identifier.
        split: Dataset split name (typically "test").
        local_path: Path to a local CSV/JSONL file (offline fallback).
        instance_ids: If set, only return these instance IDs.
        repo_filter: If set, only return instances from this repo.
        max_instances: If set, limit total instances returned.

    Returns:
        List of SweBenchInstance records.
    """
    if local_path:
        instances = _load_from_local(local_path)
    else:
        instances = _load_from_huggingface(dataset_name, split)

    # Apply filters
    if instance_ids is not None:
        id_set = frozenset(instance_ids)
        instances = [i for i in instances if i.instance_id in id_set]

    if repo_filter is not None:
        instances = [i for i in instances if i.repo == repo_filter]

    if max_instances is not None:
        instances = instances[:max_instances]

    logger.info("Loaded %d SWE-Bench instances", len(instances))
    return instances


def _load_from_huggingface(dataset_name: str, split: str) -> list[SweBenchInstance]:
    """Load instances from HuggingFace datasets."""
    try:
        from datasets import load_dataset
    except ImportError:
        raise ImportError(
            "HuggingFace 'datasets' package is required. "
            "Install with: pip install datasets\n"
            "Or use a local file: load_swebench_dataset(local_path='swe_bench.csv')"
        ) from None

    ds = load_dataset(dataset_name, split=split)
    instances: list[SweBenchInstance] = []

    for row in ds:
        row_dict = dict(row)
        instance = _row_to_instance(row_dict)
        instances.append(instance)

    return instances


def _load_from_local(path: str) -> list[SweBenchInstance]:
    """Load instances from a local CSV or JSONL file."""
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"Dataset file not found: {file_path}")

    if file_path.suffix in (".csv", ".tsv"):
        return _load_csv(file_path)
    elif file_path.suffix in (".jsonl", ".json"):
        return _load_jsonl(file_path)
    else:
        raise ValueError(
            f"Unsupported file format: {file_path.suffix}. "
            "Expected .csv, .tsv, .jsonl, or .json"
        )


def _load_csv(path: Path) -> list[SweBenchInstance]:
    delimiter = "\t" if path.suffix == ".tsv" else ","
    instances: list[SweBenchInstance] = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        for row in reader:
            instances.append(_row_to_instance(dict(row)))
    return instances


def _load_jsonl(path: Path) -> list[SweBenchInstance]:
    instances: list[SweBenchInstance] = []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()

    # Try JSON array first, then JSONL
    if content.startswith("["):
        rows = json.loads(content)
        for row in rows:
            instances.append(_row_to_instance(dict(row)))
    else:
        for line in content.splitlines():
            line = line.strip()
            if line:
                instances.append(_row_to_instance(json.loads(line)))
    return instances


def _row_to_instance(row: dict) -> SweBenchInstance:
    """Convert a raw dataset row dict to a SweBenchInstance.

    Handles common field name variations across different SWE-Bench versions.
    """
    # Normalize field names — different versions use different keys
    instance_id = _get_first(row, "instance_id", "instanceId", "id")
    repo = _get_first(row, "repo", "repository", "repo_name")
    problem_statement = _get_first(
        row, "problem_statement", "problemStatement", "problem", "issue", "issue_text"
    )
    base_commit = _get_first(row, "base_commit", "baseCommit", "base_sha", "commit")
    hint_text = _get_first(row, "hint_text", "hintText", "hint")
    version = row.get("version", "")

    if not all([instance_id, repo, problem_statement, base_commit]):
        missing = []
        if not instance_id:
            missing.append("instance_id")
        if not repo:
            missing.append("repo")
        if not problem_statement:
            missing.append("problem_statement")
        if not base_commit:
            missing.append("base_commit")
        raise ValueError(
            f"Row missing required fields: {missing}. Available keys: {sorted(row.keys())}"
        )

    return SweBenchInstance(
        instance_id=instance_id,
        repo=repo,
        problem_statement=problem_statement,
        base_commit=base_commit,
        hint_text=hint_text,
        version=version,
        extra_fields=row,
    )


def _get_first(row: dict, *keys: str) -> str | None:
    """Return the first key present in *row*, or None."""
    for k in keys:
        val = row.get(k)
        if val is not None and val != "":
            return str(val)
    return None
