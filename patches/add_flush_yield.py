"""Add FlushMessage yield in loop.py before tool execution."""
import sys

path = r"C:\PU\src\agent_core\query\loop.py"
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# Insert FlushMessage before tool execution
old = """            return

        # -- 2e. Execute tools & yield results ------------------------------
        tool_results = await run_tools("""

new = """            return

        # -- 2d5. Persistence boundary before tool execution ---------------
        # Flush the current message list (including the assistant message
        # with tool_use requests) to JSONL *before* tools run.  If the
        # process is killed during tool execution, the JSONL is complete
        # up to this boundary and can be recovered.
        yield FlushMessage(messages=list(messages))

        # -- 2e. Execute tools & yield results ------------------------------
        tool_results = await run_tools("""

assert old in content, f"old not found"
content = content.replace(old, new)

# Also need to import FlushMessage at top of loop.py
import_line = "from ..models import AssistantText, CompactMessage, DoneMessage, ErrorMessage, FlushMessage, ToolResult, ToolUse"
old_import = "from ..models import AssistantText, CompactMessage, DoneMessage, ErrorMessage, ToolResult, ToolUse"
if old_import in content:
    content = content.replace(old_import, import_line)
    print("[import] updated")
else:
    # Try to find what's there
    idx = content.find("from ..models import")
    if idx >= 0:
        end = content.find("\n", idx)
        print(f"[import] current: {content[idx:end]!r}")
    else:
        print("[import] NOT FOUND")

with open(path, "w", encoding="utf-8") as f:
    f.write(content)

print("OK: FlushMessage yield added to loop.py")
