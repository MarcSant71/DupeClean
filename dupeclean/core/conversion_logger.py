"""
Dedicated Local Conversion & Decode Error Logger (`logs/conversion.log`).
- Clean single-line format:
    YYYY-MM-DD HH:MM:SS - <file_path> - <error>
- Separate from `logs/crash.log` (fatal application crashes with LGPD redaction).
- Thread-safe, lazy-created (only written when an error/recovery occurs), and rotated at 5 MB.
"""

import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Union

from dupeclean.core.crash_logger import get_logs_dir

_LOG_LOCK = threading.Lock()
_MAX_LOG_BYTES = 5 * 1024 * 1024  # 5 MB rotation threshold


def get_conversion_log_path() -> Path:
    """Return the path to `logs/conversion.log` alongside `logs/crash.log`."""
    return get_logs_dir() / "conversion.log"


def _rotate_if_needed(log_path: Path) -> None:
    """Rotate `conversion.log` -> `conversion.log.1` when exceeding 5 MB."""
    try:
        if log_path.exists() and log_path.stat().st_size >= _MAX_LOG_BYTES:
            backup = log_path.with_name(f"{log_path.name}.1")
            os.replace(str(log_path), str(backup))
    except OSError:
        pass


def log_conversion_error(
    mode: str,
    stage: str,
    file_path: Union[str, Path],
    reason: str,
    level: str = "ERROR",
) -> None:
    """
    Append a single-line entry to `logs/conversion.log`:
      YYYY-MM-DD HH:MM:SS - <file_path> - <error>
    Never raises — logging failures must never interrupt a scan.
    """
    try:
        log_path = get_conversion_log_path()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        clean_reason = " ".join(str(reason).splitlines()).strip() or "Unknown error"
        if len(clean_reason) > 300:
            clean_reason = clean_reason[:297] + "..."
        line = f"{ts} - {file_path} - {clean_reason}\n"

        with _LOG_LOCK:
            _rotate_if_needed(log_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                f.write(line)
    except Exception:
        pass
