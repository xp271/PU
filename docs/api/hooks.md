# Hooks

Hooks let you observe, block, or rewrite agent behaviour at four points in the
lifecycle. They are lightweight callables typed via `typing.Protocol`.

## Hook kinds

| Kind (`hook_kind`) | Fires | May |
|--------------------|-------|-----|
| `pre_tool_use` | before a tool runs (after bash_guard + permission) | continue / block / modify input |
| `post_tool_use` | after the tool returns, **before** result persistence | continue / block / modify output |
| `stop` | at the **top of each turn** (messages in a legal state) | terminate the loop early |
| `subagent_start` | before a sub-agent launches (`AgentTool.call`) | continue / block |

### Why `hook_kind` (not `isinstance`)

All four protocols share a single `async __call__`, so
`@runtime_checkable` + `isinstance` would match every hook against every
protocol. Classification therefore uses an **explicit** `hook_kind` ClassVar.
Subclass `HookBase` or simply expose the attribute:

```python
class AuditPre:
    hook_kind = "pre_tool_use"
    async def __call__(self, tool_name, tool_input, context):
        return HookContinue()
```

## Return types

| Type | Effect |
|------|--------|
| `HookContinue()` | proceed unchanged |
| `HookBlock(reason=..., message=...)` | refuse; tool yields an `is_error` result |
| `HookModify(new_result=...)` | replace tool **input** (pre) or **content** (post) |
| `StopResult(should_stop=..., message=...)` | stop hooks only; `True` ends the loop |

### Safety: modified input is re-checked

When a `pre_tool_use` hook returns `HookModify`, the modified input is
re-validated **and bash_guard + permission are re-run**. A hook cannot smuggle
a dangerous command past the safety checks.

### Ordering

`post_tool_use` runs on the **full** `ToolCallResult` *before* large-result
persistence/truncation, so `HookModify` operates on real content rather than a
truncated file pointer. The terminal `DoneMessage` is always emitted last; a
stop hook's `message` is yielded as `AssistantText` before it.

## Registry & isolation

`HookRegistry` is **per-query** (hung on `ToolUseContext.hook_registry`), never
a process-global singleton — concurrent agents and tests never cross-pollute.
The module-level `_default_hook_registry` is a read-only template; each
`query()` derives an instance via `copy_with_overrides(config.hooks)`.

```python
from agent_core import HookRegistry, HookBlock

class DenyBash:
    hook_kind = "pre_tool_use"
    async def __call__(self, tool_name, tool_input, context):
        if tool_name == "Bash":
            return HookBlock(reason="bash disabled in this session")
        return HookContinue()

hr = HookRegistry()
hr.register(DenyBash(), priority=0)   # lower priority runs first
```

Pass hooks via `AgentConfig.hooks` (applied automatically), or hand a registry
straight to `query(..., hook_registry=hr)`. Child agents inherit the parent
registry plus their `AgentDefinition.hooks` via `copy_with_overrides`.

### Priority

Lower number runs first; equal priority preserves registration order. Built-in
hooks use `priority=0`, user hooks default to `100`.

## Error handling

A hook that raises does not crash the loop: pre/post/subagent-start hooks treat
the exception as a block; stop hooks treat it as "no opinion" and continue.
