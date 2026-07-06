"""Fix: add FlushMessage to loop.py imports."""
path = r"C:\PU\src\agent_core\query\loop.py"
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# Find the import block
old = "    ErrorMessage,\n    ToolResult,"
new = "    ErrorMessage,\n    FlushMessage,\n    ToolResult,"
assert old in content, f"old import not found"
content = content.replace(old, new)

with open(path, "w", encoding="utf-8") as f:
    f.write(content)

print("OK: FlushMessage added to loop.py imports")
