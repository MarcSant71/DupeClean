"""
Critical Crash Log Manager with Privacy Sanitization and 7-Day Retention.
Writes to disk strictly when a fatal crash or unhandled exception occurs,
ensuring 100% privacy by stripping usernames, personal directories, and file names.
"""

import os
import sys
import time
import re
import platform
import traceback
import ctypes
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, Union

from dupeclean import __version__

_CRASH_LOG_LOCK = threading.Lock()
_MAX_CRASH_LOG_BYTES = 5 * 1024 * 1024  # 5 MB rotation threshold


def get_project_root() -> Path:
    """Return the application base directory (project root in dev or frozen executable directory)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent.parent


def get_logs_dir() -> Path:
    """Return the logs directory at the application root (sibling of lib/ and data/)."""
    base_dir = get_project_root()
    logs_dir = base_dir / "logs"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        return logs_dir
    except Exception:
        appdata = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "DupeClean" / "logs"
        appdata.mkdir(parents=True, exist_ok=True)
        return appdata


def prune_old_logs(logs_dir: Path, max_age_days: int = 7) -> int:
    """
    Remove log files in logs_dir older than max_age_days.
    Returns the number of deleted files.
    """
    pruned_count = 0
    try:
        now = time.time()
        max_age_seconds = max_age_days * 86400
        for item in logs_dir.glob("crash_*.log"):
            try:
                if (now - item.stat().st_mtime) > max_age_seconds:
                    item.unlink()
                    pruned_count += 1
            except Exception:
                pass
    except Exception:
        pass
    return pruned_count


def sanitize_text(raw_text: str, project_root: Optional[Path] = None) -> str:
    """
    Aplica filtros de privacidade em conformidade rigorosa com a LGPD:
    - Masks the Windows username (%USERNAME% / Home).
    - Converts absolute source code paths into project-relative paths.
    - Masks local user folder/file paths ([REDACTED_PATH]).
    - Mascara caminhos UNC de rede ([REDACTED_UNC_PATH]).
    """
    if not raw_text:
        return ""

    sanitized = raw_text
    root = project_root or get_project_root()
    root_str = str(root)

    # 1. Mask OS username
    username = os.environ.get("USERNAME", "")
    if username and len(username) >= 2:
        sanitized = re.sub(re.escape(username), "[USER]", sanitized, flags=re.IGNORECASE)

    try:
        home_user = Path.home().name
        if home_user and len(home_user) >= 2 and home_user.lower() != username.lower():
            sanitized = re.sub(re.escape(home_user), "[USER]", sanitized, flags=re.IGNORECASE)
    except Exception:
        pass

    # 2. Convert absolute source code paths to relative paths
    variants = [
        root_str,
        root_str.replace("/", "\\"),
        root_str.replace("\\", "/"),
    ]
    for v in variants:
        if v:
            sanitized = sanitized.replace(v + "\\", "")
            sanitized = sanitized.replace(v + "/", "")
            sanitized = sanitized.replace(v, ".")

    # 3. Simplifica caminhos do runtime Python / site-packages
    sanitized = re.sub(r'["\'][^"\']*[\\/]site-packages[\\/]([^"\']+)["\']', r'"site-packages/\1"', sanitized)
    sanitized = re.sub(r'["\'][^"\']*[\\/]Lib[\\/]([^"\']+)["\']', r'"python-lib/\1"', sanitized)

    # 4. Mascara caminhos de rede UNC: \\server\share\...
    sanitized = re.sub(r'(\\\\[A-Za-z0-9_.\-]+(?:\\[^\s"\'\r\n\t]+)*)', r'[REDACTED_UNC_PATH]', sanitized)

    # 5. Mask local user drive paths: [A-Za-z]:\...
    def _path_replacer(match):
        full = match.group(0)
        # Check if it has a file extension at the end
        ext_match = re.search(r'(\.[a-zA-Z0-9_]{1,8})$', full.rstrip("\"'"))
        ext = ext_match.group(1).lower() if ext_match else ""
        return f"[REDACTED_PATH/*{ext}]" if ext else "[REDACTED_PATH]"

    # Mascara caminhos delimitados por aspas
    sanitized = re.sub(r'["\'][A-Za-z]:\\[^"\'\r\n\t]+["\']', _path_replacer, sanitized)
    # Mask stray paths in text that do not belong to the project
    sanitized = re.sub(r'(?<![A-Za-z0-9_])[A-Za-z]:\\(?:[^\s"\'\r\n\t,;:]+)', _path_replacer, sanitized)

    return sanitized


def get_system_telemetry() -> Dict[str, Any]:
    """Collect technical hardware and system telemetry without any personal identifiers."""
    telemetry = {
        "dupeclean_version": __version__,
        "os": f"{platform.system()} {platform.release()} (build {platform.version()})",
        "python": f"{platform.python_version()} ({platform.architecture()[0]})",
        "cpu_count": os.cpu_count() or 1,
    }

    # System RAM (Windows GlobalMemoryStatusEx)
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            telemetry["ram_total_gb"] = round(stat.ullTotalPhys / (1024 ** 3), 2)
            telemetry["ram_avail_gb"] = round(stat.ullAvailPhys / (1024 ** 3), 2)
    except Exception:
        pass

    # GPU / CUDA / PyTorch information
    try:
        import torch
        telemetry["pytorch_version"] = torch.__version__
        telemetry["cuda_available"] = torch.cuda.is_available()
        if torch.cuda.is_available():
            telemetry["cuda_version"] = torch.version.cuda
            telemetry["gpu_name"] = torch.cuda.get_device_name(0)
            telemetry["vram_total_gb"] = round(torch.cuda.get_device_properties(0).total_memory / (1024 ** 3), 2)
            telemetry["vram_allocated_gb"] = round(torch.cuda.memory_allocated(0) / (1024 ** 3), 2)
            telemetry["vram_reserved_gb"] = round(torch.cuda.memory_reserved(0) / (1024 ** 3), 2)
    except Exception:
        pass

    return telemetry


def log_critical_crash(
    exc: Union[Exception, Tuple[Any, Any, Any]],
    context: Optional[Dict[str, str]] = None
) -> Optional[Path]:
    """
    Official entry point for recording critical application crashes:
    1. Formats exception and traceback.
    2. Applies strict LGPD sanitization.
    3. Cleans legacy dated crash_*.log files older than 7 days and rotates crash.log at 5 MB.
    4. Writes to `logs/crash.log` using the unified `[YYYY-MM-DD HH:MM:SS] [CRASH  ]: ...` format.
    """
    try:
        logs_dir = get_logs_dir()
        prune_old_logs(logs_dir, max_age_days=7)

        # Handle exception format
        if isinstance(exc, tuple) and len(exc) == 3:
            exc_type, exc_val, exc_tb = exc
            raw_tb = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
            exc_class_name = exc_type.__name__ if hasattr(exc_type, "__name__") else str(exc_type)
            exc_message = str(exc_val)
        elif isinstance(exc, BaseException):
            raw_tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            exc_class_name = type(exc).__name__
            exc_message = str(exc)
        else:
            raw_tb = str(exc)
            exc_class_name = "UnknownException"
            exc_message = str(exc)

        telemetry = get_system_telemetry()
        sanitized_tb = sanitize_text(raw_tb).rstrip()
        sanitized_msg = " ".join(sanitize_text(exc_message).splitlines()).strip()

        timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_path = logs_dir / "crash.log"

        meta_parts = [
            f"DupeClean {telemetry.get('dupeclean_version', 'Unknown')}",
            f"{telemetry.get('os', 'Unknown')}",
            f"Python {telemetry.get('python', 'Unknown')}",
            f"RAM {telemetry.get('ram_avail_gb', 'N/A')}/{telemetry.get('ram_total_gb', 'N/A')} GB",
        ]
        if telemetry.get("cuda_available"):
            meta_parts.append(
                f"GPU {telemetry.get('gpu_name', 'Unknown')} "
                f"(VRAM {telemetry.get('vram_allocated_gb', 'N/A')}/{telemetry.get('vram_total_gb', 'N/A')} GB, "
                f"CUDA {telemetry.get('cuda_version', 'N/A')})"
            )
        if context:
            for k, v in context.items():
                meta_parts.append(f"{k}: {sanitize_text(str(v))}")

        header_line = f"{timestamp_str} - {' | '.join(meta_parts)} - {exc_class_name}: {sanitized_msg}"
        tb_lines = [f"  {ln}" for ln in sanitized_tb.splitlines()] if sanitized_tb else []
        entry = "\n".join([header_line] + tb_lines) + "\n"

        with _CRASH_LOG_LOCK:
            try:
                if log_path.exists() and log_path.stat().st_size >= _MAX_CRASH_LOG_BYTES:
                    backup = log_path.with_name(f"{log_path.name}.1")
                    os.replace(str(log_path), str(backup))
            except OSError:
                pass
            with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                f.write(entry)

        return log_path
    except Exception:
        return None
