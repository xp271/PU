"""Fix agent.py: handle FlushMessage mid-turn persistence boundary."""
path = r"C:\PU\src\agent_core\agent.py"
with open(path, "r", encoding="utf-8") as f:
    content = f.read()

old = """            async for message in stream:
                # Thread this turn's full history back into the session so the
                # next prompt continues the conversation.
                if session is not None and message.type == "done":
                    await session.flush_new_messages(message.final_messages)
                    session.record_turn_result(message.final_messages)
                # \xa7Item 12: bypass-record every message (never mutates it).
                if recorder is not None:
                    await recorder.record_message(message)  # type: ignore[attr-defined]
                yield message"""

new = """            async for message in stream:
                if session is not None:
                    if message.type == "flush":
                        # Mid-turn persistence boundary: flush accumulated
                        # messages to JSONL *before* tool execution so that
                        # a kill during tool run loses at most this turn.
                        await session.flush_new_messages(message.messages)
                    elif message.type == "done":
                        # Terminal turn: flush remaining messages and record
                        # the full history into session state for the next
                        # prompt.
                        await session.flush_new_messages(message.final_messages)
                        session.record_turn_result(message.final_messages)
                # \xa7Item 12: bypass-record every message (never mutates it).
                if recorder is not None:
                    await recorder.record_message(message)  # type: ignore[attr-defined]
                yield message"""

assert old in content, f"old not found"
content = content.replace(old, new)

with open(path, "w", encoding="utf-8") as f:
    f.write(content)

print("OK: agent.py handles FlushMessage")
