"""Pure, reusable helpers shared across the statistics plug-in."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger("mirror")

# Binary size units, smallest to largest; index 0 (bytes) is never divided.
_BINARY_UNITS: list[str] = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]


def format_bytes(num_bytes: int) -> str:
    """Format a byte count as a human-readable string.

    Args:
        num_bytes(int): Size in bytes.

    Return:
        text(str): Human-readable size, e.g. "1.5 GiB", "512 B".
    """
    if num_bytes < 1024:
        return f"{num_bytes} B"

    size = float(num_bytes)
    for unit in _BINARY_UNITS[1:]:
        size /= 1024.0
        if size < 1024.0:
            return f"{size:.1f} {unit}"
    return f"{size:.1f} {_BINARY_UNITS[-1]}"


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write bytes to path atomically (tempfile in the same dir + os.replace).

    Mirrors mirror.py's own atomic writer so consumers never observe a
    partially-written file. Creates parent directories as needed. The final
    file is given the requested mode so a web server running as another user
    can read it.

    Args:
        path(Path): Destination file path.
        data(bytes): Payload to write.
        mode(int): Permission bits for the final file (default 0o644).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f"{path.name}.", suffix=".tmp", delete=False
    )
    tmp_path = Path(tmp_file.name)
    try:
        tmp_file.write(data)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
        tmp_file.close()
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        if not tmp_file.closed:
            tmp_file.close()
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str, mode: int = 0o644) -> None:
    """Write text (UTF-8) to path atomically. Thin wrapper over atomic_write_bytes.

    Args:
        path(Path): Destination file path.
        text(str): Text payload.
        mode(int): Permission bits for the final file (default 0o644).
    """
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)
