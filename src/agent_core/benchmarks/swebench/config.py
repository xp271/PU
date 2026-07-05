"""SWE-Bench configuration — benchmark-level options, separate from AgentConfig."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SweBenchConfig(BaseModel):
    """Benchmark-level configuration (Agent-level config is separate AgentConfig)."""

    # 工作目录
    work_dir: str = Field(
        default="./swe_bench_work",
        description="Root directory for cloned repos and per-instance workspaces",
    )

    # 数据集配置
    dataset_name: str = Field(
        default="ScaleAI/SWE-bench_Pro",
        description="HuggingFace dataset name",
    )
    dataset_split: str = Field(default="test", description="Dataset split to load")
    local_csv: str | None = Field(
        default=None,
        description="Path to local CSV/JSONL file (offline mode, overrides HF)",
    )

    # 过滤
    instance_ids: list[str] | None = Field(
        default=None,
        description="Specific instance IDs to run (None = all)",
    )
    repo_filter: str | None = Field(
        default=None,
        description="Only run instances from this repo (e.g. 'django/django')",
    )
    max_instances: int | None = Field(
        default=None,
        description="Limit total number of instances (for quick smoke tests)",
    )

    # 执行配置
    max_parallel: int = Field(
        default=1,
        ge=1,
        le=8,
        description="Maximum concurrent agent instances",
    )
    timeout_per_instance: int = Field(
        default=3600,
        ge=60,
        description="Timeout in seconds per instance (via asyncio.wait_for)",
    )

    # 输出配置
    output_dir: str = Field(
        default="./swe_bench_results",
        description="Directory for results (patch diffs, logs, patches.json)",
    )
    model_prefix: str = Field(
        default="agent-core",
        description="Model prefix in patches.json (SWE-Bench eval convention)",
    )

    # 断点续传
    resume: bool = Field(
        default=True,
        description="Skip instances that already have a result file",
    )

    # 清理策略
    cleanup_work_dirs: bool = Field(
        default=False,
        description="Remove per-instance work dirs after completion to save disk space",
    )

    # Git 仓库配置
    repo_base_url: str | None = Field(
        default=None,
        description=(
            "Override base URL for git clones. "
            "Default is https://github.com. Use a mirror when GitHub is unreachable, "
            "e.g. 'https://gh-proxy.com/https://github.com' or 'https://gitee.com/mirrors'"
        ),
    )
    git_proxy: str | None = Field(
        default=None,
        description=(
            "HTTP(S) proxy for git operations. "
            "e.g. 'http://127.0.0.1:7890' — sets http.proxy + https.proxy per clone"
        ),
    )
    pre_cloned_dir: str | None = Field(
        default=None,
        description=(
            "Directory containing pre-cloned repos. When set, the runner looks for "
            "existing clones here before cloning remotely. Repos should be named "
            "'owner__repo' (e.g. 'django__django')."
        ),
    )
