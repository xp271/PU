# Tools API

Tools are the agent's interface to the outside world. Six built-in tools cover
filesystem operations; the `Tool` base class lets you add your own.

## `Tool` base class

```python
from agent_core import Tool, ToolCallResult, ToolUseContext

class Tool(ABC):
    name: ClassVar[str]                       # unique name (registry key)
    is_concurrency_safe: ClassVar[bool] = False
    is_read_only: ClassVar[bool] = False
    is_destructive: ClassVar[bool] = False

    @abstractmethod
    async def call(self, input: BaseModel, context: ToolUseContext) -> ToolCallResult: ...

    @abstractmethod
    def validate_input(self, input: dict[str, Any]) -> BaseModel: ...

    @classmethod
    def input_schema(cls) -> dict[str, Any]: ...     # Pydantic JSON Schema

    @classmethod
    def output_schema(cls) -> dict[str, Any]: ...    # ToolCallResult JSON Schema
```

### `ToolCallResult`

```python
class ToolCallResult(BaseModel):
    content: Any                          # tool output
    is_error: bool = False                # set True on failure
    truncated: bool = False               # output was truncated
    truncation_notice: str | None = None  # truncation description
    metadata: dict[str, Any] | None = None  # structured metadata (e.g. diff hunks)
```

### `ToolUseContext`

Execution context passed to every `call()`:

| Field | Type | Description |
|-------|------|-------------|
| `abort_signal` | `asyncio.Event` | External cancellation signal |
| `read_file_state` | `dict[str, Any]` | File read state cache |
| `messages` | `list[dict[str, Any]]` | Current conversation history |
| `agent_id` | `str` | Agent instance identifier |
| `workspace_root` | `str \| None` | Workspace root (security boundary) |
| `parent_config` | `Any` | Parent AgentConfig or None |
| `parent_trace_id` | `str` | Parent trace identifier |
| `agent_depth` | `int` | Nest depth (prevents infinite recursion) |

---

## Registry

```python
from agent_core import register_tool, get_tool, registered_tools

# Decorator
@register_tool
class MyTool(Tool):
    name: ClassVar[str] = "MyTool"
    ...

# Lookup (singleton instance, cached)
tool = get_tool("MyTool")

# List all registered
all_tools = registered_tools()  # -> dict[str, type[Tool]]
```

---

## Read (`name = "Read"`)

Reads a file. Read-only, concurrency-safe.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `file_path` | `str` | — | Absolute file path |
| `offset` | `int` | `0` | Start line (0-based), ≥ 0 |
| `limit` | `int` | `2000` | Max lines, 1–10000 |
| `max_bytes` | `int` | `500_000` | Byte read cap |

- Binary / device files detected and rejected
- Smart cache: unchanged files served from memory
- Multi-encoding fallback: utf-8 → gbk → shift_jis → euc_kr → big5 → latin-1

---

## Edit (`name = "Edit"`)

String replacement in a file. Not concurrency-safe, destructive.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `file_path` | `str` | — | Absolute file path |
| `old_string` | `str` | — | Exact text to replace (must be unique) |
| `new_string` | `str` | — | Replacement text (empty = delete) |

- File must have been Read first (in `read_file_state`)
- Stale detection: rejects edit if file changed since last read
- Atomic write via tempfile + `os.replace`

---

## Write (`name = "Write"`)

Write a file. Not concurrency-safe, destructive.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `file_path` | `str` | — | Absolute file path |
| `content` | `str` | — | Content to write |

- Creates parent directories automatically
- Atomic write, preserves original permissions

---

## Glob (`name = "Glob"`)

File pattern matching. Read-only, concurrency-safe.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `pattern` | `str` | — | Glob pattern (e.g. `"**/*.py"`) |
| `path` | `str` | `"."` | Search base directory |

- Workspace boundary checked, symlinks resolved
- Hidden files/dirs filtered by default
- Sorted by mtime descending, capped at 100 results

---

## Grep (`name = "Grep"`)

Content search via ripgrep. Read-only, concurrency-safe.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `pattern` | `str` | — | Regex pattern |
| `path` | `str` | `"."` | File or directory to search |
| `glob` | `str \| None` | `None` | File name filter (e.g. `"*.py"`) |
| `output_mode` | `"files_with_matches"` / `"content"` / `"count"` | `"files_with_matches"` | Output format |
| `head_limit` | `int` | `100` | Max output lines (1–1000) |
| `offset` | `int` | `0` | Skip first N results |
| `context_around` | `int` | `0` | Context lines (-C, 0–10) |
| `ignore_case` | `bool` | `False` | Case insensitive (-i) |
| `multiline` | `bool` | `False` | Multiline mode |

- Calls system `rg --json`, parses JSON line output
- 30-second timeout, 100 KB output cap

---

## Bash (`name = "Bash"`)

Execute a shell command. Not concurrency-safe, destructive.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `command` | `str` | — | Shell command |
| `timeout` | `float` | `120.0` | Timeout seconds (1–600) |
| `working_dir` | `str \| None` | `None` | Working directory |

- Triple termination: normal / timeout (SIGKILL) / abort_signal
- stdout + stderr each capped at 100 KB
- Non-zero exit → `is_error=True` with exit code

---

## AgentTool & SkillTool

Two special tools registered automatically:

- **AgentTool** (`name = "Agent"`): spawns child agents. See [Sub-Agent guide](../guides/subagent.md).
- **SkillTool** (`name = "Skill"`): invokes reusable prompt templates. See [Skills guide](../guides/skills.md).
