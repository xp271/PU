"""
llama.cpp local-model streaming client.

Talks to a locally running ``llama-server.exe`` via its OpenAI-compatible
``/v1/chat/completions`` endpoint.  Inherits ``OpenAIClient`` wholesale —
only overrides constructor defaults and error classification.

Key differences from OpenAI
---------------------------
- Default ``base_url`` is ``http://127.0.0.1:8080/v1``.
- No API key required (llama-server has no auth by default).
- ``APIConnectionError`` is always retryable (server may not be running).
- ``StreamStart.provider`` is ``"llamacpp"`` (inherited via ``self.provider.value``).
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import sys
from pathlib import Path
from typing import Any, ClassVar

import openai

from ..models import AgentConfig, ModelProvider
from .base import BaseClient, register_client
from .errors import ErrorCategory
from .openai import OpenAIClient

logger = logging.getLogger("agent_core.llm.llamacpp")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"
_DEFAULT_PORT = 8080
_SERVER_EXE = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "llama.cpp" / "llama-server.exe"
)
_MODELS_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "llama.cpp" / "models"
)

# ---------------------------------------------------------------------------
# Module-level server state (singleton — one server per process)
# ---------------------------------------------------------------------------

_server_proc: asyncio.subprocess.Process | None = None
_server_lock = asyncio.Lock()
_server_atexit_registered = False


def _sync_shutdown() -> None:
    """atexit handler — best-effort terminate (won't fire on os.execv)."""
    global _server_proc
    proc = _server_proc
    if proc is not None and proc.returncode is None:
        try:
            proc.terminate()
        except Exception:
            pass


async def shutdown_server() -> None:
    """Explicitly terminate the server subprocess started by this process.

    Must be called before ``os.execv`` / process restart — atexit does NOT
    fire on ``os._exit()`` used by ``os.execv`` on Windows.
    Idempotent; safe to call when no server is running.
    """
    global _server_proc
    async with _server_lock:
        proc = _server_proc
        if proc is None or proc.returncode is not None:
            return
        logger.info("shutting down llamacpp server (pid=%d)", proc.pid)
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass
        except Exception:
            logger.exception("error shutting down llamacpp server")
        finally:
            _server_proc = None


async def _shutdown_if_running() -> None:
    """If the server was started by us and the current config no longer
    uses the llamacpp provider, shut it down to free GPU resources."""
    global _server_proc
    async with _server_lock:
        if _server_proc is not None and _server_proc.returncode is None:
            await shutdown_server()


async def ensure_server(config: AgentConfig) -> None:
    """Ensure the local llama.cpp server is running.

    Idempotent — second call returns immediately if the server was
    already started by this process or detected as externally running.

    Called automatically by the LLM layer before the first request;
    entry points (CLI / QQBot) may also call it eagerly for better UX.
    """
    global _server_proc, _server_atexit_registered

    # -- quick return: not a llamacpp model -------------------------------
    try:
        model_id = config.model_id
    except ValueError:
        logger.debug("ensure_server: config.model_id raised ValueError — skipping")
        return
    if model_id.provider != ModelProvider.LLAMACPP:
        logger.debug("ensure_server: provider=%s (not llamacpp) — skipping", model_id.provider)
        await _shutdown_if_running()
        return

    logger.info(
        "ensure_server: llamacpp model=%s, server_exe=%s, models_dir=%s",
        model_id.name, _SERVER_EXE, _MODELS_DIR,
    )

    async with _server_lock:
        # -- already started by us ---------------------------------------
        if _server_proc is not None and _server_proc.returncode is None:
            logger.info("ensure_server: server already started (pid=%d)", _server_proc.pid)
            return

        # -- check if an external server is already listening -------------
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", _DEFAULT_PORT),
                timeout=3.0,
            )
            writer.close()
            logger.info("ensure_server: external server already on port %d", _DEFAULT_PORT)
            _server_proc = None
            return
        except Exception:
            logger.debug("ensure_server: no server on port %d, will start", _DEFAULT_PORT)

        # -- locate model file -------------------------------------------
        model_name = model_id.name
        model_path = _MODELS_DIR / f"{model_name}.gguf"
        if not model_path.is_file():
            candidates = list(_MODELS_DIR.glob(f"{model_name}*"))
            if candidates:
                model_path = candidates[0]
                logger.info(
                    "ensure_server: using %s (exact match %s not found)",
                    model_path, model_name + ".gguf",
                )
            else:
                logger.error(
                    "ensure_server: model file not found: %s or %s*.gguf "
                    "in %s/",
                    model_path, model_name, _MODELS_DIR,
                )
                return

        logger.info("ensure_server: model_path=%s", model_path)

        # -- locate server executable ------------------------------------
        if not _SERVER_EXE.is_file():
            logger.error(
                "ensure_server: server exe not found: %s",
                _SERVER_EXE,
            )
            return

        logger.info("ensure_server: server_exe=%s", _SERVER_EXE)

        # -- start server ------------------------------------------------
        ctx_size = config.context_size or 8192
        llc = config.llamacpp
        logger.info(
            "ensure_server: starting subprocess — model=%s ctx=%d ngl=%d "
            "kv=%s/%s",
            model_path.name, ctx_size, llc.gpu_layers,
            llc.cache_type_k, llc.cache_type_v,
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                str(_SERVER_EXE),
                "-m", str(model_path),
                "--host", "127.0.0.1",
                "--port", str(_DEFAULT_PORT),
                "--ctx-size", str(ctx_size),
                "--cache-type-k", llc.cache_type_k,
                "--cache-type-v", llc.cache_type_v,
                "-ngl", str(llc.gpu_layers),
                "--tools", "all",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            logger.exception("failed to start llama.cpp server")
            return

        _server_proc = proc

        # register atexit cleanup (once)
        if not _server_atexit_registered:
            atexit.register(_sync_shutdown)
            _server_atexit_registered = True

        # drain stderr in background
        _stderr_chunks: list[bytes] = []

        async def _drain_stderr() -> None:
            assert proc.stderr is not None
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                _stderr_chunks.append(chunk)

        drain_task = asyncio.ensure_future(_drain_stderr())

        # -- wait for server to become ready -----------------------------
        deadline = asyncio.get_event_loop().time() + 120.0
        print("[llamacpp] loading model…", end="", flush=True)
        while asyncio.get_event_loop().time() < deadline:
            if proc.returncode is not None:
                await drain_task
                stderr_text = (
                    b"".join(_stderr_chunks)
                    .decode("utf-8", errors="replace")[-2000:]
                )
                print(
                    f"\n[llamacpp] server exited unexpectedly (code={proc.returncode})",
                    file=sys.stderr,
                )
                if stderr_text:
                    print(f"[llamacpp] stderr tail:\n{stderr_text}", file=sys.stderr)
                try:
                    proc.terminate()
                except Exception:
                    pass
                _server_proc = None
                return

            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", _DEFAULT_PORT),
                    timeout=3.0,
                )
                writer.write(
                    b"GET /v1/models HTTP/1.1\r\n"
                    b"Host: 127.0.0.1:%d\r\n"
                    b"Connection: close\r\n"
                    b"\r\n" % (_DEFAULT_PORT,)
                )
                await writer.drain()
                response = await asyncio.wait_for(reader.read(4096), timeout=3.0)
                writer.close()
                await writer.wait_closed()
                if b"200" in response[:256]:
                    print(f"\n[llamacpp] ready (port {_DEFAULT_PORT} responding)")
                    drain_task.cancel()
                    try:
                        await drain_task
                    except asyncio.CancelledError:
                        pass
                    return
            except Exception:
                pass

            await asyncio.sleep(1.0)
            print(".", end="", flush=True)

        # timeout — clean up
        drain_task.cancel()
        try:
            await drain_task
        except asyncio.CancelledError:
            pass
        stderr_text = (
            b"".join(_stderr_chunks).decode("utf-8", errors="replace")[-2000:]
        )
        print(
            f"\n[llamacpp] server did not become ready within 120 s (pid={proc.pid})",
            file=sys.stderr,
        )
        if stderr_text:
            print(f"[llamacpp] stderr tail:\n{stderr_text}", file=sys.stderr)
        try:
            proc.terminate()
            await proc.wait()
        except Exception:
            pass
        _server_proc = None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@register_client
class LlamaCppClient(OpenAIClient):
    """Streaming client backed by a local llama.cpp server (OpenAI-compatible API)."""

    provider: ClassVar[ModelProvider] = ModelProvider.LLAMACPP

    def __init__(
        self,
        model: str,
        api_key: str | None,
        base_url: str | None,
        sampling_kwargs: dict[str, Any] | None = None,
        sampling_extra: dict[str, Any] | None = None,
    ) -> None:
        import httpx

        super().__init__(
            model=model,
            api_key=api_key or "not-needed",
            base_url=base_url or _DEFAULT_BASE_URL,
            sampling_kwargs=sampling_kwargs,
            sampling_extra=sampling_extra,
        )
        # Re-create the OpenAI client with system proxy bypassed —
        # local server must be reached directly on 127.0.0.1.
        kwargs: dict[str, Any] = {
            "api_key": api_key or "not-needed",
            "base_url": base_url or _DEFAULT_BASE_URL,
            "http_client": httpx.AsyncClient(
                transport=httpx.AsyncHTTPTransport(proxy=None),
            ),
        }
        self._client = openai.AsyncOpenAI(**kwargs)

    @classmethod
    def classify_error(cls, exception: Exception) -> ErrorCategory:
        """Connection refused / timeout → retryable; delegate the rest to OpenAI logic."""
        if isinstance(exception, openai.APIConnectionError):
            return "transient"
        return super().classify_error(exception)
