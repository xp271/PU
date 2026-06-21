"""
Compression prompt templates.

Each template is a Python format string accepting the following keys:

**COMPRESSION_REQUEST**
  {preview_chars}      — tool_result truncation preview length
  {conversation_text}  — rendered conversation to summarise

**RETRY_FEEDBACK**
  {previous_errors}    — verification error messages from the previous attempt

**CONVERSATION_SUMMARY**
  {summary}            — the generated summary text

**TOOL_RESULT_TRUNCATED**
  {preview}            — the truncated text
  {original_len}       — original character count

**ROUND_ENTRY** (used per-message within conversation_text)
  {round_num}          — 1-based round index

Override any of these by setting ``CompressionConfig.prompt_template``
to a custom string.
"""

# ---------------------------------------------------------------------------
# Main compression request sent to the LLM
# ---------------------------------------------------------------------------

COMPRESSION_REQUEST = """<compression_request>
Please summarize the following conversation rounds. For each round, capture:
- The user's intent / goal
- Key tool calls (tool name + essential inputs)
- Key findings / results (first {preview_chars} chars of output)
- Any important decisions or state changes

<conversation_to_summarize>
{conversation_text}
</conversation_to_summarize>

Produce a concise summary. Focus on information that is still relevant to the ongoing task. Do not repeat information already present in the system prompt (date, git status, environment).
</compression_request>"""

# ---------------------------------------------------------------------------
# Retry feedback — appended to COMPRESSION_REQUEST on verification failure
# ---------------------------------------------------------------------------

RETRY_FEEDBACK = """<previous_attempt_feedback>
Your previous summarization was rejected because:
- {previous_errors}

Please fix these issues in your new summary.
</previous_attempt_feedback>"""

# ---------------------------------------------------------------------------
# Wrapper message injected into the conversation after compression
# ---------------------------------------------------------------------------

CONVERSATION_SUMMARY = """<conversation_summary>
{summary}
</conversation_summary>

The above is a summary of earlier conversation rounds. The following rounds are the most recent unsummarized conversation."""

# ---------------------------------------------------------------------------
# Tool result truncation marker
# ---------------------------------------------------------------------------

TOOL_RESULT_TRUNCATED = "{preview} [truncated, original ~{original_len} chars]"

# ---------------------------------------------------------------------------
# Per-round header
# ---------------------------------------------------------------------------

ROUND_HEADER = "\n[Round {round_num}]"
