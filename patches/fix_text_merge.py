"""Fix _build_assistant_message: merge text fragments into a single block."""
import sys

path = r"C:\PU\src\agent_core\query\loop.py"
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# 1) Replace docstring
old_doc = '    """Build an assistant message from accumulated text and tool-use blocks.\n\n    Empty text fragments are skipped so that no ``{"type":"text","text":""}``\n    blocks are produced.\n    """'
new_doc = '    """Build an assistant message from accumulated text and tool-use blocks.\n\n    All streaming text fragments are joined into a *single* ``text`` block\n    so that the persisted content is compact and human-readable (no 69-block\n    messages from 69 streaming deltas).\n    """'

if old_doc in content:
    content = content.replace(old_doc, new_doc)
    print("[1/2] docstring replaced")
else:
    print("[1/2] docstring NOT FOUND - trying alternative match")
    # Try finding just a portion
    marker = 'Empty text fragments are skipped'
    idx = content.find(marker)
    print(f"  found at {idx}")

# 2) Replace the text loop
old_loop = """    for t in texts:
        stripped = t.strip()
        if stripped:
            content.append({"type": "text", "text": stripped})"""
new_loop = """    # Merge all streaming text fragments into a single text block
    merged = "".join(texts).strip()
    if merged:
        content.append({"type": "text", "text": merged})"""

if old_loop in content:
    content = content.replace(old_loop, new_loop)
    print("[2/2] text loop replaced")
else:
    print("[2/2] text loop NOT FOUND")
    # Find the function
    idx = content.find("def _build_assistant_message")
    print(f"  function at offset {idx}")
    if idx >= 0:
        print(content[idx:idx+500])

with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print("Done")
