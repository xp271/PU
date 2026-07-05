"""SWE-Bench Pro benchmark integration.

Usage::

    import asyncio
    from agent_core.benchmarks.swebench.runner import SweBenchRunner
    from agent_core.benchmarks.swebench.config import SweBenchConfig
    from agent_core.models import AgentConfig

    config = AgentConfig(
        model="anthropic/claude-sonnet-4-5",
        permission_mode="bypassPermissions",
        max_turns=50,
    )
    bench_config = SweBenchConfig(work_dir="./swe_bench_work")
    runner = SweBenchRunner(config, bench_config)
    results = asyncio.run(runner.run_all())
"""
