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
import os
import sys
from pathlib import Path
from typing import Any

from ..agent import Agent
from ..mcp.client import McpSessionManager
from ..models import AgentConfig, ModelId, ModelProvider
from ..permissions import PermissionOutcome
from ..session import SessionState
from ._config import _find_config_file, _load_config

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
    from ..persistence.session import list_sessions as _pers_list

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

    from ..logging.path import compute_project_hash, log_file_path
    from ..logging.reader import aggregate_metrics, load_log

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
    from ..skills.loader import discover_skills

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

    session = SessionState()

    if resume_id is not None or continue_last:
        from ..persistence.session import list_sessions as _pers_list
        from ..persistence.session import load_session as _pers_load

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

    from ..persistence.path import compute_project_hash, session_jsonl_path

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
                await stream.aclose()
                await agent.aclose()
    finally:
        await session.end_persistence()


# ---------------------------------------------------------------------------
# MCP startup preflight
# ---------------------------------------------------------------------------

async def _preflight_mcp(config: AgentConfig) -> None:
    """Connect to every configured MCP server once, at startup."""
    if not config.mcp_servers:
        return

    print(f"[mcp] checking {len(config.mcp_servers)} server(s)…")
    mcp = McpSessionManager()
    try:
        for name, server_config in config.mcp_servers.items():
            try:
                state = await mcp.connect(name, server_config)
            except Exception as exc:
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

_LLAMACPP_SERVER_EXE = Path(__file__).resolve().parent.parent.parent.parent / "llama.cpp" / "llama-server.exe"
_LLAMACPP_MODELS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "llama.cpp" / "models"


async def _preflight_llamacpp(
    config: AgentConfig,
) -> "Callable[[], Awaitable[None]] | None":
    """Ensure the llama.cpp server is running before the REPL starts."""
    try:
        model_id = config.model_id
    except ValueError:
        return None
    if model_id.provider != ModelProvider.LLAMACPP:
        return None

    import asyncio

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", 8080), timeout=3.0,
        )
        writer.write(
            b"GET /v1/models HTTP/1.1\r\n"
            b"Host: 127.0.0.1:8080\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=3.0)
        writer.close()
        if b"200" in response[:256]:
            print(f"[llamacpp] server already running on port 8080")
            return None
    except Exception:
        pass

    model_name = model_id.name
    model_path = _LLAMACPP_MODELS_DIR / f"{model_name}.gguf"
    if not model_path.is_file():
        candidates = list(_LLAMACPP_MODELS_DIR.glob(f"{model_name}*"))
        if candidates:
            model_path = candidates[0]
        else:
            print(
                f"[llamacpp] model file not found: {model_path}\n"
                f"  Place the GGUF file in {_LLAMACPP_MODELS_DIR}/",
                file=sys.stderr,
            )
            return None

    if not _LLAMACPP_SERVER_EXE.is_file():
        print(
            f"[llamacpp] server not running and {_LLAMACPP_SERVER_EXE} not found.",
            file=sys.stderr,
        )
        return None

    ctx_size = config.context_size or 8192
    llc = config.llamacpp
    print(
        f"[llamacpp] starting server with model: {model_path.name} "
        f"(ctx={ctx_size}, ngl={llc.gpu_layers}, "
        f"kv={llc.cache_type_k}/{llc.cache_type_v})"
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            str(_LLAMACPP_SERVER_EXE),
            "-m", str(model_path),
            "--host", "127.0.0.1",
            "--port", "8080",
            "--ctx-size", str(ctx_size),
            "--cache-type-k", llc.cache_type_k,
            "--cache-type-v", llc.cache_type_v,
            "-ngl", str(llc.gpu_layers),
            "--tools", "all",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        print(f"[llamacpp] failed to start server: {exc}", file=sys.stderr)
        return None

    _stderr_chunks: list[bytes] = []

    async def _drain_stderr() -> None:
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            _stderr_chunks.append(chunk)

    drain_task = asyncio.ensure_future(_drain_stderr())

    async def _shutdown() -> None:
        if not drain_task.done():
            drain_task.cancel()
            try:
                await drain_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass

    print("[llamacpp] loading model…", end="", flush=True)
    deadline = asyncio.get_event_loop().time() + 120.0
    while asyncio.get_event_loop().time() < deadline:
        if proc.returncode is not None:
            await drain_task
            stderr_text = b"".join(_stderr_chunks).decode("utf-8", errors="replace")[-2000:]
            print(f"\n[llamacpp] server exited unexpectedly (code={proc.returncode})", file=sys.stderr)
            if stderr_text:
                print(f"[llamacpp] stderr tail:\n{stderr_text}", file=sys.stderr)
            await _shutdown()
            return None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", 8080), timeout=3.0,
            )
            writer.write(
                b"GET /v1/models HTTP/1.1\r\n"
                b"Host: 127.0.0.1:8080\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(4096), timeout=3.0)
            writer.close()
            await writer.wait_closed()
            if b"200" in response[:256]:
                print(f"\n[llamacpp] ready (port 8080 responding)")
                drain_task.cancel()
                try:
                    await drain_task
                except asyncio.CancelledError:
                    pass
                return _shutdown
        except Exception:
            pass
        await asyncio.sleep(1.0)
        print(".", end="", flush=True)

    drain_task.cancel()
    try:
        await drain_task
    except asyncio.CancelledError:
        pass
    stderr_text = b"".join(_stderr_chunks).decode("utf-8", errors="replace")[-2000:]
    print(f"\n[llamacpp] server did not become ready within 120 s (pid={proc.pid})", file=sys.stderr)
    if stderr_text:
        print(f"[llamacpp] stderr tail:\n{stderr_text}", file=sys.stderr)
    await _shutdown()
    return None


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
    shutdown_llamacpp = await _preflight_llamacpp(config)
    await _preflight_mcp(config)
    try:
        await _repl(config, resume_id=resume_id, continue_last=continue_last)
    finally:
        if shutdown_llamacpp is not None:
            await shutdown_llamacpp()


def main() -> None:
    """CLI entry point (agent-core console script)."""
    config = _load_config()

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
