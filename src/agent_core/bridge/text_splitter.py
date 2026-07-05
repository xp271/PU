"""字符边界长文本拆分。

微信客服 send_msg API 的 text.content 有限制（2048 字符），
不是 2048 字节。使用 2000 字符安全余量，在自然边界处拆分。
"""

from __future__ import annotations


def split_text(text: str, max_chars: int = 1900) -> list[str]:
    """按字符边界拆分长文本，确保每条不超过限制。

    优先在换行、句号、空格等自然断点处拆分，避免截断到词语中间。

    Args:
        text: 待拆分的文本
        max_chars: 单条最大字符数，默认 1900（QQ 限制 2000，留出安全余量）

    Returns:
        拆分后的文本片段列表
    """
    if len(text) <= max_chars:
        return [text] if text.strip() else []

    chunks: list[str] = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = min(start + max_chars, text_len)
        # 如果结尾在字符串末尾，直接添加
        if end >= text_len:
            chunk = text[start:].strip()
            if chunk:
                chunks.append(chunk)
            break

        # 向后搜索自然断点（最后 200 字符范围内）
        search_start = max(start, end - 200)
        natural_break = -1

        for sep in ("\n", "。", ". ", " "):
            pos = text.rfind(sep, search_start, end)
            if pos > search_start:
                # 对于换行，包含换行符在前一段
                if sep == "\n":
                    natural_break = pos + 1
                elif sep.endswith(" "):
                    natural_break = pos + len(sep)
                else:
                    natural_break = pos + 1
                break

        if natural_break > start:
            end = natural_break

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end

    return chunks
