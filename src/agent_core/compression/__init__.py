"""
Auto-compression: LLM-based conversation summarization when the context
approaches the model window limit.

Compression is preferred over hard truncation — it preserves information
by summarizing older rounds instead of dropping them entirely.

Prompt templates can be customised by importing from ``.prompts`` or by
setting ``CompressionConfig.prompt_template``.
"""

from .summarizer import compress, CompressionAttempt, CompressionError
from .trigger import can_compress, _partition_rounds

__all__ = [
    "compress",
    "can_compress",
    "CompressionAttempt",
    "CompressionError",
    "_partition_rounds",
]
