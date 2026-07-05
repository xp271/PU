"""SWE-Bench Pro Runner — core execution engine.

Walks through SWE-Bench instances, launches an Agent for each one, extracts
patches from modified working trees, and exports results.

Usage::

    import asyncio
    from agent_core.benchmarks.swebench.runner import SweBenchRunner
    from agent_core.benchmarks.swebench.config import SweBenchConfig
    from agent_core.models import AgentConfig

    agent_config = AgentConfig(
        model="anthropic/claude-sonnet-4-5",
        permission_mode="bypassPermissions",
        max_turns=50,
    )
    bench_config = SweBenchConfig(work_dir="./swe_bench_work")
    runner = SweBenchRunner(agent_config, bench_config)
    results = asyncio.run(runner.run_all())
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...agent import Agent
from ...logging.config import LoggingConfig
from ...models import AgentConfig, AgentResult, StopReason
from .config import SweBenchConfig
from .dataset import SweBenchInstance, load_swebench_dataset
from .patch import export_patches_json, extract_git_diff
from .prompt import build_system_prompt

logger = logging.getLogger("agent_core.benchmarks.swebench.runner")

# GitHub base URL for cloning. SWE-Bench repos are on GitHub.
_GITHUB_BASE = "https://github.com"


@dataclass
class SweBenchResult:
    """The outcome of running one SWE-Bench instance."""

    instance_id: str
    repo: str
    base_commit: str
    patch: str = ""
    stop_reason: str = ""
    error_detail: str | None = None
    total_tokens: int = 0
    tool_uses: int = 0
    elapsed_seconds: float = 0.0
    work_dir: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.stop_reason == "end_turn" and bool(self.patch)


class SweBenchRunner:
    """Sweep through SWE-Bench instances, running an Agent for each one.

    Each instance gets:
    1. A cloned repo checkout at its ``base_commit``
    2. A fresh Agent instance with workspace_root pointed to the repo
    3. A SWE-Bench system prompt injected via ``system`` parameter
    4. A timeout (via ``asyncio.wait_for``)
    5. Git-diff patch extraction against the base_commit
    """

    def __init__(
        self,
        agent_config: AgentConfig,
        bench_config: SweBenchConfig,
    ) -> None:
        self._agent_config = agent_config
        self._bench_config = bench_config
        self._work_dir = Path(bench_config.work_dir).resolve()
        self._output_dir = Path(bench_config.output_dir).resolve()
        self._work_dir.mkdir(parents=True, exist_ok=True)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_all(
        self, *, instances: list[SweBenchInstance] | None = None,
    ) -> list[SweBenchResult]:
        """Run every instance and collect results.

        Args:
            instances: Pre-loaded instances. When None, loads from dataset
                       using bench_config settings.
        """
        if instances is None:
            instances = load_swebench_dataset(
                dataset_name=self._bench_config.dataset_name,
                split=self._bench_config.dataset_split,
                local_path=self._bench_config.local_csv,
                instance_ids=self._bench_config.instance_ids,
                repo_filter=self._bench_config.repo_filter,
                max_instances=self._bench_config.max_instances,
            )

        if not instances:
            logger.warning("No instances to run")
            return []

        results: list[SweBenchResult] = []

        # Serial execution for simplicity; parallel execution deferred to future.
        for idx, instance in enumerate(instances):
            instance_id = instance.instance_id

            # Resume: skip if output already exists
            if self._bench_config.resume and self._has_existing_result(instance_id):
                logger.info("[%d/%d] %s — skipping (already done)", idx + 1, len(instances), instance_id)
                results.append(self._load_existing_result(instance_id))
                continue

            logger.info("[%d/%d] %s — starting", idx + 1, len(instances), instance_id)
            result = await self.run_single(instance)
            results.append(result)

            # Persist result immediately (crash-safe)
            self._save_result(result)

        # Export patches.json
        patches_path = str(self._output_dir / "patches.json")
        export_patches_json(
            [r.__dict__ for r in results],
            patches_path,
            prefix=self._bench_config.model_prefix,
        )

        # Summary
        successes = sum(1 for r in results if r.success)
        logger.info(
            "Done: %d/%d succeeded, %d total instances",
            successes,
            len(results),
            len(results),
        )

        return results

    async def run_single(self, instance: SweBenchInstance) -> SweBenchResult:
        """Run a single SWE-Bench instance and return the result."""
        start_time = time.monotonic()

        # 1. Prepare workspace
        work_dir = self._instance_work_dir(instance.instance_id)
        try:
            await self._prepare_workspace(instance, work_dir)
        except Exception as exc:
            logger.error("Workspace prep failed for %s: %s", instance.instance_id, exc)
            return SweBenchResult(
                instance_id=instance.instance_id,
                repo=instance.repo,
                base_commit=instance.base_commit,
                stop_reason="error",
                error_detail=f"Workspace prep failed: {exc}",
                elapsed_seconds=time.monotonic() - start_time,
                work_dir=str(work_dir),
            )

        # 2. Build config for this instance
        instance_config = self._build_instance_config(instance, work_dir)

        # 3. Build system prompt
        system_prompt = build_system_prompt(
            repo=instance.repo,
            base_commit=instance.base_commit,
            problem_statement=instance.problem_statement,
            hint_text=instance.hint_text,
        )

        # 4. Run Agent
        agent = Agent(instance_config)
        try:
            agent_result = await asyncio.wait_for(
                agent.run(instance.problem_statement, system=system_prompt),
                timeout=self._bench_config.timeout_per_instance,
            )
        except asyncio.TimeoutError:
            logger.warning("%s — timeout (%ds)", instance.instance_id, self._bench_config.timeout_per_instance)
            agent_result = AgentResult(
                content="",
                total_tokens=0,
                tool_uses=0,
                stop_reason="end_turn",  # we override below
                error_detail="timeout",
            )
            # After timeout the agent coroutine is cancelled; close the same
            # Agent instance so its internal resources (MCP sessions, anyio
            # cancel scopes) are cleaned up properly.
            try:
                await agent.aclose()
            except Exception:
                pass
        else:
            await agent.aclose()

        # 5. Extract patch
        patch = ""
        try:
            patch = extract_git_diff(str(work_dir), instance.base_commit)
        except Exception as exc:
            logger.warning("Patch extraction failed for %s: %s", instance.instance_id, exc)

        # 6. Determine stop_reason
        stop_reason = _classify_stop_reason(agent_result, patch)

        elapsed = time.monotonic() - start_time

        # 7. Cleanup
        if self._bench_config.cleanup_work_dirs:
            await self._cleanup_workspace(work_dir)

        return SweBenchResult(
            instance_id=instance.instance_id,
            repo=instance.repo,
            base_commit=instance.base_commit,
            patch=patch,
            stop_reason=stop_reason,
            error_detail=agent_result.error_detail,
            total_tokens=agent_result.total_tokens,
            tool_uses=agent_result.tool_uses,
            elapsed_seconds=elapsed,
            work_dir=str(work_dir),
        )

    # ------------------------------------------------------------------
    # Workspace management
    # ------------------------------------------------------------------

    def _instance_work_dir(self, instance_id: str) -> Path:
        safe_id = instance_id.replace("/", "_").replace("\\", "_")
        return self._work_dir / safe_id

    async def _prepare_workspace(self, instance: SweBenchInstance, work_dir: Path) -> None:
        """Ensure the repo is cloned at the correct base_commit.

        Priority:
        1. Pre-cloned directory (if configured)
        2. Existing clone in work_dir (reuse)
        3. Fresh clone from remote (or mirror)
        """
        # 0. Check pre-cloned dir
        pre_cloned = self._bench_config.pre_cloned_dir
        if pre_cloned:
            pre_path = Path(pre_cloned) / instance.repo.replace("/", "__")
            if pre_path.is_dir() and (pre_path / ".git").is_dir():
                logger.debug("Copying pre-cloned repo: %s", pre_path)
                import shutil
                await asyncio.to_thread(shutil.copytree, str(pre_path), str(work_dir), symlinks=True)
                await self._run_git(work_dir, "fetch", "--all", "--tags")
                await self._run_git(work_dir, "checkout", "--detach", instance.base_commit)
                return

        # 1. Build clone URL (with mirror support)
        repo_url = self._build_repo_url(instance.repo)

        # 2. Reuse existing clone
        if work_dir.is_dir() and (work_dir / ".git").is_dir():
            logger.debug("Reusing existing clone: %s", work_dir)
            await self._run_git(work_dir, "remote", "set-url", "origin", repo_url)
            await self._run_git(work_dir, "fetch", "origin")
            await self._run_git(work_dir, "checkout", "--detach", instance.base_commit)
            return

        # 3. Fresh clone
        logger.debug("Cloning %s → %s", repo_url, work_dir)
        work_dir.parent.mkdir(parents=True, exist_ok=True)
        await self._run_git(
            work_dir.parent,
            "clone",
            "--depth=1",
            repo_url,
            str(work_dir.name),
        )
        await self._run_git(work_dir, "fetch", "--unshallow", "--tags")
        await self._run_git(work_dir, "checkout", "--detach", instance.base_commit)

    def _build_repo_url(self, repo: str) -> str:
        """Resolve the git clone URL for a repo, applying mirror config."""
        base = self._bench_config.repo_base_url
        if base is not None:
            # base可以是完整镜像地址，如 https://gh-proxy.com/https://github.com
            return f"{base}/{repo}.git"
        return f"{_GITHUB_BASE}/{repo}.git"

    async def _run_git(self, cwd: Path, *args: str) -> None:
        """Run a git command in *cwd*, with optional proxy, raising on failure."""
        env = os.environ.copy()
        proxy = self._bench_config.git_proxy
        if proxy is not None:
            env["http_proxy"] = proxy
            env["https_proxy"] = proxy
            env["HTTP_PROXY"] = proxy
            env["HTTPS_PROXY"] = proxy

        # Prepend longpaths flag on Windows so repos with deeply-nested paths
        # (ansible, etc.) can be cloned without "Filename too long" errors.
        git_args = list(args)
        if sys.platform == "win32" and args and args[0] in ("clone", "fetch", "checkout"):
            git_args = ["-c", "core.longpaths=true"] + git_args

        await self._run_subprocess_with_env("git", *git_args, cwd=str(cwd), env=env)

    async def _run_subprocess_with_env(
        self, cmd: str, *args: str, cwd: str, env: dict | None = None
    ) -> None:
        """Run a subprocess with optional env, raising on failure."""
        proc = await asyncio.create_subprocess_exec(
            cmd, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error_msg = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"{cmd} {' '.join(args)} failed (rc={proc.returncode}): {error_msg}"
            )

    async def _cleanup_workspace(self, work_dir: Path) -> None:
        """Remove the work directory."""
        try:
            import shutil
            await asyncio.to_thread(shutil.rmtree, str(work_dir), ignore_errors=True)
            logger.debug("Cleaned up %s", work_dir)
        except Exception as exc:
            logger.warning("Failed to clean up %s: %s", work_dir, exc)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def _build_instance_config(self, instance: SweBenchInstance, work_dir: Path) -> AgentConfig:
        """Create a per-instance AgentConfig clone with workspace_root + logging."""
        cfg = self._agent_config.model_copy()
        cfg.workspace_root = str(work_dir)
        # Enable conversation logging so the full agent trace is persisted.
        # Logs go into <output_dir>/logs/<instance_id>.jsonl for later debugging.
        log_dir = str(self._output_dir / "logs")
        cfg.logging = LoggingConfig(
            enabled=True,
            log_dir=log_dir,
            rolling="per_session",
            record_content=True,
            record_tool_io=True,
        )
        return cfg

    # ------------------------------------------------------------------
    # Persistence / resume
    # ------------------------------------------------------------------

    def _result_path(self, instance_id: str) -> Path:
        safe_id = instance_id.replace("/", "_").replace("\\", "_")
        return self._output_dir / f"{safe_id}.json"

    def _has_existing_result(self, instance_id: str) -> bool:
        return self._result_path(instance_id).is_file()

    def _save_result(self, result: SweBenchResult) -> None:
        path = self._result_path(result.instance_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result.__dict__, f, indent=2, ensure_ascii=False, default=str)
        logger.debug("Saved result: %s", path)

    def _load_existing_result(self, instance_id: str) -> SweBenchResult:
        path = self._result_path(instance_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        return SweBenchResult(**data)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _classify_stop_reason(agent_result: AgentResult, patch: str) -> str:
    """Normalize the agent stop reason for SWE-Bench result tracking.

    The plan defines 4 official stop_reason values plus our timeout override.
    """
    if agent_result.error_detail == "timeout":
        return "timeout"

    reason = agent_result.stop_reason

    # end_turn with a non-empty patch = successful completion
    if reason == "end_turn":
        if not patch.strip():
            return "end_turn_no_patch"
        return "end_turn"

    # max_turns with partial work
    if reason == "max_turns":
        return "max_turns"

    # error during LLM call or runtime
    if reason == "error":
        return "error"

    # permission_denied
    if reason == "permission_denied":
        return "permission_denied"

    return reason
