from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import aiofiles
from charset_normalizer import from_bytes


# Files at or below this size have their full text cached in read_file_state so
# that subsequent Read/Edit/Write can operate without touching disk. Larger
# files degrade gracefully: only the fingerprint is kept and content is re-read
# from disk on demand.
MAX_CACHE_BYTES = 256 * 1024


def build_state(
    path: Path,
    *,
    content: str | None,
    total_lines: int | None = None,
    offset: int = 0,
    limit: int = 0,
    byte_truncated: bool = False,
) -> dict[str, Any]:
    """Construct a read_file_state entry: fingerprint + optional full-text cache.

    content is stored only when it is the trustworthy full text of the file and
    within the cache size budget; pass None to record fingerprint-only state.
    """
    state = file_fingerprint(path)
    state.update(
        {
            "content": content,
            "total_lines": total_lines,
            "offset": offset,
            "limit": limit,
            "byte_truncated": byte_truncated,
        }
    )
    return state


async def async_read_bytes(path: Path) -> bytes:
    async with aiofiles.open(path, "rb") as f:
        return await f.read()


async def async_read_text(path: Path, encoding: str = "utf-8") -> str:
    async with aiofiles.open(path, encoding=encoding) as f:
        return await f.read()


async def async_atomic_write(
    path: Path, content: str, encoding: str = "utf-8"
) -> dict[str, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".write_", suffix=".tmp")
    os.close(fd)
    try:
        async with aiofiles.open(tmp_path, "w", encoding=encoding, newline="") as f:
            await f.write(content)
            await f.flush()
            os.fsync(f.fileno())
        if path.exists():
            shutil.copymode(str(path), tmp_path)
            if os.name == "nt":
                os.chmod(str(path), 0o666)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return file_fingerprint(path)


def file_fingerprint(path: Path) -> dict[str, int]:
    """Return a (mtime_ns, size) fingerprint used for stale detection.

    Uses st_mtime_ns (integer nanoseconds) instead of float st_mtime to avoid
    precision loss on filesystems with coarse mtime resolution, and pairs it
    with size as a second signal to catch same-second modifications.
    """
    st = path.stat()
    return {"mtime_ns": st.st_mtime_ns, "size": st.st_size}


def fingerprint_changed(path: Path, recorded: dict[str, Any]) -> bool:
    """True if the file's current fingerprint differs from the recorded one."""
    current = file_fingerprint(path)
    return (
        recorded.get("mtime_ns") != current["mtime_ns"]
        or recorded.get("size") != current["size"]
    )


def check_workspace(path: Path, context_workspace_root: str | None) -> None:
    if context_workspace_root is None:
        return
    workspace = Path(context_workspace_root).resolve()
    try:
        path.relative_to(workspace)
    except ValueError:
        raise PermissionError(
            f"Access denied: {path} is outside workspace {workspace}"
        )


def safe_decode_bytes(data: bytes, encoding: str = "utf-8") -> str:
    """Decode bytes to text, trying common encodings before giving up.

    Raises ValueError if the data looks binary (contains NUL after a decode
    attempt) or cannot be decoded by any candidate. latin-1 is tried last
    because it maps any byte 0-255 and never raises UnicodeDecodeError, so it
    must not mask a genuine binary file — hence the NUL check after every
    successful decode.
    """
    candidates = [encoding, "gbk", "shift_jis", "euc_kr", "big5", "latin-1"]
    for enc in candidates:
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        if "\x00" in text:
            raise ValueError("Cannot decode as text; likely binary")
        return text

    result = from_bytes(data).best()
    if result is not None:
        text = str(result)
        if "\x00" not in text:
            return text
    raise ValueError("Cannot decode as text; likely binary")
