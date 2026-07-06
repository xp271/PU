"""Add FlushMessage to models.py + AgentMessage union."""
import sys

path = r"C:\PU\src\agent_core\models.py"
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# 1) Insert FlushMessage before CompactMessage
marker = "class CompactMessage(BaseModel):"
idx = content.find(marker)
assert idx >= 0, f"Marker {marker!r} not found"

flush_class = """

class FlushMessage(BaseModel):
    \"\"\"Mid-turn persistence boundary emitted right before tool execution.

    Carries the current message list so the session layer can flush
    accumulated messages to JSONL *before* tools run.  This creates a
    recoverable save-point at every tool-call boundary: if the process
    is killed during tool execution, the JSONL is complete up to the
    assistant message that requested the tool(s).
    \"\"\"

    type: Literal[\"flush\"] = \"flush\"
    messages: list[dict[str, Any]] = Field(
        description=\"Current message list at the tool-execution boundary.\",
    )

"""

content = content[:idx] + flush_class + content[idx:]

# 2) Update AgentMessage union
old_union = "AssistantText | ToolUse | ToolResult | ErrorMessage | DoneMessage | CompactMessage,"
new_union = "AssistantText | ToolUse | ToolResult | ErrorMessage | DoneMessage | FlushMessage | CompactMessage,"
assert old_union in content, "Union not found!"
content = content.replace(old_union, new_union)

with open(path, "w", encoding="utf-8") as f:
    f.write(content)

print("OK: FlushMessage added to models.py")
