# Permissions API

The permission engine controls which tools the agent may execute. It is a
7-step matching chain evaluated for every tool call.

## `PermissionEngine`

```python
from agent_core import PermissionEngine

engine = PermissionEngine(
    rules=None,      # list[PermissionRule] | None → default_rules()
    mode="ask",      # PermissionMode
)

# Run for a specific tool
decision = await engine.check(
    tool_name="Bash",
    tool_input={"command": "ls"},
    callback=my_callback,         # PermissionCallback | None
    tool_is_read_only=False,
    tool_is_destructive=True,
)

print(decision.allowed)  # True / False
print(decision.reason)   # human-readable explanation
print(decision.source)   # "rule" | "mode" | "callback" | "default"
```

### 7-step matching chain

```
1. Exact tool_pattern match     (rule.action=allow/deny → return immediately)
2. Prefix match                 (pattern ending in *)
3. fnmatch wildcard             (supports * ? [])
4. path_constraint check        (if rule has path glob, validate tool_input)
5. PermissionMode fallback
   ├─ bypassPermissions → allowed  (source="mode")
   └─ acceptEdits + non-destructive → allowed
6. read_only safeguard           → allowed  (source="default")
7. Default ask → callback or deny destructive / allow read-only
```

Rules always take priority over mode. Even in `bypassPermissions`, a
`priority=0 deny` rule can still block a tool.

---

## `PermissionMode`

| Mode | Destructive tools | Non-destructive tools | Bypass rules? |
|------|------------------|-----------------------|---------------|
| `bypassPermissions` | allowed | allowed | No — rules (steps 1–4) still match first |
| `acceptEdits` | fall through → ask | allowed (step 5) | No |
| `ask` | callback or denied | callback or allowed (step 6) | No |

---

## `PermissionRule`

```python
from agent_core.permissions import PermissionRule

class PermissionRule(BaseModel):
    tool_pattern: str          # exact / "prefix*" / fnmatch wildcard
    action: Literal["allow", "deny", "ask"] = "ask"
    priority: int = 0          # lower = higher precedence
    path_constraint: str | None = None  # optional path glob constraint
    description: str = ""      # human-readable rule description
```

### Match patterns

| Pattern | Matches |
|---------|---------|
| `"Bash"` | Only `"Bash"` |
| `"File*"` | `"FileReadTool"`, `"FileWriteTool"` |
| `"mcp__*"` | `"mcp__github__list_issues"`, etc. |

### Built-in default rules

| tool_pattern | action | priority |
|-------------|--------|----------|
| `Read` | allow | 100 |
| `Glob` | allow | 100 |
| `Grep` | allow | 100 |
| `Bash` | ask | 100 |
| `Edit` | ask | 100 |
| `Write` | ask | 100 |

---

## Sub-agent permission inheritance

Three pure functions implement the contract — a child agent can **never** gain
privilege beyond its parent:

```python
from agent_core.permissions.inheritance import (
    compute_child_permission_mode,
    compute_child_allowed_tools,
    compute_child_disallowed_tools,
)

# Mode: child = min(parent, child_definition)
#   bypassPermissions > acceptEdits > ask
child_mode = compute_child_permission_mode("ask", None)         # → "ask"

# Tools: child = parent ∩ child_definition
child_allowed = compute_child_allowed_tools(
    frozenset(["Read", "Write", "Bash"]),
    ["Read", "Glob", "Grep"],
)  # → frozenset(["Read"])

# Disallowed: child = parent ∪ child_definition
child_disallowed = compute_child_disallowed_tools(
    frozenset(["Bash"]),
    ["Edit"],
)  # → frozenset(["Bash", "Edit"])
```

### Invariants

1. Child can **never** upgrade its permission mode
2. Child can **never** use a parent-disallowed tool
3. Child can **only** use tools allowed by both parent and child

---

## Bash AST safety checks

Before any Bash tool executes, `analyze_bash_command()` parses the command
with tree-sitter and detects dangerous patterns.

| Severity | Pattern | Detection |
|----------|---------|-----------|
| critical | `curl/wget ... | bash/sh` | Pipeline with downloader → shell |
| critical | `rm -rf /` or `rm -rf /*` | rm with recursive + root path |
| critical | redirect overwriting system files | `>` to `/dev/null` with source in `/etc/` |
| high | `chmod 777` on system paths | chmod 777 + `/usr/`, `/bin/`, `/etc/` prefix |
| high | `sudo` + dangerous command | sudo with rm/chmod/dd/mkfs |
| medium | `sudo` (generic) | any sudo usage |
| medium | `eval` + variable | eval with `$` references |

Unsafe commands are blocked — the tool returns `is_error=True` immediately,
without execution.
