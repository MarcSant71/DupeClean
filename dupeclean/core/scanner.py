"""
Rastreador de arquivos de alto desempenho (Filesystem Crawler).
Recursively scans directories using os.scandir with metadata caching
nativos do Win32 (WIN32_FIND_DATA), eliminando roundtrips de rede via SMB/iSCSI.
"""

import os
import time
import ctypes
from pathlib import Path
from typing import List, Set, Callable, Optional, Dict, Tuple
from dupeclean.core.models import ScanPath, FileRecord, ScanMode

# Cache de capacidades de volume por letra de unidade (ex: 'C:\\' -> ('NTFS', True))
_VOLUME_CAPS_CACHE: Dict[str, Tuple[str, bool]] = {}


def get_volume_capabilities(path: Path) -> Tuple[str, bool]:
    """
    Detecta o sistema de arquivos do volume (ex: 'NTFS', 'ReFS', 'FAT32', 'exFAT').
    Returns (fs_name, supports_file_id). Cached in memory for zero overhead.
    """
    try:
        drive = os.path.splitdrive(os.path.abspath(str(path)))[0].upper()
        if not drive:
            return "UNKNOWN", False
        root = drive + "\\"
        if root in _VOLUME_CAPS_CACHE:
            return _VOLUME_CAPS_CACHE[root]

        fs_name_buf = ctypes.create_unicode_buffer(256)
        vol_name_buf = ctypes.create_unicode_buffer(256)
        serial = ctypes.c_ulong()
        max_comp_len = ctypes.c_ulong()
        flags = ctypes.c_ulong()
        res = ctypes.windll.kernel32.GetVolumeInformationW(
            root, vol_name_buf, 256, ctypes.byref(serial),
            ctypes.byref(max_comp_len), ctypes.byref(flags), fs_name_buf, 256
        )
        if res:
            fs_name = fs_name_buf.value.upper()
            supports_file_id = fs_name in ("NTFS", "REFS")
            res_tuple = (fs_name, supports_file_id)
            _VOLUME_CAPS_CACHE[root] = res_tuple
            return res_tuple
    except Exception:
        pass
    return "UNKNOWN", False

# Supported extensions
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif",
    ".gif", ".ico", ".heic", ".heif", ".avif",
    ".raw", ".cr2", ".nef", ".arw", ".dng", ".orf", ".rw2"
}

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".ts", ".m2ts", ".mts", ".vob", ".mpg", ".mpeg",
    ".rmvb", ".rm", ".3gp", ".3g2", ".asf", ".f4v", ".divx",
    ".ogv", ".m2v", ".m4b", ".dav"
}


def crawl_directories(
    scan_paths: List[ScanPath],
    excluded_paths: List[Path],
    mode: ScanMode,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None
) -> List[FileRecord]:
    """
    Crawls all included directories, marking the 'is_reference' flag
    and respecting excluded paths. Uses high-throughput os.scandir.
    Emits continuous telemetry with live counts and active folder names.
    """
    records: List[FileRecord] = []
    seen_paths: Set[str] = set()

    # Normalize exclusions
    resolved_exclusions = [p.resolve() for p in excluded_paths if p.exists()]
    total_scanned = 0
    last_reported_count = 0
    last_report_time = 0.0

    def _crawl_folder(folder_path: Path, is_ref: bool):
        nonlocal total_scanned, last_reported_count, last_report_time
        if stop_check and stop_check():
            return

        now = time.time()
        if progress_callback and (now - last_report_time >= 0.08 or total_scanned - last_reported_count >= 100):
            last_reported_count = total_scanned
            last_report_time = now
            progress_callback(f"Scanning folders... ({total_scanned:,} files found)", total_scanned, 0)

        try:
            _, supports_file_id = get_volume_capabilities(folder_path)
            with os.scandir(folder_path) as it:
                subdirs: List[Path] = []
                for entry in it:
                    if stop_check and stop_check():
                        return

                    try:
                        # is_dir e is_file no Windows usam atributos em cache do WIN32_FIND_DATA
                        if entry.is_dir(follow_symlinks=False):
                            sub_p = Path(entry.path)
                            # Check exclusions only if excluded folders are configured
                            if resolved_exclusions and any(sub_p == exc or exc in sub_p.parents for exc in resolved_exclusions):
                                continue
                            subdirs.append(sub_p)

                        elif entry.is_file(follow_symlinks=False):
                            path_str = entry.path.lower()
                            if path_str in seen_paths:
                                continue

                            # Fast extension extraction (5x faster than os.path.splitext)
                            name = entry.name
                            dot_pos = name.rfind(".")
                            if dot_pos == -1:
                                continue
                            ext = name[dot_pos:].lower()

                            # Filtrar pelo modo
                            if mode == ScanMode.IMAGES and ext not in IMAGE_EXTENSIONS:
                                continue
                            elif mode == ScanMode.VIDEOS and ext not in VIDEO_EXTENSIONS:
                                continue

                            # entry.stat(follow_symlinks=False) avoids extra network RPC on Windows
                            st = entry.stat(follow_symlinks=False)
                            size = st.st_size

                            # Ignora arquivos de 0 bytes
                            if size == 0:
                                continue

                            vol_id = getattr(st, "st_dev", 0) or 0
                            f_id = (getattr(st, "st_ino", 0) or 0) if supports_file_id else 0

                            records.append(FileRecord(
                                path=Path(entry.path),
                                size=size,
                                mtime=st.st_mtime,
                                is_reference=is_ref,
                                name=entry.name,
                                ext=ext,
                                volume_id=vol_id,
                                file_id=f_id,
                            ))
                            seen_paths.add(path_str)
                            total_scanned += 1

                            now = time.time()
                            if progress_callback and (now - last_report_time >= 0.08 or total_scanned - last_reported_count >= 100):
                                last_reported_count = total_scanned
                                last_report_time = now
                                progress_callback(f"Scanning folders... ({total_scanned:,} files found)", total_scanned, 0)

                    except (OSError, PermissionError):
                        continue

            # Desce recursivamente nas subpastas
            for sub_p in subdirs:
                if stop_check and stop_check():
                    return
                _crawl_folder(sub_p, is_ref)

        except (OSError, PermissionError):
            return

    for sp in scan_paths:
        if stop_check and stop_check():
            return records
        root_dir = sp.path.resolve()
        if not root_dir.exists():
            continue

        _crawl_folder(root_dir, sp.is_reference)

    if stop_check and stop_check():
        return records

    if progress_callback:
        progress_callback(f"Scanning folders... ({total_scanned:,} files found)", total_scanned, 0)

    return records
