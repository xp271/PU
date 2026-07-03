"""Interactive CLI for manual Agent testing.

Usage:
    agent-core               # interactive REPL
    echo "prompt" | agent-core  # pipe mode (one-shot)

Configuration (first found wins):
    1. Path in $AGENT_CORE_CONFIG
    2. ./agent-core.config.json   (current working directory)
    3. ~/.agent-core/config.json  (user home)

Config file format (JSON):

    {
        "model": "anthropic/claude-sonnet-4-5",
        "api_keys": {
            "anthropic": "sk-ant-xxx",
            "openai": "sk-xxx"
        },
        "max_turns": 20,
        "permission_mode": "bypassPermissions"
    }

Missing api_keys fall back to environment variables
(ANTHROPIC_API_KEY / OPENAI_API_KEY).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from .agent import Agent
from .mcp.client import McpSessionManager
from .models import AgentConfig, ModelId, ModelProvider
from .permissions import PermissionOutcome
from .session import SessionState

# ---------------------------------------------------------------------------
# Config file resolution
# ---------------------------------------------------------------------------

_ENV_VAR_MAP: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "llamacpp": "LLAMACPP_API_KEY",
}


def _find_config_file() -> Path | None:
    """Locate the first existing config file in priority order.

    1. ``$AGENT_CORE_CONFIG`` environment variable (absolute path)
    2. ``./agent-core.config.json`` (current working directory)
    3. ``{package_root}/agent-core.config.json`` (resolved via ``__file__``;
       works for editable installs where the package lives inside the project)
    4. ``~/.agent-core/config.json`` (user home)
    """
    candidates: list[Path] = []

    override = os.environ.get("AGENT_CORE_CONFIG")
    if override:
        candidates.append(Path(override).expanduser().resolve())

    candidates.append(Path("agent-core.config.json").resolve())

    # Search relative to the package install location.
    # For editable installs (``pip install -e .``) this resolves to the
    # project root (e.g. ``C:\\PU``).  For regular installs it falls
    # through silently when no config is found.
    _pkg_root = Path(__file__).resolve().parent.parent.parent
    candidates.append(_pkg_root / "agent-core.config.json")

    candidates.append(Path.home() / ".agent-core" / "config.json")

    for p in candidates:
        if p.is_file():
            return p
    return None


# -- MCP config loading helpers ----------------------------------------------


_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_vars(obj: Any) -> Any:
    """Recursively expand ``${ENV_VAR}`` placeholders."""

    def _sub(m: Any) -> str:
        var = m.group(1)
        if var not in os.environ:
            raise KeyError(
                f"Environment variable '{var}' referenced in .mcp.json is not set"
            )
        return os.environ[var]

    if isinstance(obj, str):
        return _ENV_PLACEHOLDER.sub(_sub, obj)
    if isinstance(obj, list):
        return [_expand_env_vars(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    return obj


def _load_mcp_servers_raw() -> dict[str, Any]:
    """Load MCP servers from file chain: ~/.agent/mcp.json → .mcp.json."""
    servers: dict[str, Any] = {}

    # 1. User-level
    user_mcp = Path.home() / ".agent" / "mcp.json"
    if user_mcp.is_file():
        try:
            raw = json.loads(user_mcp.read_text(encoding="utf-8"))
            servers.update(raw.get("mcpServers", {}))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[config] failed to read {user_mcp}: {exc}", file=sys.stderr)

    # 2. Project-level (overrides same-named entries)
    project_mcp = Path(".mcp.json").resolve()
    if project_mcp.is_file():
        try:
            raw = json.loads(project_mcp.read_text(encoding="utf-8"))
            servers.update(raw.get("mcpServers", {}))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[config] failed to read {project_mcp}: {exc}", file=sys.stderr)

    # Expand ${ENV_VAR} placeholders
    try:
        expanded = _expand_env_vars(servers)
        if isinstance(expanded, dict):
            servers = expanded
    except KeyError as exc:
        print(f"[config] {exc}", file=sys.stderr)

    return servers


def _load_config() -> AgentConfig:
    """Build AgentConfig from config-file + env-var fallback."""
    config_path = _find_config_file()
    raw: dict[str, Any] = {}

    if config_path is not None:
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            print(f"[config] {config_path}")
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[config] failed to read {config_path}: {exc}", file=sys.stderr)

    # -- api_keys: file values take precedence over env vars --
    api_keys: dict[str, str] = raw.get("api_keys", {})
    for provider in ModelProvider:
        if provider.value in api_keys:
            continue
        env_var = _ENV_VAR_MAP.get(provider.value)
        if env_var:
            val = os.environ.get(env_var, "")
            if val:
                api_keys[provider.value] = val

    model: str = raw.get("model", "anthropic/claude-sonnet-4-5")
    max_turns: int = raw.get("max_turns", 20)
    permission_mode: str = raw.get("permission_mode", "ask")

    # -- MCP servers: code injection in config > .mcp.json files --
    mcp_servers: dict[str, Any] = {}
    try:
        mcp_servers = _load_mcp_servers_raw()
    except Exception as exc:
        print(f"[config] MCP config load failed: {exc}", file=sys.stderr)
    # Code injection overrides file-based config
    mcp_servers.update(raw.get("mcp_servers", {}))

    # -- Guard: refuse to start with no credentials at all --
    # Providers that don't require authentication (e.g. local llama.cpp server).
    _NO_AUTH_PROVIDERS = {"llamacpp"}
    if not any(api_keys.values()):
        provider = ModelId.parse(model).provider.value
        if provider not in _NO_AUTH_PROVIDERS and not api_keys.get(provider):
            env_var = _ENV_VAR_MAP.get(provider, f"{provider.upper()}_API_KEY")
            print(
                f"[config] ERROR: No API key found for provider '{provider}'.\n"
                f"  Searched: {config_path or 'no config file found'}\n"
                f"  Env var:  {env_var} (not set)\n"
                f"\n"
                f"  Fix: set the {env_var} environment variable, or create\n"
                f"  ~/.agent-core/config.json with an api_keys.{provider} entry.",
                file=sys.stderr,
            )
            sys.exit(1)

    return AgentConfig(
        model=model,
        api_keys=api_keys,
        max_turns=max_turns,
        permission_mode=permission_mode,  # type: ignore[arg-type]
        mcp_servers=mcp_servers,  # type: ignore[arg-type]
        allow_unknown_model=raw.get("allow_unknown_model", False),
        base_urls=raw.get("base_urls", {}),
        context_size=raw.get("context_size"),
    )


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
Commands:
  /model <provider/name>   Switch model (e.g. /model openai/gpt-4o)
  /key <provider> <key>    Set API key for a provider
  /clear                   Clear conversation history (keeps session permissions)
  /skills                  List loaded skills
  /sessions                List saved sessions (--resume <id> to restore)
  /logs                    Show conversation-log metrics for this session
  /help                    Show this help
  /quit                    Exit"""


def _handle_command(config: AgentConfig, text: str) -> bool:
    """Process a slash-command.  Returns True if the REPL should exit."""
    parts = text.split(maxsplit=2)
    cmd = parts[0].lower()

    if cmd == "/quit":
        return True

    if cmd == "/help":
        print(_HELP_TEXT)
        return False

    if cmd == "/model":
        if len(parts) < 2:
            print(f"Current model: {config.model}")
            return False
        config.model = parts[1]
        print(f"Model set to: {config.model}")
        return False

    if cmd == "/key":
        if len(parts) < 3:
            print("Usage: /key <provider> <api-key>")
            return False
        provider = parts[1]
        key = parts[2]
        config.api_keys[provider] = key
        print(f"API key updated for: {provider}")
        return False

    if cmd == "/skills":
        _list_skills(config)
        return False

    print(f"Unknown command: {cmd}. Type /help for available commands.")
    return False


async def _list_sessions_cmd(config: AgentConfig) -> None:
    """Print all persisted sessions for the current project."""
    from .persistence.session import list_sessions as _pers_list

    sessions = await _pers_list(config.workspace_root)
    if not sessions:
        print("(no saved sessions)")
        return

    for s in sessions:
        marker = " [unfinished]" if s.has_unfinished_turn else ""
        sid_short = s.session_id[:8]
        print(
            f"  {sid_short}...  {s.last_activity[:19]}  "
            f"({s.message_count} msgs)  {s.title}{marker}"
        )


async def _show_logs_cmd(config: AgentConfig, session_id: str) -> None:
    """Print conversation-log metrics for the current session (Item 12)."""
    if not config.logging.enabled:
        print("(conversation logging is disabled — set logging.enabled=True)")
        return

    from .logging.path import compute_project_hash, log_file_path
    from .logging.reader import aggregate_metrics, load_log

    ph = compute_project_hash(config.workspace_root)
    path = log_file_path(
        ph,
        session_id,
        rolling=config.logging.rolling,
        log_dir=config.logging.log_dir,
        trace_id=session_id,
    )
    records = await load_log(path)
    if not records:
        print(f"(no log records yet at {path})")
        return

    agg = aggregate_metrics(records)
    print(f"  log file       : {path}")
    print(f"  traces / turns : {agg['sessions']} / {agg['turns']}")
    print(
        f"  tokens         : {agg['total_tokens']} "
        f"(in {agg['input_tokens']} / out {agg['output_tokens']})"
    )
    print(f"  tool uses      : {agg['tool_uses']}  {agg['tool_frequency']}")
    print(f"  errors         : {agg['errors']}  (rate {agg['error_rate']:.2f})")
    if agg["latency_ms_avg"] is not None:
        print(
            f"  latency ms     : avg {agg['latency_ms_avg']:.0f}  "
            f"p50 {agg['latency_ms_p50']:.0f}  p95 {agg['latency_ms_p95']:.0f}"
        )


def _list_skills(config: AgentConfig) -> None:
    """Print all discoverable skills to stdout."""
    from .skills.loader import discover_skills

    search_dirs: list[str] = []
    if config.skills_dir:
        search_dirs.append(config.skills_dir)
    cwd = os.getcwd()
    search_dirs.append(os.path.join(cwd, ".agent", "skills"))
    search_dirs.append(os.path.join(os.path.expanduser("~"), ".agent", "skills"))

    all_skills: dict[str, Any] = {}
    for d in search_dirs:
        for name, sd in discover_skills(d).items():
            if name not in all_skills:
                all_skills[name] = sd

    if not all_skills:
        print("(no skills found)")
        print("Place SKILL.md files in .agent/skills/<name>/SKILL.md")
        return

    for name, sd in all_skills.items():
        mode = sd.execution_mode or "inline"
        bg = " [background]" if sd.background else ""
        print(f"  {name} ({mode}{bg}) — {sd.description or '(no description)'}")


async def _permission_callback(
    tool_name: str, tool_input: dict[str, Any]
) -> PermissionOutcome:
    """Ask the user via stdin whether to allow a tool call.

    ``y`` allow once · ``s`` allow for the session · ``n`` deny once ·
    ``r`` deny for the session.  Anything else (or empty) denies once.
    """
    summary = _summarise(tool_input)
    prompt_text = (
        f"\n[permission] Allow {tool_name}? {summary}\n"
        "  [y]=allow once  [s]=allow session  [n]=deny once  [r]=deny session: "
    )
    answer = (await asyncio.to_thread(input, prompt_text)).strip().lower()
    if answer.startswith("s"):
        return PermissionOutcome.ALLOW_SESSION
    if answer.startswith("r"):
        return PermissionOutcome.DENY_SESSION
    if answer.startswith("y"):
        return PermissionOutcome.ALLOW_ONCE
    return PermissionOutcome.DENY_ONCE


async def _repl(
    config: AgentConfig,
    *,
    resume_id: str | None = None,
    continue_last: bool = False,
) -> None:
    """Read-eval-print loop."""
    print()
    print(_HELP_TEXT)
    print()

    # One session for the whole REPL: carries conversation history and
    # session-remembered permission decisions across prompts.
    session = SessionState()

    # ---- Session resume (Phase B) ----
    if resume_id is not None or continue_last:
        from .persistence.session import list_sessions as _pers_list
        from .persistence.session import load_session as _pers_load

        if continue_last:
            sessions = await _pers_list(config.workspace_root)
            if sessions:
                resume_id = sessions[0].session_id

        if resume_id:
            loaded = await _pers_load(
                resume_id, workspace_root=config.workspace_root
            )
            if loaded is not None:
                session = loaded
                print(
                    f"[resumed session {resume_id[:8]}... "
                    f"({len(session.messages)} messages)]"
                )
                print(
                    "[note: session permission memory is not restored "
                    "across sessions — you may be re-prompted for tool approvals]"
                )
            else:
                print(
                    f"[session {resume_id[:8]}... not found, starting fresh]"
                )

    # ---- Begin persistence (Phase B) ----
    from .persistence.path import compute_project_hash, session_jsonl_path

    ph = compute_project_hash(config.workspace_root)
    tpath = session_jsonl_path(ph, session.session_id)
    await session.begin_persistence(tpath)

    try:
        while True:
            try:
                if sys.stdin.isatty():
                    prompt = input("agent-core> ").strip()
                else:
                    prompt = sys.stdin.readline()
                    if not prompt:
                        break
                    prompt = prompt.strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not prompt:
                continue

            if prompt.startswith("/"):
                cmd_parts = prompt.strip().split()
                if prompt.strip() == "/clear":
                    session.clear_history()
                    # Reset written_count too so disk doesn't diverge
                    session._written_count = len(session.messages)
                    print("[history cleared]")
                    continue
                if cmd_parts[0].lower() == "/sessions":
                    await _list_sessions_cmd(config)
                    continue
                if cmd_parts[0].lower() == "/logs":
                    await _show_logs_cmd(config, session.session_id)
                    continue
                if _handle_command(config, prompt):
                    break
                continue

            # -- Run agent with current prompt --
            agent = Agent(config, session=session)
            agent.on_permission_check(_permission_callback)
            stream = agent.stream(prompt)
            try:
                async for msg in stream:
                    if msg.type == "assistant_text":
                        print(msg.content, end="", flush=True)
                    elif msg.type == "tool_use":
                        print(f"\n[{msg.tool_name}] {_summarise(msg.tool_input)}")
                    elif msg.type == "tool_result":
                        if msg.is_error:
                            # 错误结果完整展示，方便定位问题
                            preview = _ellipsis(str(msg.content), 500)
                            print(f"  -> ERR: {preview}")
                        else:
                            preview = _ellipsis(str(msg.content), 120)
                            print(f"  -> OK: {preview}")
                    elif msg.type == "error":
                        print(f"\n[ERROR] {msg.message}")
                    elif msg.type == "compact":
                        if msg.status == "started":
                            print(
                                f"\n[compact] compressing {msg.rounds_compressed} round(s) "
                                f"(keeping {msg.rounds_preserved}), "
                                f"tokens ~{msg.tokens_before}…",
                                end="", flush=True,
                            )
                        elif msg.status == "completed":
                            pct = int((1 - msg.compression_ratio) * 100) if msg.compression_ratio else 0
                            print(
                                f"\n[compact] done: {msg.tokens_before} → {msg.tokens_after} tokens "
                                f"({pct}% reduced, {msg.retries_used} retries)",
                            )
                    elif msg.type == "done":
                        print(f"\n--- {msg.stop_reason} | {msg.total_tokens} tokens ---")
            except KeyboardInterrupt:
                print("\n[cancelled]")
            except Exception as exc:
                print(f"\n[exception] {type(exc).__name__}: {exc}")
            finally:
                # Close the stream (and its MCP sessions) in THIS task, even if the
                # loop was abandoned via exception — never at asyncio.run() shutdown
                # in a different task (anyio cancel-scope requirement).
                await stream.aclose()
                # §Item 12: drain this prompt's conversation-log sink (no-op
                # when logging is disabled).
                await agent.aclose()
    finally:
        await session.end_persistence()


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _summarise(obj: object) -> str:
    """Compact representation of a tool input dict."""
    if isinstance(obj, dict):
        parts = []
        for k, v in obj.items():
            parts.append(f"{k}={_ellipsis(str(v), 40)}")
        return "{" + ", ".join(parts) + "}"
    return str(obj)


def _ellipsis(text: str, max_len: int) -> str:
    """Truncate *text* to *max_len* with an ellipsis if needed."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


# ---------------------------------------------------------------------------
# MCP startup preflight
# ---------------------------------------------------------------------------

async def _preflight_mcp(config: AgentConfig) -> None:
    """Connect to every configured MCP server once, at startup.

    This triggers any ``npx -y`` package download and verifies the
    handshake *before* the REPL accepts input — so the first real query
    isn't blocked on a cold download, and an unreachable/broken server
    surfaces immediately instead of mid-query.

    Sessions are closed again right away: each ``query()`` opens its own
    (the manager is query-scoped, and connect/aclose must run in the same
    task).  The win here is warming the npx cache + reporting status.
    ``connect()`` and ``aclose()`` run in this single ``asyncio.run`` task,
    satisfying the anyio same-task requirement.
    """
    if not config.mcp_servers:
        return

    print(f"[mcp] checking {len(config.mcp_servers)} server(s)…")
    mcp = McpSessionManager()
    try:
        for name, server_config in config.mcp_servers.items():
            try:
                state = await mcp.connect(name, server_config)
            except Exception as exc:  # defensive: never block startup
                print(f"[mcp]   x {name}: {exc}", file=sys.stderr)
                continue
            if state.connection_error is None:
                print(f"[mcp]   ok {name}: {len(state.tools)} tool(s)")
            else:
                print(
                    f"[mcp]   x {name}: {state.connection_error}",
                    file=sys.stderr,
                )
    finally:
        await mcp.aclose()


# ---------------------------------------------------------------------------
# LLM server preflight (llama.cpp auto-start)
# ---------------------------------------------------------------------------

_LLAMACPP_SERVER_EXE = Path(__file__).resolve().parent.parent.parent / "llama.cpp" / "llama-server.exe"
_LLAMACPP_MODELS_DIR = Path(__file__).resolve().parent.parent.parent / "llama.cpp" / "models"
_LLAMACPP_HEALTH_URL = "http://127.0.0.1:8080/v1/models"


async def _preflight_llamacpp(config: AgentConfig) -> None:
    """Ensure the llama.cpp server is running before the REPL starts.

    1. Resolve the model name from config → GGUF file path.
    2. Health-check the configured base_url.
    3. If unreachable, spawn ``llama-server.exe`` with ``-m`` (single-model mode).
    4. Poll until ready (timeout 120 s — large models take time to load).
    """
    try:
        model_id = config.model_id
    except ValueError:
        return
    if model_id.provider != ModelProvider.LLAMACPP:
        return

    import asyncio

    import httpx

    base_url = config.get_base_url(ModelProvider.LLAMACPP) or "http://127.0.0.1:8080/v1"
    health_url = base_url.rstrip("/") + "/models"

    # 1. Quick check — is it already running?
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0)) as client:
            r = await client.get(health_url)
            if r.status_code == 200:
                models = r.json().get("data", [])
                model_ids = [m.get("id", "?") for m in models]
                print(f"[llamacpp] server already running — {len(models)} model(s): {', '.join(model_ids)}")
                return
    except Exception:
        pass

    # 2. Resolve model file from config
    model_name = model_id.name
    model_path = _LLAMACPP_MODELS_DIR / f"{model_name}.gguf"
    if not model_path.is_file():
        # Try glob match in case filename differs slightly
        candidates = list(_LLAMACPP_MODELS_DIR.glob(f"{model_name}*"))
        if candidates:
            model_path = candidates[0]
        else:
            print(
                f"[llamacpp] model file not found: {model_path}\n"
                f"  Place the GGUF file in {_LLAMACPP_MODELS_DIR}/",
                file=sys.stderr,
            )
            return

    # 3. Not running — start in single-model mode
    if not _LLAMACPP_SERVER_EXE.is_file():
        print(
            f"[llamacpp] server not running and {_LLAMACPP_SERVER_EXE} not found.",
            file=sys.stderr,
        )
        return

    ctx_size = config.context_size or 8192
    print(f"[llamacpp] starting server with model: {model_path.name} (ctx={ctx_size})")

    try:
        proc = await asyncio.create_subprocess_exec(
            str(_LLAMACPP_SERVER_EXE),
            "-m", str(model_path),
            "--host", "127.0.0.1",
            "--port", "8080",
            "--ctx-size", str(ctx_size),
            "--cache-type-k", "q8_0",
            "--cache-type-v", "q8_0",
            "--tools", "all",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        print(f"[llamacpp] failed to start server: {exc}", file=sys.stderr)
        return

    # 4. Poll until ready (large models may take >60 s to load)
    print("[llamacpp] loading model…", end="", flush=True)
    deadline = asyncio.get_event_loop().time() + 120.0
    while asyncio.get_event_loop().time() < deadline:
        if proc.returncode is not None:
            print(f"\n[llamacpp] server exited unexpectedly (code={proc.returncode})", file=sys.stderr)
            return
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.0)) as client:
                r = await client.get(health_url)
                if r.status_code == 200:
                    models = r.json().get("data", [])
                    model_ids = [m.get("id", "?") for m in models]
                    print(f"\n[llamacpp] ready — {len(models)} model(s): {', '.join(model_ids)}")
                    return
        except Exception:
            pass
        await asyncio.sleep(1.0)
        print(".", end="", flush=True)

    print(f"\n[llamacpp] server did not become ready within 120 s (pid={proc.pid})", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _main(
    config: AgentConfig,
    *,
    resume_id: str | None = None,
    continue_last: bool = False,
) -> None:
    """Async entry: preflight LLM server, preflight MCP servers, then run the REPL."""
    await _preflight_llamacpp(config)
    await _preflight_mcp(config)
    await _repl(config, resume_id=resume_id, continue_last=continue_last)


def main() -> None:
    """CLI entry point (agent-core console script)."""
    config = _load_config()

    # Parse command-line arguments (simple, no argparse dependency)
    resume_id: str | None = None
    continue_last: bool = False
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--resume" and i + 1 < len(args):
            i += 1
            resume_id = args[i]
        elif arg == "--continue":
            continue_last = True
        i += 1

    asyncio.run(_main(config, resume_id=resume_id, continue_last=continue_last))


if __name__ == "__main__":
    main()
