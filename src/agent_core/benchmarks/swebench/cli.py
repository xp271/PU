"""CLI for SWE-Bench Pro benchmarks — standalone entry point.

Usage::

    # Download dataset
    python -m agent_core.benchmarks.swebench download --output swe_bench_data/

    # Run a single instance
    python -m agent_core.benchmarks.swebench run --single "django__django-12345" \\
        --model anthropic/claude-sonnet-4-5

    # Run all instances
    python -m agent_core.benchmarks.swebench run --all --output results/

    # Gather patches
    python -m agent_core.benchmarks.swebench gather --results results/ \\
        --prefix my-model --output patches.json
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import SweBenchConfig
from .dataset import load_swebench_dataset
from .patch import export_patches_json
from .runner import SweBenchRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger("agent_core.benchmarks.swebench.cli")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SWE-Bench Pro — agent evaluation harness",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- download ----
    dl = sub.add_parser("download", help="Download SWE-Bench dataset from HuggingFace")
    dl.add_argument("--output", default="./swe_bench_data", help="Output directory")
    dl.add_argument(
        "--dataset", default="ScaleAI/SWE-bench_Pro", help="HF dataset name"
    )
    dl.add_argument("--split", default="test", help="Dataset split")
    dl.add_argument(
        "--format", choices=["jsonl", "csv"], default="jsonl", help="Output format"
    )

    # ---- run ----
    rn = sub.add_parser("run", help="Run agent on SWE-Bench instances")
    rn.add_argument("--all", action="store_true", default=True, help="Run all instances (default)")
    rn.add_argument("--single", help="Run a specific instance by ID")
    rn.add_argument("--ids", help="File with instance IDs, one per line")
    rn.add_argument("--model", default="anthropic/claude-sonnet-4-5", help="Model identifier")
    rn.add_argument("--max-turns", type=int, default=50, help="Max agent turns per instance")
    rn.add_argument("--timeout", type=int, default=3600, help="Timeout per instance (seconds)")
    rn.add_argument("--work-dir", default="./swe_bench_work", help="Working directory")
    rn.add_argument("--output", default="./swe_bench_results", help="Results directory")
    rn.add_argument("--repo", help="Filter instances by repo (e.g. django/django)")
    rn.add_argument("--max-instances", type=int, help="Limit number of instances")
    rn.add_argument("--local-csv", help="Path to local dataset CSV/JSONL (offline mode)")
    rn.add_argument("--prefix", default="agent-core", help="Model prefix in patches.json")
    rn.add_argument("--no-resume", action="store_true", help="Don't skip existing results")
    rn.add_argument("--cleanup", action="store_true", help="Remove work dirs after completion")
    rn.add_argument("--api-key", help="API key for the provider")
    rn.add_argument("--permission-mode", default="bypassPermissions",
                   choices=["bypassPermissions", "acceptEdits", "ask"])
    rn.add_argument("--repo-base-url", help="Git repo mirror base URL (e.g. https://gh-proxy.com/https://github.com)")
    rn.add_argument("--git-proxy", help="HTTP(S) proxy for git (e.g. http://127.0.0.1:7890)")
    rn.add_argument("--pre-cloned-dir", help="Directory of pre-cloned repos (owner__repo naming)")

    # ---- gather ----
    gth = sub.add_parser("gather", help="Collect patches from results directory")
    gth.add_argument("--results", default="./swe_bench_results", help="Results directory")
    gth.add_argument("--prefix", default="agent-core", help="Model prefix")
    gth.add_argument("--output", default="patches.json", help="Output JSON path")

    return parser


def _download(args: argparse.Namespace) -> None:
    """Download the dataset and save locally."""
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    instances = load_swebench_dataset(
        dataset_name=args.dataset,
        split=args.split,
    )

    if args.format == "csv":
        import csv

        output_path = output_dir / "swe_bench_pro.csv"
        if instances:
            fieldnames = ["instance_id", "repo", "base_commit", "version", "problem_statement", "hint_text"]
            with open(output_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for inst in instances:
                    row = inst.model_dump()
                    row["hint_text"] = row.get("hint_text") or ""
                    writer.writerow(row)
    else:
        import json

        output_path = output_dir / "swe_bench_pro.jsonl"
        with open(output_path, "w", encoding="utf-8") as f:
            for inst in instances:
                f.write(json.dumps(inst.model_dump(), ensure_ascii=False) + "\n")

    print(f"Downloaded {len(instances)} instances → {output_path}")


def _run(args: argparse.Namespace) -> None:
    """Run the benchmark."""
    from ...models import AgentConfig

    # Resolve API key
    api_keys: dict[str, str] = {}
    if args.api_key:
        # Parse provider from model string
        if "/" in args.model:
            provider = args.model.split("/")[0]
        else:
            provider = "anthropic"
        api_keys[provider] = args.api_key

    # Collect instance filter
    instance_ids: list[str] | None = None
    if args.single:
        instance_ids = [args.single]
    elif args.ids:
        ids_path = Path(args.ids)
        if ids_path.is_file():
            instance_ids = ids_path.read_text(encoding="utf-8").strip().splitlines()

    agent_config = AgentConfig(
        model=args.model,
        permission_mode=args.permission_mode,  # type: ignore[arg-type]
        max_turns=args.max_turns,
        api_keys=api_keys,
    )

    bench_config = SweBenchConfig(
        work_dir=args.work_dir,
        output_dir=args.output,
        instance_ids=instance_ids,
        repo_filter=args.repo,
        max_instances=args.max_instances,
        local_csv=args.local_csv,
        timeout_per_instance=args.timeout,
        model_prefix=args.prefix,
        resume=not args.no_resume,
        cleanup_work_dirs=args.cleanup,
        repo_base_url=args.repo_base_url,
        git_proxy=args.git_proxy,
        pre_cloned_dir=args.pre_cloned_dir,
    )

    runner = SweBenchRunner(agent_config, bench_config)
    results = asyncio.run(runner.run_all())

    # Print summary
    successes = sum(1 for r in results if r.success)
    patches = sum(1 for r in results if r.patch)
    errors = sum(1 for r in results if r.stop_reason == "error")

    print(f"\n{'='*60}")
    print(f"SWE-Bench Pro Run Complete")
    print(f"  Total:    {len(results)}")
    print(f"  Success:  {successes} (agent reported end_turn + produced patch)")
    print(f"  Patches:  {patches} (any non-empty diff)")
    print(f"  Errors:   {errors}")
    print(f"  Results:  {args.output}/")
    print(f"  Patches:  {args.output}/patches.json")
    print(f"{'='*60}")


def _gather(args: argparse.Namespace) -> None:
    """Collect individual result JSONs into a patches.json."""
    results_dir = Path(args.results)

    if not results_dir.is_dir():
        print(f"Error: results directory not found: {results_dir}", file=sys.stderr)
        sys.exit(1)

    results: list[dict] = []
    for json_file in sorted(results_dir.glob("*.json")):
        if json_file.name == "patches.json":
            continue
        try:
            import json
            data = json.loads(json_file.read_text(encoding="utf-8"))
            if "instance_id" in data:
                results.append(data)
        except Exception as exc:
            logger.warning("Skipping %s: %s", json_file, exc)

    if not results:
        print("No results found in", results_dir)
        sys.exit(1)

    export_patches_json(results, args.output, prefix=args.prefix)
    print(f"Gathered {len(results)} patches → {args.output}")


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "download":
        _download(args)
    elif args.command == "run":
        _run(args)
    elif args.command == "gather":
        _gather(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
