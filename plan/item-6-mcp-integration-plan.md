# Item 6: MCP 协议集成 —— 实现计划

> 目标：MCP 客户端，动态获取服务端工具并注册，配置来源管理，工具冲突去重，OAuth 认证。
> 对应 TodoList 条目 #6，预估 2 天。

---

> **状态** (2026-06-21)：计划阶段（已修订）。`mcp>=1.0,<2` 已在 `pyproject.toml` 中声明，`AgentConfig.mcp_servers` 字段已预留但未消费。
>
> **本次修订要点**：① 连接生命周期改为单一 `AsyncExitStack`（`McpSessionManager`），修复"块结束即销毁 session"的根本缺陷；② 满足 anyio 同 task 进入/退出约束；③ 注册表与连接表改为 query-scoped 实例（支撑并发与子 Agent 隔离）；④ 删除重复 `initialize()`；⑤ OAuth 接入 transport header；⑥ 修复 optional 字段 Pydantic 校验、`_is_fatal_error` 误判、`_needs_refresh` 注释/实现不符；⑦ 补全 `${ENV_VAR}` 展开。

---

## 1. 架构总览

### 1.1 MCP 在 Agent 生命周期中的位置

```
Agent(config).run(prompt)
  │
  ▼
agent.run() ──► agent.stream() ──► query() 主循环
                                       │
                              ┌────────▼──────────┐
                              │  1. 初始化阶段      │
                              │  - enrich_context  │
                              │  - get_client      │
                              │  - convert_tools   │  ← 在此之前需要 MCP 工具已在注册表中
                              │                    │
                              │  ★ NEW:            │
                              │  mcp = McpSessionManager()               │
                              │  for s in config.mcp_servers:            │
                              │    await mcp.connect(s)  → enter stack    │
                              │    register_mcp_tools(registry, s, state) │
                              └────────────────────┘
                                       │
                              ┌────────▼──────────┐
                              │  2. Turn 循环      │
                              │  - LLM 调用        │
                              │  - run_tools()     │  ← MCP 工具与内置工具走同一条管线
                              │    ├─ resolve      │
                              │    ├─ validate     │
                              │    ├─ permission   │
                              │    └─ execute      │
                              └────────────────────┘
                                       │
                              ┌────────▼──────────┐
                              │  3. 清理阶段       │
                              │  finally:          │
                              │    await mcp.aclose()  (same task)       │
                              └────────────────────┘
```

**核心设计原则**：
- MCP 工具 = 标准 `Tool` 子类，走与内置工具完全相同的管线（resolver → validator → permission → executor）
- MCP 连接管理在 `query()` 初始化阶段完成，外部不可见
- 命名空间隔离：`mcp__{server_name}__{tool_name}` 避免冲突
- 配置加载遵循项目已有优先级链模式（代码注入 > JSON 文件 > 默认值）
- 连接失败不阻塞 Agent 启动（降级运行，日志 warning）

**生命周期关键约束**（必须严格遵守，否则"能连上但调不动"）：
- MCP 连接（`stdio_client` / `ClientSession`）是基于 anyio 的 `async with` 上下文，**进入和退出必须在同一个 task**。连接句柄必须存活到整个 `query()` 生命周期，因此**不能**在 `connect_to_server` 内部用 `async with ... return` 的方式建立连接——那会在 return 时立即触发 `__aexit__`，销毁 session 和子进程。
- 统一用一个 `contextlib.AsyncExitStack`（在 `query()` 主 task 内创建并持有），通过 `enter_async_context()` 进入每个连接，在 `query()` 的 `finally` 块里 `await stack.aclose()` 统一退出。`AsyncExitStack` 的 enter / aclose 必须在同一 task，绝不能跨 task 或被外层 `asyncio.timeout` 的取消打断。
- 注册表与连接表均为 **query-scoped 实例**（随 `query()` 调用创建），**不是模块级全局**——否则并发 query / 子 Agent（Item 8）会互相污染工具注册表与连接状态。

### 1.2 新增文件与模块

```
src/agent_core/mcp/
├── __init__.py              # 公开导出: McpSessionManager, McpServerConfig
├── config.py                # McpServerConfig + 配置加载链
├── client.py                # McpSessionManager (AsyncExitStack 持有连接) / connect / fetch_tools
├── registry.py              # MCP 工具 → Tool 子类动态生成 + 冲突检测 + 注册（query-scoped）
├── auth.py                  # OAuth 认证流程（token 刷新 / step-up / 过期处理）
└── _transport.py            # 内部: stdio/SSE/WebSocket transport 适配层
```

> **注意**：连接不再以模块级 `_active_connections` 全局字典持有，而是由 `McpSessionManager` 实例持有（每个 `query()` 一个）。工具注册同理走 query-scoped 注册表实例。

### 1.3 影响/修改的现有文件

| 文件 | 修改内容 |
|------|----------|
| `models.py` | 新增 `McpServerConfig` / `McpOAuthConfig` / `McpServerTransport`；`AgentConfig.mcp_servers` 类型从 `dict[str, Any]` 收紧为 `dict[str, McpServerConfig]` |
| `tools/registry.py` | **前置重构**：把模块级 `_registry` dict + `register_tool()` 重构为可实例化的 `ToolRegistry`（`register`/`unregister`/`contains`/`names`/`registered_tools`）。query() 持有自己的实例（子 Agent 隔离前置项） |
| `query/loop.py` | 初始化阶段创建 `McpSessionManager` 并 bootstrap；`finally` 块 `await mcp.aclose()`（同 task）；`convert_tools` 改为从 query-scoped registry 读取 |
| `llm/anthropic.py`（及其他 provider） | `convert_tools()` 数据源从模块全局改为传入的 `registry` 实例 |
| `__init__.py` | 导出 `McpServerConfig` / `McpServerTransport` / `McpOAuthConfig` / `McpSessionManager` |
| `cli.py` | 新增 `_load_mcp_servers()` + `_expand_env_vars()`，加载 `~/.agent/mcp.json` 和项目 `.mcp.json` |

---

## 2. 数据模型设计

### 2.1 McpServerConfig (`models.py`)

```python
class McpServerTransport(str, Enum):
    STDIO = "stdio"
    SSE = "sse"
    WEBSOCKET = "websocket"


class McpServerConfig(BaseModel):
    """Configuration for a single MCP server connection."""

    transport: McpServerTransport = McpServerTransport.STDIO
    command: str | None = None          # stdio: executable path (e.g. "npx", "python")
    args: list[str] = Field(default_factory=list)  # stdio: CLI arguments
    env: dict[str, str] = Field(default_factory=dict)  # stdio: extra environment
    url: str | None = None              # sse/websocket: server endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # sse/ws: HTTP headers
    cwd: str | None = None              # stdio: working directory

    # -- OAuth --
    auth: McpOAuthConfig | None = None

    # -- Tool filtering (optional) --
    # Semantics: None or [] = no filtering on that axis (include=all / exclude=none).
    # exclude is applied AFTER include. A non-empty include = allow-list.
    include_tools: list[str] | None = None   # regex patterns
    exclude_tools: list[str] | None = None   # regex patterns

    # -- Connection management --
    connect_timeout: float = 30.0       # seconds
    request_timeout: float = 60.0       # seconds
    max_reconnect_attempts: int = 3
    reconnect_delay: float = 2.0        # seconds (exponential backoff base)

    @model_validator(mode="after")
    def _validate_transport_fields(self) -> "McpServerConfig":
        if self.transport == McpServerTransport.STDIO:
            if not self.command:
                raise ValueError("'command' is required for stdio transport")
        elif self.transport in (McpServerTransport.SSE, McpServerTransport.WEBSOCKET):
            if not self.url:
                raise ValueError("'url' is required for sse/websocket transport")
        return self
```

### 2.2 McpOAuthConfig (`models.py`)

```python
class McpOAuthConfig(BaseModel):
    """OAuth 2.0 / 2.1 configuration for MCP server authentication."""

    client_id: str
    client_secret: str | None = None    # None for PKCE (public clients)
    authorization_url: str | None = None  # for manual / device-code flows
    token_url: str                      # token endpoint
    scopes: list[str] = Field(default_factory=list)
    redirect_uri: str | None = None
    device_code_url: str | None = None  # for device-code flow

    # -- Token storage --
    token: str | None = None            # bearer access token (runtime-only)
    refresh_token: str | None = None    # runtime-only
    expires_at: float | None = None     # unix timestamp; 0 = never

    # -- Metadata validation --
    audience: str | None = None         # for JWT audience validation
    issuer: str | None = None           # for JWT issuer validation
```

### 2.3 McpConnectionState (内部类型, `client.py`)

```python
from dataclasses import dataclass, field
from contextlib import AsyncExitStack
import asyncio
from mcp import ClientSession

@dataclass
class McpConnectionState:
    """Runtime state for an active MCP server connection."""
    server_name: str
    config: McpServerConfig
    session: ClientSession | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)  # raw tool defs from server
    connected_at: float = 0.0
    connection_error: str | None = None
    fatal: bool = False  # True = unrecoverable, don't retry


class McpSessionManager:
    """Owns all MCP connections for a single query() invocation.

    Connections are entered via a single AsyncExitStack and torn down
    together on aclose(). enter / aclose MUST run in the same task.
    NOT a module-level global — one instance per query()/sub-agent so
    concurrent queries never share state.
    """
    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self.connections: dict[str, McpConnectionState] = {}

    async def aclose(self) -> None:
        # Single point of teardown for every entered transport/session.
        await self._stack.aclose()
        self.connections.clear()
```

> teardown 句柄不再单独存 `(read, write)`——所有连接由 `McpSessionManager._stack` 统一管理，`session` 字段在 `aclose()` 前持续有效。

### 2.4 配置加载链 (`mcp/config.py`)

配置来源优先级（后加载的覆盖前者的同名字段）：

```
4. AgentConfig.mcp_servers (代码注入)      ← 最高优先级
3. .mcp.json (项目根目录)                   ← 项目级
2. ~/.agent/mcp.json (用户目录)             ← 用户级
1. 内置默认 (空)                            ← 最低优先级
```

**`.mcp.json` 格式**：

```json
{
    "mcpServers": {
        "filesystem": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/allowed/files"]
        },
        "github": {
            "transport": "sse",
            "url": "https://api.github.com/mcp",
            "headers": {
                "Authorization": "Bearer ${GITHUB_TOKEN}"
            }
        },
        "postgres": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-postgres", "postgresql://localhost/mydb"],
            "exclude_tools": ["drop_table", "truncate_table"]
        }
    }
}
```

`${ENV_VAR}` 占位符在加载时展开为环境变量值。

---

## 3. 模块详细设计

### 3.1 Transport 适配层 (`mcp/_transport.py`)

内部模块，封装 `mcp` SDK 的 transport 创建逻辑。

```python
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp.client.sse import sse_client
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from typing import Any

@asynccontextmanager
async def open_session(
    config: McpServerConfig,
    auth_headers: dict[str, str] | None = None,
) -> AsyncGenerator[ClientSession, None]:
    """Open an MCP session for *config* and yield it.

    The session is yielded *inside* the transport's async-with, so the
    transport (and any stdio subprocess) stays alive for as long as the
    caller holds the session. The caller enters this via
    ``AsyncExitStack.enter_async_context`` and never exits it manually —
    teardown happens when the stack is closed (same task).

    initialize() is called exactly once, here.
    """
    match config.transport:
        case McpServerTransport.STDIO:
            params = StdioServerParameters(
                command=config.command,
                args=config.args,
                env=config.env or None,
                cwd=config.cwd,
            )
            async with stdio_client(params) as (read, write), \
                       ClientSession(read, write) as session:
                await session.initialize()
                yield session

        case McpServerTransport.SSE:
            headers = {**config.headers, **(auth_headers or {})}
            async with sse_client(url=config.url, headers=headers) as (read, write), \
                       ClientSession(read, write) as session:
                await session.initialize()
                yield session

        case McpServerTransport.WEBSOCKET:
            # websocket transport — depends on mcp SDK version
            raise NotImplementedError("WebSocket transport not yet supported")
```

**设计要点**：
- `initialize()` 只在此处调用一次（之前 `connect_to_server` 里的重复调用已删除）
- `auth_headers` 由 OAuth token manager 注入（见 §3.4），打通 OAuth 与 transport 的集成
- stdio 用 `StdioServerParameters` 包装参数，对齐 SDK 实际签名（`stdio_client` 接收单个 params 对象）
- 调用方通过 `AsyncExitStack.enter_async_context(open_session(...))` 进入，**不**手动 `async with`——这样 session 才能跨出函数存活

### 3.2 连接管理 (`mcp/client.py`)

```python
import asyncio
import logging
import re
import time as _time
from contextlib import AsyncExitStack
from typing import Any

from ._transport import open_session
from .auth import OAuthTokenManager
from ..models import McpServerConfig, McpServerTransport

logger = logging.getLogger("agent_core.mcp")


def _tools_from_result(result, config: McpServerConfig) -> list[dict[str, Any]]:
    tools = [
        {"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema}
        for t in result.tools
    ]
    return _filter_tools(tools, config.include_tools, config.exclude_tools)


class McpSessionManager:
    """Owns all MCP connections for one query() invocation.

    All sessions are entered through a single AsyncExitStack. connect()
    and aclose() MUST execute in the same task (anyio cancel-scope
    requirement) — do NOT wrap connect() in an outer asyncio.timeout that
    could cancel across the stack boundary; the per-attempt timeout below
    lives entirely inside the connecting coroutine.
    """

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self.connections: dict[str, McpConnectionState] = {}

    async def connect(self, name: str, config: McpServerConfig) -> McpConnectionState:
        """Connect to one server, entering its session into the shared stack.

        On success, session stays alive until aclose(). On failure the
        state carries connection_error + fatal flag and nothing is left
        on the stack for this server.
        """
        state = McpConnectionState(server_name=name, config=config)

        # Resolve OAuth headers up front (if configured).
        auth_headers: dict[str, str] | None = None
        if config.auth is not None:
            token = await OAuthTokenManager(config.auth).get_valid_token()
            if token:
                auth_headers = {"Authorization": f"Bearer {token}"}

        for attempt in range(config.max_reconnect_attempts + 1):
            try:
                # Per-attempt timeout lives inside this task; it guards only
                # the connect+handshake, not the long-lived session.
                async with asyncio.timeout(config.connect_timeout):
                    session = await self._stack.enter_async_context(
                        open_session(config, auth_headers)
                    )
                    result = await session.list_tools()

                state.session = session
                state.tools = _tools_from_result(result, config)
                state.connected_at = _time.monotonic()
                state.connection_error = None
                state.fatal = False
                self.connections[name] = state
                logger.info(
                    "Connected to MCP server '%s' (%s): %d tools",
                    name, config.transport.value, len(state.tools),
                )
                return state

            except asyncio.TimeoutError:
                msg = f"Connection to '{name}' timed out after {config.connect_timeout}s"
                logger.warning(msg)
                state.connection_error, state.fatal = msg, False

            except Exception as exc:
                msg = f"Failed to connect to MCP server '{name}': {exc}"
                is_fatal = _is_fatal_error(exc)
                if is_fatal or attempt >= config.max_reconnect_attempts:
                    logger.error(msg)
                    state.connection_error, state.fatal = msg, is_fatal
                    break
                delay = config.reconnect_delay * (2 ** attempt)
                logger.warning("%s — retrying in %.1fs (attempt %d/%d)",
                               msg, delay, attempt + 1, config.max_reconnect_attempts)
                await asyncio.sleep(delay)

        self.connections[name] = state
        return state

    async def refresh_tools(self, name: str) -> list[dict[str, Any]]:
        """Re-list tools (e.g. on notifications/list_changed)."""
        state = self.connections.get(name)
        if state is None or state.session is None:
            raise RuntimeError(f"MCP server '{name}' is not connected")
        result = await state.session.list_tools()
        state.tools = _tools_from_result(result, state.config)
        return state.tools

    async def aclose(self) -> None:
        """Tear down every session/transport. Must run in the same task
        that called connect()."""
        try:
            await self._stack.aclose()
        except Exception as exc:
            logger.warning("Error during MCP teardown: %s", exc)
        finally:
            self.connections.clear()
            logger.info("All MCP connections closed")


def _filter_tools(
    tools: list[dict[str, Any]],
    include: list[str] | None,
    exclude: list[str] | None,
) -> list[dict[str, Any]]:
    """Apply regex include/exclude filters to tool list."""
    result = tools
    if include:
        patterns = [re.compile(p) for p in include]
        result = [t for t in result if any(p.search(t["name"]) for p in patterns)]
    if exclude:
        patterns = [re.compile(p) for p in exclude]
        result = [t for t in result if not any(p.search(t["name"]) for p in patterns)]
    return result


def _is_fatal_error(exc: Exception) -> bool:
    """Determine if a *connection* error is fatal (no retry).

    Prefer typed/status-based checks over substring matching — matching
    'invalid'/'not found' against arbitrary message text wrongly marks
    transient or tool-level errors as fatal. We inspect HTTP status codes
    where available and a *tight* set of auth signals otherwise.
    """
    # 1. HTTP status (httpx / SDK errors usually carry .response.status_code)
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return True
    if status is not None and 500 <= status < 600:
        return False  # server-side, worth retrying

    # 2. Config/transport errors that can never succeed on retry
    if isinstance(exc, (NotImplementedError, FileNotFoundError, ValueError)):
        return True

    # 3. Narrow auth keyword fallback (only unambiguous auth terms)
    msg = str(exc).lower()
    return any(kw in msg for kw in ("unauthorized", "forbidden", "invalid_grant", "invalid_client"))
```

> 删去了原 `disconnect_all()` 空壳——真正的清理由 `McpSessionManager.aclose()` 通过 `AsyncExitStack` 完成，不再依赖手动持有 `(read, write)`。
```

### 3.3 MCP 工具注册 (`mcp/registry.py`)

核心：为每个 MCP 服务端工具动态创建一个 `Tool` 子类并注册到全局工具注册表。

```python
"""
MCP tool → Tool subclass bridge.

Each MCP tool is wrapped in a dynamically-generated ``Tool`` subclass
and registered under the name ``mcp__{server}__{tool}``.

The wrapper delegates ``call()`` to ``session.call_tool()`` and uses
the MCP-provided inputSchema for validation.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from pydantic import BaseModel, create_model

from ..tools.base import Tool, ToolCallResult, ToolUseContext
from ..tools.registry import ToolRegistry  # query-scoped registry instance type
from .client import McpConnectionState

logger = logging.getLogger("agent_core.mcp")

MCP_TOOL_PREFIX = "mcp__"


# NOTE: registration now operates on a *passed-in* ToolRegistry instance
# (one per query()/sub-agent), NOT a module-level global. This is required
# for Item 8 sub-agent isolation and for safe concurrent queries.


def _mcp_tool_name(server: str, tool: str) -> str:
    """Generate the namespaced tool name: mcp__{server}__{tool}."""
    return f"{MCP_TOOL_PREFIX}{server}__{tool}"


def _parse_mcp_tool_name(name: str) -> tuple[str, str] | None:
    """Parse 'mcp__server__tool' → ('server', 'tool'), or None."""
    if not name.startswith(MCP_TOOL_PREFIX):
        return None
    parts = name.removeprefix(MCP_TOOL_PREFIX).split("__", 1)
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def register_mcp_tools(
    registry: ToolRegistry,
    server_name: str,
    state: McpConnectionState,
) -> dict[str, type[Tool]]:
    """Create and register Tool subclasses for every tool on an MCP server.

    Operates on the *query-scoped* ``registry`` instance.

    Returns a dict of ``{tool_class_name: Tool subclass}`` for the newly
    registered tools.

    Conflict resolution:
    - Built-in tools with the same base name win (RETAIN existing).
    - Previously registered MCP tools from a different server → log warning
      but both coexist under different namespaced names.
    - Re-registering the SAME server → remove old, register new.
    """
    registered: dict[str, type[Tool]] = {}

    # -- 1. Remove stale registrations for this server ---------------
    _unregister_server_tools(registry, server_name)

    # -- 2. For each tool, build and register -----------------------
    for tool_def in state.tools:
        tool_name = tool_def["name"]
        namespaced = _mcp_tool_name(server_name, tool_name)

        # Conflict check: built-in (non-namespaced) name collision
        if registry.contains(tool_name):
            logger.warning(
                "MCP tool '%s' from server '%s' shadows built-in tool '%s'. "
                "Built-in wins. MCP tool registered as '%s'.",
                tool_name, server_name, tool_name, namespaced,
            )

        # Check for exact namespaced name collision (different server)
        if registry.contains(namespaced):
            logger.warning(
                "Namespaced tool '%s' already registered. "
                "Overwriting with tool from server '%s'.",
                namespaced, server_name,
            )

        # -- Build the dynamic Tool subclass -------------------------
        # NOTE: bind *state* (not state.session) so a later reconnect that
        # swaps state.session is picked up live. Capture via default arg to
        # avoid the classic late-binding closure bug in this loop.
        tool_cls = _build_mcp_tool_class(
            namespaced=namespaced,
            server_name=server_name,
            tool_def=tool_def,
            state=state,
        )

        try:
            registry.register(tool_cls)
            registered[namespaced] = tool_cls
        except (TypeError, ValueError) as exc:
            logger.error("Failed to register MCP tool '%s': %s", namespaced, exc)

    logger.info(
        "Registered %d/%d tools from MCP server '%s'",
        len(registered), len(state.tools), server_name,
    )
    return registered


def _build_mcp_tool_class(
    namespaced: str,
    server_name: str,
    tool_def: dict[str, Any],
    state: McpConnectionState,
) -> type[Tool]:
    """Dynamically create a Tool subclass wrapping one MCP tool."""

    # 1. Build the Pydantic Input model from MCP inputSchema
    input_model = _build_input_model(namespaced, tool_def)

    # 2. Resolve session lazily from the live state (survives reconnect)
    def _session_getter() -> Any:
        return state.session

    # 3. Dynamically create the Tool subclass
    class_dict = {
        "name": namespaced,
        "is_concurrency_safe": False,     # MCP tools default to serial
        "is_read_only": False,            # conservative default
        "is_destructive": False,          # conservative default
        "_server_name": server_name,
        "_session_getter": staticmethod(_session_getter),
        "_mcp_tool_name": tool_def["name"],
        "_input_model": input_model,
        "validate_input": classmethod(_make_validate_input(input_model)),
        "call": _make_mcp_call(namespaced),
    }

    # 4. Set class-level attributes for schema generation
    tool_cls = type(f"MCPTool_{namespaced.replace('-', '_')}", (Tool,), class_dict)

    return tool_cls


def _build_input_model(
    namespaced: str,
    tool_def: dict[str, Any],
) -> type[BaseModel]:
    """Convert MCP inputSchema (JSON Schema) to a Pydantic model.

    Simple approach: parse JSON Schema properties into Pydantic fields.
    Falls back to a generic dict-accepting model if the schema is complex
    (``$ref`` / ``oneOf`` / ``anyOf`` are not expanded — see Risks §9.3).
    """
    schema = tool_def.get("inputSchema", {})

    if not schema or schema.get("type") != "object":
        # Schema-less or non-object → accept raw dict
        return create_model(f"{namespaced}_Input", raw=(dict[str, Any], ...))

    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    fields: dict[str, Any] = {}
    for prop_name, prop_schema in properties.items():
        py_type = _json_type_to_python(prop_schema)
        if prop_name in required:
            fields[prop_name] = (py_type, ...)
        else:
            # Optional field: widen the type to allow None, default None.
            # (Previously `(int, None)` etc. raised a Pydantic validation
            # error because None isn't a valid int.)
            fields[prop_name] = (Optional[py_type], None)

    if not fields:
        # Object type without declared properties → accept raw dict
        fields["raw"] = (dict[str, Any], ...)

    return create_model(f"{namespaced}_Input", **fields)


def _json_type_to_python(prop_schema: dict) -> Any:
    """Map JSON Schema type → Python type for Pydantic."""
    type_map = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    jtype = prop_schema.get("type", "string")
    # JSON Schema allows type to be a list (e.g. ["string", "null"]) → Any
    if isinstance(jtype, list):
        return Any
    return type_map.get(jtype, Any)


def _make_validate_input(model_cls: type[BaseModel]):
    """Build the validate_input classmethod body bound to *model_cls*.

    Replaces the previous shared lambda — each tool class gets its own
    factory-produced function, which is easier to debug and avoids relying
    on `cls._input_model` lookup at call time.
    """
    def _validate(cls, raw: dict[str, Any]) -> BaseModel:
        if "raw" in model_cls.model_fields:
            return model_cls(raw=raw)
        return model_cls(**raw)
    return _validate


def _make_mcp_call(tool_name: str):
    """Create the async call() method for a dynamic MCP Tool subclass."""

    async def _call(self: Tool, input: BaseModel, context: ToolUseContext) -> ToolCallResult:
        session = self._session_getter()  # type: ignore[attr-defined]
        if session is None:
            return ToolCallResult(
                content=f"MCP server disconnected. Cannot call tool '{tool_name}'.",
                is_error=True,
            )

        # Extract arguments dict from the validated input model
        if "raw" in type(input).model_fields:
            args = input.raw  # type: ignore[attr-defined]
        else:
            args = input.model_dump(exclude_none=True)

        try:
            result = await session.call_tool(self._mcp_tool_name, args)  # type: ignore[attr-defined]

            # MCP returns list[ContentBlock]; extract text
            text_parts = []
            for block in result.content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)
                else:
                    text_parts.append(str(block))

            return ToolCallResult(
                content="\n".join(text_parts),
                is_error=getattr(result, "isError", False),
            )

        except Exception as exc:
            return ToolCallResult(
                content=f"MCP tool '{tool_name}' execution error: {exc}",
                is_error=True,
            )

    return _call


def _unregister_server_tools(registry: ToolRegistry, server_name: str) -> None:
    """Remove all tools belonging to *server_name* from the query-scoped registry."""
    prefix = _mcp_tool_name(server_name, "")
    to_remove = [name for name in registry.names() if name.startswith(prefix)]
    for name in to_remove:
        registry.unregister(name)  # also drops any cached instance
    if to_remove:
        logger.debug("Unregistered %d tools from MCP server '%s'", len(to_remove), server_name)
```

> **依赖**：本节假设 `ToolRegistry` 暴露 `register(cls)` / `unregister(name)` / `contains(name)` / `names()` 实例方法。如果当前 `tools/registry.py` 只有模块级 `register_tool` + `_registry` dict，需要先把它重构成可实例化的 `ToolRegistry`（这是 Item 8 子 Agent 隔离的前置项，建议在 Step 4 之前补一个小重构步骤）。

### 3.4 OAuth 认证 (`mcp/auth.py`)

```python
"""
OAuth 2.0 / 2.1 authentication for MCP servers.

Supports:
- Client credentials (machine-to-machine)
- Authorization code with PKCE
- Device code flow
- Token refresh
- Manual step-up when token expires
"""

import asyncio
import logging
import time as _time
from typing import Any

import httpx
from ..models import McpOAuthConfig

logger = logging.getLogger("agent_core.mcp.auth")


class OAuthTokenManager:
    """Manages the OAuth token lifecycle for one MCP server."""

    def __init__(self, config: McpOAuthConfig) -> None:
        self._config = config
        self._lock = asyncio.Lock()

    async def get_valid_token(self) -> str | None:
        """Return a valid access token, refreshing if needed.

        Returns None if no token is available and auto-refresh is not possible.
        """
        async with self._lock:
            if self._config.token:
                # Check if token needs refresh
                if self._needs_refresh():
                    await self._refresh_token()
                return self._config.token

            # -- No token yet — try client credentials if available --
            if self._config.client_secret:
                await self._client_credentials_flow()
                return self._config.token

            return None

    async def start_authorization_code_flow(self, code: str, code_verifier: str | None = None) -> str:
        """Exchange an authorization code for tokens."""
        async with self._lock:
            payload = {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self._config.client_id,
                "redirect_uri": self._config.redirect_uri,
            }
            if self._config.client_secret:
                payload["client_secret"] = self._config.client_secret
            if code_verifier:
                payload["code_verifier"] = code_verifier

            return await self._token_request(payload)

    async def start_device_code_flow(self) -> dict[str, str]:
        """Initiate device code flow. Returns {'device_code', 'user_code', 'verification_uri'}."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._config.device_code_url,
                data={
                    "client_id": self._config.client_id,
                    "scope": " ".join(self._config.scopes),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._config.token = data.get("access_token")
            self._config.refresh_token = data.get("refresh_token")
            expires = data.get("expires_in")
            if expires:
                self._config.expires_at = _time.time() + expires
            return data

    async def _refresh_token(self) -> None:
        """Refresh the access token using a stored refresh token."""
        if not self._config.refresh_token:
            return

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    self._config.token_url,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self._config.refresh_token,
                        "client_id": self._config.client_id,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                self._config.token = data["access_token"]
                self._config.refresh_token = data.get("refresh_token", self._config.refresh_token)
                expires = data.get("expires_in")
                if expires:
                    self._config.expires_at = _time.time() + expires
                logger.info("OAuth token refreshed for '%s'", self._config.client_id)

        except Exception as exc:
            logger.error("Token refresh failed: %s", exc)
            self._config.token = None

    async def _client_credentials_flow(self) -> None:
        """Perform client credentials grant."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self._config.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                    "scope": " ".join(self._config.scopes),
                },
            )
            resp.raise_for_status()
            data = resp.json()
            self._config.token = data["access_token"]
            expires = data.get("expires_in")
            if expires:
                self._config.expires_at = _time.time() + expires

    async def _token_request(self, payload: dict[str, Any]) -> str:
        """POST to token endpoint and store tokens."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(self._config.token_url, data=payload)
            resp.raise_for_status()
            data = resp.json()
            self._config.token = data["access_token"]
            self._config.refresh_token = data.get("refresh_token")
            expires = data.get("expires_in")
            if expires:
                self._config.expires_at = _time.time() + expires
            logger.info("OAuth token obtained for '%s'", self._config.client_id)
            return self._config.token

    def _needs_refresh(self) -> bool:
        """Refresh once the token enters the last 20% of its lifetime
        (and always with at least a 60s safety margin)."""
        if self._config.expires_at is None:
            return False
        now = _time.time()
        ttl_remaining = self._config.expires_at - now
        # We don't know the original TTL precisely; approximate the 20%
        # window from the remaining lifetime and clamp to >= 60s.
        margin = max(60.0, 0.2 * max(ttl_remaining, 0.0))
        return ttl_remaining < margin
```

### 3.5 MCP 资源/Prompt 获取 (`mcp/resources.py`)

```python
"""
Optional: MCP Resource & Prompt fetching.

Resources and prompts are not automatically registered as Agent tools.
Instead they are available for:
- Direct access via the MCP client handle
- Future mapping to Skill system (Phase 3 — Item 9)
"""

import logging
from typing import Any

logger = logging.getLogger("agent_core.mcp.resources")


async def fetch_resources(session) -> list[dict[str, Any]]:
    """Fetch all resources from an MCP server."""
    try:
        result = await session.list_resources()
        return [
            {"uri": str(r.uri), "name": r.name, "description": r.description or ""}
            for r in result.resources
        ]
    except Exception as exc:
        logger.warning("Failed to fetch resources: %s", exc)
        return []


async def read_resource(session, uri: str) -> list[Any]:
    """Read a specific resource by URI."""
    result = await session.read_resource(uri)
    return result.contents
```

---

## 4. 集成点

### 4.1 `query()` 主循环集成 (`query/loop.py`)

在初始化阶段（LLM 调用之前）新增 MCP bootstrap：

```python
# query/loop.py — 新增 imports
from ..mcp.client import McpSessionManager
from ..mcp.registry import register_mcp_tools

async def query(prompt, config, trace_id, ...):
    # The registry is created PER query() — built-ins are seeded into it,
    # then MCP tools are added on top. Nothing here touches module globals,
    # so concurrent queries / sub-agents (Item 8) stay isolated.
    registry = build_tool_registry(config.tools)   # query-scoped ToolRegistry

    # The session manager owns an AsyncExitStack. Both its connect() and its
    # aclose() MUST run in THIS task — that's why bootstrap and cleanup live
    # in the same function, not in a helper task or a background group.
    mcp = McpSessionManager()

    # ---- 0. MCP bootstrap (before convert_tools) ----
    if config.mcp_servers:
        for server_name, server_config in config.mcp_servers.items():
            try:
                state = await mcp.connect(server_name, server_config)
                if state.connection_error is None:
                    register_mcp_tools(registry, server_name, state)
                else:
                    logger.warning(
                        "MCP server '%s' unavailable (%s) — continuing without it",
                        server_name, state.connection_error,
                    )
            except Exception as exc:
                # A single bad server must never abort the whole agent.
                logger.warning("MCP bootstrap error for '%s' (skipping): %s",
                               server_name, exc)

    try:
        # ---- 1. Original init ----
        # convert_tools now reads from the query-scoped `registry`, which
        # already contains both built-ins and MCP tools.
        system = await enrich_context(workspace_root)
        messages = [_build_initial_user_message(prompt)]
        # ...

        # ---- 2. Turn loop ----
        for turn_index in range(1, config.max_turns + 1):
            # ...
            ...
    finally:
        # ---- 3. Cleanup — same task as connect(), unconditional ----
        await mcp.aclose()
```

**关键设计决策**：
- MCP bootstrap 必须在 `convert_tools()` **之前**执行，否则 MCP 工具不会出现在发送给 LLM 的 tool schema 中
- 单个 MCP server 连接失败**不阻塞** Agent 启动，也**不影响**其他 server——内置工具与其余 MCP 工具仍可用
- `connect()` 与 `aclose()` 在**同一个 task** 内执行，满足 anyio cancel-scope 约束；`finally` 块无条件 `aclose()`，无论 Agent 如何终止都会清理（含 stdio 子进程）
- 注册表是 **query-scoped 实例**，`convert_tools()` 从该实例读取，天然支持并发 query 与子 Agent 隔离

### 4.2 `convert_tools()` 与 MCP 工具的配合

`convert_tools()` 从**传入的 query-scoped registry**读取 schema。MCP 工具注册到该实例后自动包含在内，**无需修改 LLM provider 的转换逻辑**（仅需把数据源从模块全局改为传入的 registry 实例）。

```python
# llm/anthropic.py — convert_tools()
@classmethod
def convert_tools(cls, registry: ToolRegistry, allowed_tools: list[str]) -> list[dict]:
    all_tools = registry.registered_tools()
    # allowed_tools=["*"] → all registered tools (including MCP)
    # allowed_tools=["Read", "mcp__github__list_issues"] → subset
    ...
```

### 4.3 CLI 集成 (`cli.py`)

CLI 配置加载时新增 `.mcp.json` 解析：

```python
# cli.py — _load_config() 中新增
import os
import re

_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_vars(obj: Any) -> Any:
    """Recursively expand ${ENV_VAR} placeholders in str/list/dict values.

    A missing variable raises KeyError (fail fast) rather than silently
    expanding to an empty string — a blank Authorization header is worse
    than a clear startup error.
    """
    if isinstance(obj, str):
        def _sub(m: re.Match) -> str:
            var = m.group(1)
            if var not in os.environ:
                raise KeyError(f"Environment variable '{var}' referenced in .mcp.json is not set")
            return os.environ[var]
        return _ENV_PLACEHOLDER.sub(_sub, obj)
    if isinstance(obj, list):
        return [_expand_env_vars(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    return obj


def _load_mcp_servers() -> dict[str, McpServerConfig]:
    """Load MCP servers from file chain: ~/.agent/mcp.json → .mcp.json (project overrides)."""
    servers: dict[str, Any] = {}

    # 1. User-level
    user_mcp = Path.home() / ".agent" / "mcp.json"
    if user_mcp.is_file():
        raw = json.loads(user_mcp.read_text(encoding="utf-8"))
        servers.update(raw.get("mcpServers", {}))

    # 2. Project-level (overrides same-named entries)
    project_mcp = Path(".mcp.json").resolve()
    if project_mcp.is_file():
        raw = json.loads(project_mcp.read_text(encoding="utf-8"))
        servers.update(raw.get("mcpServers", {}))

    # Expand ${ENV_VAR} placeholders, then validate into typed configs
    return {name: McpServerConfig(**_expand_env_vars(cfg)) for name, cfg in servers.items()}
```

> 合并是 server 条目级别的覆盖（项目级整体替换同名 server），不做字段级深合并——与 §2.4 的优先级链一致，避免"半个 server 配置"这类难调试的中间状态。

### 4.4 公开 API 导出 (`__init__.py`)

```python
# 增量导出
from .models import McpServerConfig, McpServerTransport, McpOAuthConfig
from .mcp import McpSessionManager

__all__ += [
    "McpServerConfig",
    "McpServerTransport",
    "McpOAuthConfig",
    "McpSessionManager",
]
```

---

## 5. 测试策略

### 5.1 单元测试 (`tests/unit/test_mcp.py`)

| # | 测试项 | 说明 |
|---|--------|------|
| 1 | `McpServerConfig` validation | stdio 缺少 command → ValueError；sse 缺少 url → ValueError；合法配置通过 |
| 2 | `McpOAuthConfig` model | 字段默认值、校验逻辑 |
| 3 | `_mcp_tool_name()` / `_parse_mcp_tool_name()` | 命名格式正确性、边界条件（空串、单下划线、特殊字符） |
| 4 | `_filter_tools()` | include/exclude regex 过滤；空过滤；所有匹配/不匹配 |
| 5 | `_build_input_model()` | JSON Schema → Pydantic model：基本类型、required 字段、嵌套对象 |
| 6 | `_json_type_to_python()` | 类型映射覆盖（string→str, integer→int, number→float, boolean→bool, array→list, object→dict） |
| 7 | `_build_mcp_tool_class()` | 动态 Tool 子类：name 属性、input_schema 返回格式、validate_input 校验 |
| 8 | `_make_validate_input()` | 正确输入通过、缺失 required 失败、optional 缺省为 None、raw-dict 直通 |
| 9 | `register_mcp_tools(registry, ...)` | 注册到传入实例、工具计数、冲突检测（内置优先）、重注册同 server 先清旧 |
| 10 | `_unregister_server_tools(registry, ...)` | 清理指定 server 的工具、残留检测、不误删其他 server |
| 11 | `_is_fatal_error()` | 401/403/typed config error→fatal；timeout/5xx→retry；普通工具级 "not found" 不误判 |
| 12 | `OAuthTokenManager` | needs_refresh 逻辑、token 过期检测（剩余 TTL < max(60s, 20% TTL)） |
| 13 | `_build_input_model()` optional 字段 | 非 required `int`/`list` 字段缺省 None 不报 Pydantic 校验错（Optional 包装） |

### 5.2 Mock 集成测试 (`tests/integration/test_mcp.py`)

使用 mock `mcp.ClientSession` 测试端到端流程：

| # | 测试项 | 说明 |
|---|--------|------|
| 1 | `McpSessionManager.connect` — success | mock session 返回工具列表 → 验证 `McpConnectionState.tools` 与 stack 持有 |
| 2 | `connect` — timeout | mock 超时 → retry → 最终失败状态，session 未进入 stack |
| 3 | `connect` — fatal | mock 401 → 不重试 → fatal=True |
| 4 | **session 跨函数存活** | connect 后在另一处 `call_tool` 仍可用（验证未被 `__aexit__` 提前销毁——回归保护核心 bug） |
| 5 | `register_mcp_tools` + query loop | 注册 mock MCP 工具 → 验证 `convert_tools(registry,...)` 包含该工具 |
| 6 | MCP 工具执行管线 | mock session.call_tool 返回 → 验证 ToolResult |
| 7 | 配置加载链 | .mcp.json 加载 → 项目覆盖用户 → 合并结果 |
| 8 | 工具冲突 | 同名内置工具 → MCP 工具以 namespaced 名注册，内置保留 |
| 9 | `aclose()` 清理 | 多个连接 → `aclose` 后 `connections` 为空、stack 已退出、stdio 子进程被终止 |
| 10 | bootstrap 单点失败隔离 | 一个 server connect 失败 → 其余 server + 内置工具仍可用 |
| 11 | `${ENV_VAR}` 展开 | `"${GITHUB_TOKEN}"` → 展开；缺失变量 → KeyError |
| 12 | OAuth header 注入 | 配置 auth → connect 时 `Authorization: Bearer` 进入 SSE headers |
| 13 | 并发隔离 | 两个并发 query 各自 registry/manager 互不污染 |

### 5.3 兼容性说明

- MCP 集成测试**不依赖**真实的 MCP 服务端（全部使用 mock session）
- `mcp` SDK 的 `ClientSession` / `list_tools()` / `call_tool()` 接口被 mock
- 不引入 vcrpy（无外部 HTTP 调用）

---

## 6. 实现步骤

| 步骤 | 内容 | 预估工时 | 产出文件 |
|------|------|----------|----------|
| **Step 0** | **前置重构**：`tools/registry.py` → 可实例化 `ToolRegistry`（query()/provider 改为传实例） | 1.5h | `tools/registry.py`, `query/loop.py`, `llm/*.py` |
| **Step 1** | `McpServerConfig` + `McpOAuthConfig` + `McpServerTransport` 数据模型 | 0.5h | `models.py` |
| **Step 2** | Transport 适配层（`_transport.py`）— `open_session` stdio/sse + auth header 注入 | 1h | `mcp/_transport.py` |
| **Step 3** | `McpSessionManager`（`client.py`）— AsyncExitStack 持有/connect/retry/filter/aclose | 2.5h | `mcp/client.py` |
| **Step 4** | 动态 Tool 注册（`registry.py`）— 类生成/注册/冲突检测/清理 | 3h | `mcp/registry.py` |
| **Step 5** | OAuth 认证（`auth.py`）— token manager/refresh/flow | 1.5h | `mcp/auth.py` |
| **Step 6** | 配置加载链（`config.py`）— `.mcp.json` 解析/`${ENV_VAR}` 展开/优先级合并 | 1h | `mcp/config.py` |
| **Step 7** | `query()` 集成 — bootstrap/同 task cleanup | 0.75h | `query/loop.py` |
| **Step 8** | CLI 集成 — `_load_mcp_servers()` + `_expand_env_vars()` | 0.5h | `cli.py` |
| **Step 9** | 公开 API 导出 | 0.25h | `__init__.py`, `mcp/__init__.py` |
| **Step 10** | 单元测试 (13 cases) | 2h | `tests/unit/test_mcp.py` |
| **Step 11** | Mock 集成测试 (13 cases) | 2.5h | `tests/integration/test_mcp.py` |
| **Step 12** | MCP 资源/Prompt 获取（可选，占位） | 0.5h | `mcp/resources.py` |
| **总计** | | **~19.5h (~2.5 天)** | |

> 工时较初版上调，主因：①新增 registry 可实例化重构（Step 0）；②连接生命周期改 AsyncExitStack 后 Step 3 的复杂度上升；③生命周期/隔离的回归测试增加。建议先做一个最小 spike（`AsyncExitStack` + 单个 stdio server + 一次 `call_tool`）验证生命周期跑通，再展开其余步骤。

---

## 7. 调用方示例

```python
import os
from agent_core import Agent, AgentConfig, McpServerConfig, McpServerTransport

# 通过配置文件 (推荐)
# .mcp.json:
# {
#     "mcpServers": {
#         "filesystem": {
#             "transport": "stdio",
#             "command": "npx",
#             "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
#         }
#     }
# }

# 通过代码注入
config = AgentConfig(
    model="anthropic/claude-sonnet-4-5",
    mcp_servers={
        "filesystem": McpServerConfig(
            transport=McpServerTransport.STDIO,
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        ),
        "github": McpServerConfig(
            transport=McpServerTransport.SSE,
            url="https://api.github.com/mcp",
            headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"},
        ),
    },
)

agent = Agent(config)

# LLM 会看到 tools 中包含:
# - Read, Write, Edit, Glob, Grep, Bash (内置)
# - mcp__filesystem__read_file, mcp__filesystem__write_file, ...
# - mcp__github__list_issues, mcp__github__create_pr, ...

async for msg in agent.stream("列出这个仓库的 open issues"):
    match msg:
        case ToolUse(tool_name="mcp__github__list_issues"):
            print("Agent 正在调用 GitHub MCP 工具...")
        case ToolResult(tool_name="mcp__github__list_issues", content=issues):
            print(f"获取到 issues: {issues}")
        case AssistantText(content=text):
            print(text, end="")
```

---

## 8. 与后续 Phase 的关系

| 后续 Item | 依赖关系 |
|-----------|----------|
| Item 7 (AgentDefinition) | AgentDefinition 可包含 `mcp_servers` 字段——子 Agent 独立连接或继承父级 MCP |
| Item 8 (子Agent 执行引擎) | 子 Agent 隔离环境需要独立的 `McpSessionManager` + 独立 `ToolRegistry`（query-scoped 实例已为此设计，不共享模块全局） |
| Item 9 (Skills) | MCP Resource/Prompt 可映射为 Skill 定义 |

---

## 9. 风险与注意事项

1. **`mcp` SDK 版本兼容性**：项目声明 `mcp>=1.0,<2`。API 可能在不同小版本间变化——实现时锁定具体 SDK API 签名（`ClientSession`, `list_tools()`, `call_tool()`, `stdio_client(StdioServerParameters)`, `sse_client()`）。注意 `stdio_client` 接收单个 params 对象而非散开 kwargs，须以实际 SDK 版本为准
2. **anyio cancel-scope / 同 task 约束（最高优先级）**：`stdio_client` / `sse_client` / `ClientSession` 基于 anyio，其 `async with` 的进入与退出**必须在同一个 task**。本设计用单个 `AsyncExitStack`（在 `query()` 主 task 内 enter，在同一 task 的 `finally` 内 `aclose`）满足该约束。**严禁**把 connect 放入后台 task / TaskGroup，或用会跨边界取消的外层 `asyncio.timeout` 包住整个 stack，否则触发 `RuntimeError: Attempted to exit cancel scope in a different task`
3. **连接必须存活到整个 query**：连接不能在 `connect()` 内部用 `async with ... return` 建立——那样会在返回时立即 `__aexit__`，导致 "能连上但调不动"。session 由 stack 持有，直到 `aclose()`
4. **stdio transport 子进程管理**：`stdio_client` 创建子进程，`aclose()` 经由 `AsyncExitStack` 的 `__aexit__` 终止它；集成测试需断言子进程已退出，避免僵尸进程
5. **registry 可实例化重构是前置依赖**：本计划假设 `tools/registry.py` 提供实例化的 `ToolRegistry`。若仍是模块全局 dict，必须先完成 Step 0 重构，否则并发 query 与子 Agent 隔离无法成立
6. **动态 Pydantic 模型**：`create_model()` 在复杂 JSON Schema（`$ref`, `oneOf`, `anyOf`, `type` 为数组）场景下 fallback 到 `raw: dict` 直通——刻意简化，后续可视需要增强
7. **工具 schema 注入 LLM 上下文**：大量 MCP 工具（100+）显著增加 token 消耗——后续可考虑按需注入（仅当 LLM 请求特定 MCP 服务器时展开）
8. **OAuth 集成范围**：当前 `OAuthTokenManager` 在 `connect()` 时解析一次 token 注入 header；尚未处理 SSE 长连接期间 token 过期后的 step-up / 重连刷新——列为后续增强
9. **并发安全**：所有 MCP 工具默认 `is_concurrency_safe=False`（串行执行）——若 server 支持并发，须由用户显式标记
