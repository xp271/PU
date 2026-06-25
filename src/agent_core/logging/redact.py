"""
Redaction / truncation helpers for the conversation log layer.

Keeps sensitive material (API keys, PII) and oversized payloads out of the
log files according to :class:`LoggingConfig`.
"""

from __future__ import annotations

import copy
from typing import Any

from .config import LoggingConfig

__all__ = ["Redactor"]

_MASK = "***"


class Redactor:
    """Applies ``max_text_chars`` truncation and ``redact_keys`` masking."""

    def __init__(self, config: LoggingConfig) -> None:
        self._max = config.max_text_chars
        self._keys = {k.lower() for k in config.redact_keys}

    # -- text --------------------------------------------------------------

    def text(self, s: str) -> str:
        """Truncate *s* to ``max_text_chars`` (0 = no limit), with a marker."""
        if self._max <= 0 or len(s) <= self._max:
            return s
        dropped = len(s) - self._max
        return s[: self._max] + f"…[+{dropped} chars]"

    # -- tool input --------------------------------------------------------

    def tool_input(self, data: dict[str, Any]) -> dict[str, Any]:
        """Deep-copy *data*, mask redacted keys, truncate long string values.

        The original dict is never mutated.
        """
        if not isinstance(data, dict):
            return data
        return self._scrub_dict(copy.deepcopy(data))

    def _scrub_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        for key in list(data.keys()):
            if isinstance(key, str) and key.lower() in self._keys:
                data[key] = _MASK
                continue
            data[key] = self._scrub_value(data[key])
        return data

    def _scrub_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            return self._scrub_dict(value)
        if isinstance(value, list):
            return [self._scrub_value(v) for v in value]
        if isinstance(value, str):
            return self.text(value)
        return value

    # -- tool result -------------------------------------------------------

    def tool_result(self, content: Any) -> Any:
        """Truncate string content; pass non-strings through unchanged."""
        if isinstance(content, str):
            return self.text(content)
        return content
