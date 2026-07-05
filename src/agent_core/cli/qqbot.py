"""QQ 机器人 CLI 入口 — ``agent-core-qqbot`` 命令。

用法:
    agent-core-qqbot start                # 从配置文件和/或环境变量启动
    agent-core-qqbot start --config ./my.json  # 指定配置文件
    agent-core-qqbot init                 # 生成配置模板

配置加载顺序（后面的覆盖前面的）：
    1. 配置文件（--config 指定，或自动查找）
    2. 环境变量 QQ_BOT_APP_ID / QQ_BOT_APP_SECRET
    3. 命令行参数 --model / --system-prompt

纯环境变量模式（零文件部署）：
    set QQ_BOT_APP_ID=xxx
    set QQ_BOT_APP_SECRET=xxx
    set DEEPSEEK_API_KEY=sk-xxx
    agent-core-qqbot start
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..channels.qq_bot.types import QQBotConfig
from ._config import _find_config_file, _load_config

# ---------------------------------------------------------------------------
# 配置模板
# ---------------------------------------------------------------------------

_CONFIG_TEMPLATE = """{
    "model": "deepseek/deepseek-v4-pro",
    "api_keys": {
        "deepseek": "sk-your-api-key-here",
        "anthropic": "",
        "openai": ""
    },
    "max_turns": 20,
    "qq_bot": {
        "app_id": "你的AppID",
        "app_secret": "你的AppSecret",
        "system_prompt": "你是一个有用的AI助手，请用中文回答。",
        "tools": ["*"],
        "max_turns": 20,
        "permission_mode": "bypassPermissions"
    }
}
"""

# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------


def _load_qqbot_config(config_path: str | None, cli_overrides: dict[str, str]) -> QQBotConfig:
    """加载 QQ 机器人配置。

    优先级：配置文件 < 环境变量 < CLI 参数。
    如果环境变量已提供 app_id 和 app_secret，配置文件不是必需的。
    """
    raw: dict[str, Any] = {}

    # 1. 读配置文件
    path = Path(config_path) if config_path else _find_config_file()
    if path is not None and path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            print(f"[qqbot] config: {path}")
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[qqbot] failed to read config {path}: {exc}", file=sys.stderr)

    qqbot_raw: dict[str, Any] = raw.get("qq_bot", {})

    # 2. 环境变量覆盖
    for key in ("app_id", "app_secret"):
        env_val = os.environ.get(f"QQ_BOT_{key.upper()}")
        if env_val:
            qqbot_raw[key] = env_val

    # 3. CLI 参数覆盖
    if cli_overrides.get("model"):
        qqbot_raw["model"] = cli_overrides["model"]
    if cli_overrides.get("system_prompt"):
        qqbot_raw["system_prompt"] = cli_overrides["system_prompt"]

    # 4. 如果仍然没有 app_id/app_secret，报错
    if not qqbot_raw.get("app_id") or not qqbot_raw.get("app_secret"):
        print(
            "[qqbot] Missing app_id or app_secret.\n"
            "  Option A: create agent-core.config.json with qq_bot section\n"
            "  Option B: set env vars QQ_BOT_APP_ID and QQ_BOT_APP_SECRET\n"
            "  Run 'agent-core-qqbot init' to generate a config template.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        return QQBotConfig(**qqbot_raw)
    except ValidationError as exc:
        print(f"[qqbot] Invalid config: {exc}", file=sys.stderr)
        sys.exit(1)


def _load_agent_config(config_path: str | None) -> Any:
    """加载 Agent 配置。

    优先用 --config 指定的路径，否则自动查找。
    仍然需要 LLM API key，可以从配置文件或环境变量获取。
    """
    if config_path:
        path = Path(config_path)
        if not path.is_file():
            print(f"[qqbot] Config file not found: {config_path}", file=sys.stderr)
            sys.exit(1)
        # Pass path directly — no env-var hack needed since _load_config()
        # accepts an optional config_path_override parameter.
        return _load_config(config_path_override=str(path.resolve()))

    try:
        return _load_config()
    except SystemExit:
        print(
            "[qqbot] Failed to load agent config. Make sure api_keys are set "
            "in config file or via environment variables (e.g. DEEPSEEK_API_KEY).",
            file=sys.stderr,
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------


def _cmd_init(config_path: str | None) -> None:
    """生成配置模板。"""
    target = Path(config_path) if config_path else Path("agent-core.config.json")
    if target.exists():
        print(f"[qqbot] {target} already exists. Remove it first or use --config to specify a different path.")
        sys.exit(1)
    target.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
    print(f"[qqbot] Config template written to {target}")
    print("  Edit it to set your app_id, app_secret, and API key, then run:")
    print(f"  agent-core-qqbot start --config {target}")


async def _cmd_start(config_path: str | None, cli_overrides: dict[str, str]) -> None:
    """启动 QQ 机器人。"""
    config = _load_qqbot_config(config_path, cli_overrides)

    # 日志：同时输出到控制台和项目目录下的 logs/qqbot.log
    _setup_logging()

    logger = logging.getLogger("agent_core.qq_bot")

    base = _load_agent_config(config_path)
    logger.info(f"base agent config: model={base.model}")

    from ..channels.qq_bot.channel import QQBotChannel

    channel = QQBotChannel(config)
    logger.info(f"starting QQ bot (app_id={config.app_id[:8]}...)")

    try:
        await channel.start()
    except KeyboardInterrupt:
        logger.info("received KeyboardInterrupt")
    except Exception:
        logger.exception("server crashed")
        sys.exit(1)


def _setup_logging() -> None:
    """配置日志：控制台 + 项目根目录 logs/qqbot.log。"""
    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    console.setLevel(logging.INFO)

    # 项目根目录下的日志文件（自动创建 logs 目录）
    project_root = Path(__file__).resolve().parent.parent.parent.parent
    log_dir = project_root / "logs"
    log_dir.mkdir(exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "qqbot.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.DEBUG)

    # 根 logger
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(file_handler)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> tuple[str | None, str | None, str | None, str | None]:
    """解析命令行参数。返回 (command, config_path, model, system_prompt)。"""
    command: str | None = None
    config_path: str | None = None
    model: str | None = None
    system_prompt: str | None = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("start", "init"):
            command = arg
        elif arg == "--config" and i + 1 < len(argv):
            i += 1
            config_path = argv[i]
        elif arg == "--model" and i + 1 < len(argv):
            i += 1
            model = argv[i]
        elif arg == "--system-prompt" and i + 1 < len(argv):
            i += 1
            system_prompt = argv[i]
        elif arg in ("-h", "--help"):
            command = "help"
        elif command is None:
            command = arg
        i += 1

    return command, config_path, model, system_prompt


def main() -> None:
    """CLI entry point (agent-core-qqbot console script)."""
    command, config_path, model, system_prompt = _parse_args(sys.argv[1:])

    if command == "init":
        _cmd_init(config_path)
    elif command == "start":
        cli_overrides = {}
        if model:
            cli_overrides["model"] = model
        if system_prompt:
            cli_overrides["system_prompt"] = system_prompt
        asyncio.run(_cmd_start(config_path, cli_overrides))
    else:
        print(
            "Usage:\n"
            "  agent-core-qqbot start [--config <path>] [--model <name>] [--system-prompt <text>]\n"
            "  agent-core-qqbot init  [--config <path>]\n"
            "\n"
            "Environment variables:\n"
            "  QQ_BOT_APP_ID        — Bot AppID (required)\n"
            "  QQ_BOT_APP_SECRET    — Bot AppSecret (required)\n"
            "  DEEPSEEK_API_KEY     — LLM API key (or ANTHROPIC_API_KEY / OPENAI_API_KEY)\n"
            "\n"
            "Examples:\n"
            "  agent-core-qqbot start                              # auto-find config\n"
            "  agent-core-qqbot start --config ./mybot.json        # explicit config\n"
            "  agent-core-qqbot init                               # generate template",
            file=sys.stderr,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
