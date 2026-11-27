"""
Rastreador de arquivos de alto desempenho (Filesystem Crawler).
Recursively scans directories using os.scandir with metadata caching
nativos do Win32 (WIN32_FIND_DATA), eliminando roundtrips de rede via SMB/iSCSI,
com paralelismo adaptativo para árvores profundas e detecção SSD vs HDD.
"""

import os
import time
import ctypes
import threading
from collections import deque
from pathlib import Path
from typing import List, Set, Callable, Optional, Dict, Tuple
from dupeclean.core.models import ScanPath, FileRecord, ScanMode, format_size

# Cache de capacidades de volume por letra de unidade (ex: 'C:\\' -> ('NTFS', True))
_VOLUME_CAPS_CACHE: Dict[str, Tuple[str, bool]] = {}
# Cache de tipo de armazenamento por letra de unidade ('ssd', 'hdd', 'iscsi', 'network', 'unknown')
_DRIVE_STORAGE_KIND_CACHE: Dict[str, str] = {}
# Cache de tipo de armazenamento por letra de unidade (True=SSD/NVMe, False=HDD, None=iSCSI/Rede/Desconhecido)
_DRIVE_SSD_CACHE: Dict[str, Optional[bool]] = {}
_CACHE_LOCK = threading.Lock()


def _extract_drive_letter(path: Path) -> str:
    """Fast drive-letter extraction avoiding os.path.abspath() syscall when path is already rooted."""
    p_str = str(path)
    drive = os.path.splitdrive(p_str)[0].upper()
    if drive:
        return drive
    return os.path.splitdrive(os.path.abspath(p_str))[0].upper()


def get_volume_capabilities(path: Path) -> Tuple[str, bool]:
    """
    Detecta o sistema de arquivos do volume (ex: 'NTFS', 'ReFS', 'FAT32', 'exFAT').
    Returns (fs_name, supports_file_id). Cached in memory for zero overhead.
    """
    try:
        drive = _extract_drive_letter(path)
        if not drive:
            return "UNKNOWN", False
        root = drive + "\\"
        cached = _VOLUME_CAPS_CACHE.get(root)
        if cached is not None:
            return cached

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
            with _CACHE_LOCK:
                _VOLUME_CAPS_CACHE[root] = res_tuple
            return res_tuple
    except Exception:
        pass
    return "UNKNOWN", False


def get_storage_kind(path: Path) -> str:
    """
    Classify the physical storage backing `path`:
    - 'ssd': local SSD / NVMe (no seek penalty)
    - 'hdd': local single-spindle mechanical HDD (seek penalty)
    - 'iscsi': networked block storage LUN (BusTypeiScsi — RAID/cache behind network RTT)
    - 'network': SMB / UNC network share
    - 'unknown': unable to determine (POSIX or restricted handle)
    Cached per drive letter. Requires no admin privileges on Windows.
    """
    if os.name != "nt":
        return "unknown"
    try:
        drive = _extract_drive_letter(path)
        if not drive:
            return "unknown"
        if drive.startswith("\\\\"):
            return "network"
        if not drive.endswith(":"):
            return "unknown"
        cached = _DRIVE_STORAGE_KIND_CACHE.get(drive)
        if cached is not None:
            return cached

        # Check drive type first (DRIVE_REMOTE = 4 -> network share)
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")
        if drive_type == 4:  # DRIVE_REMOTE
            with _CACHE_LOCK:
                _DRIVE_STORAGE_KIND_CACHE[drive] = "network"
                _DRIVE_SSD_CACHE[drive] = None
            return "network"

        import ctypes.wintypes as wt

        class _STORAGE_PROPERTY_QUERY(ctypes.Structure):
            _fields_ = [
                ("PropertyId", wt.DWORD),
                ("QueryType", wt.DWORD),
                ("AdditionalParameters", ctypes.c_byte * 1),
            ]

        class _DEVICE_SEEK_PENALTY_DESCRIPTOR(ctypes.Structure):
            _fields_ = [
                ("Version", wt.DWORD),
                ("Size", wt.DWORD),
                ("IncursSeekPenalty", ctypes.c_ubyte),
            ]

        k32 = ctypes.windll.kernel32
        k32.CreateFileW.restype = wt.HANDLE
        handle = k32.CreateFileW(
            f"\\\\.\\{drive}",
            0,  # Zero access flags: metadata query only (no elevation needed)
            0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE
            None,
            3,  # OPEN_EXISTING
            0,
            None,
        )
        INVALID_HANDLE_VALUE = wt.HANDLE(-1).value
        if not handle or handle == INVALID_HANDLE_VALUE:
            with _CACHE_LOCK:
                _DRIVE_STORAGE_KIND_CACHE[drive] = "unknown"
                _DRIVE_SSD_CACHE[drive] = None
            return "unknown"

        try:
            query = _STORAGE_PROPERTY_QUERY(PropertyId=7, QueryType=0)  # StorageDeviceSeekPenaltyProperty
            desc = _DEVICE_SEEK_PENALTY_DESCRIPTOR()
            bytes_ret = wt.DWORD(0)
            ok = k32.DeviceIoControl(
                handle,
                0x002D1400,  # IOCTL_STORAGE_QUERY_PROPERTY
                ctypes.byref(query),
                ctypes.sizeof(query),
                ctypes.byref(desc),
                ctypes.sizeof(desc),
                ctypes.byref(bytes_ret),
                None,
            )
            if ok:
                kind = "ssd" if desc.IncursSeekPenalty == 0 else "hdd"
            else:
                class _STORAGE_DEVICE_DESCRIPTOR_HDR(ctypes.Structure):
                    _fields_ = [
                        ("Version", wt.DWORD),
                        ("Size", wt.DWORD),
                        ("DeviceType", ctypes.c_ubyte),
                        ("DeviceTypeModifier", ctypes.c_ubyte),
                        ("RemovableMedia", ctypes.c_ubyte),
                        ("CommandQueueing", ctypes.c_ubyte),
                        ("VendorIdOffset", wt.DWORD),
                        ("ProductIdOffset", wt.DWORD),
                        ("ProductRevisionOffset", wt.DWORD),
                        ("SerialNumberOffset", wt.DWORD),
                        ("BusType", wt.DWORD),
                    ]

                q_dev = _STORAGE_PROPERTY_QUERY(PropertyId=0, QueryType=0)  # StorageDeviceProperty
                buf = (ctypes.c_ubyte * 512)()
                ok_dev = k32.DeviceIoControl(
                    handle,
                    0x002D1400,
                    ctypes.byref(q_dev),
                    ctypes.sizeof(q_dev),
                    buf,
                    ctypes.sizeof(buf),
                    ctypes.byref(bytes_ret),
                    None,
                )
                if ok_dev and bytes_ret.value >= ctypes.sizeof(_STORAGE_DEVICE_DESCRIPTOR_HDR):
                    bus_type = ctypes.cast(buf, ctypes.POINTER(_STORAGE_DEVICE_DESCRIPTOR_HDR)).contents.BusType
                    if bus_type == 9:  # BusTypeiScsi
                        kind = "iscsi"
                    elif bus_type == 17:  # BusTypeNvme
                        kind = "ssd"
                    else:
                        kind = "unknown"
                else:
                    kind = "unknown"
        finally:
            k32.CloseHandle(handle)

        ssd_bool: Optional[bool] = True if kind == "ssd" else (False if kind == "hdd" else None)
        with _CACHE_LOCK:
            _DRIVE_STORAGE_KIND_CACHE[drive] = kind
            _DRIVE_SSD_CACHE[drive] = ssd_bool
        return kind
    except Exception:
        return "unknown"


def is_solid_state_drive(path: Path) -> Optional[bool]:
    """
    Detect whether the physical drive backing `path` is a local SSD/NVMe (True),
    local mechanical HDD (False), or iSCSI/network/unknown storage (None).
    """
    kind = get_storage_kind(path)
    if kind == "ssd":
        return True
    if kind == "hdd":
        return False
    return None


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
    stop_check: Optional[Callable[[], bool]] = None,
    folder_counts_out: Optional[Dict[str, int]] = None,
) -> List[FileRecord]:
    """
    Crawls all included directories, marking the 'is_reference' flag
    and respecting excluded paths. Uses high-throughput os.scandir with adaptive
    multi-threaded directory traversal for deep folder trees and NAS/SMB shares.
    Emits continuous telemetry with live counts and total volume.
    """
    records: List[FileRecord] = []
    seen_paths: Set[str] = set()

    is_nt = os.name == "nt"
    norm_case = str.lower if is_nt else (lambda s: s)

    # Pre-normalize exclusions into O(1) exact set + prefix tuple (zero Path.parents allocations)
    excl_exact: Set[str] = set()
    excl_prefix_list: List[str] = []
    for p in excluded_paths:
        try:
            if not p.exists():
                continue
            ep_str = norm_case(os.path.normpath(str(p.resolve()))).rstrip("\\/")
            if ep_str:
                excl_exact.add(ep_str)
                excl_prefix_list.append(ep_str + os.sep)
        except OSError:
            continue
    excl_prefixes: Tuple[str, ...] = tuple(excl_prefix_list)
    has_exclusions = bool(excl_exact)

    is_img_mode = mode == ScanMode.IMAGES
    is_vid_mode = mode == ScanMode.VIDEOS

    total_scanned = 0
    total_bytes = 0
    last_reported_count = 0
    last_report_time = 0.0
    merge_lock = threading.Lock()

    def _maybe_report(force: bool = False) -> None:
        nonlocal last_reported_count, last_report_time
        if not progress_callback:
            return
        now = time.monotonic()
        if force or (now - last_report_time >= 0.08) or (total_scanned - last_reported_count >= 256):
            last_reported_count = total_scanned
            last_report_time = now
            progress_callback(
                f"Scanning folders... ({total_scanned:,} files, {format_size(total_bytes)} found)",
                total_scanned,
                0,
            )

    def _scan_one_dir(folder_str: str, is_ref: bool) -> Tuple[List[Tuple[str, FileRecord]], List[str]]:
        """Scan a single directory without holding shared locks; return (entries, subdirs)."""
        local_items: List[Tuple[str, FileRecord]] = []
        subdirs: List[str] = []
        if stop_check and stop_check():
            return local_items, subdirs

        try:
            with os.scandir(folder_str) as it:
                for entry in it:
                    if stop_check and stop_check():
                        return local_items, subdirs
                    try:
                        # is_dir / is_file on Windows use cached WIN32_FIND_DATA attributes
                        if entry.is_dir(follow_symlinks=False):
                            sub_str = entry.path
                            if has_exclusions:
                                sub_norm = norm_case(sub_str).rstrip("\\/")
                                if sub_norm in excl_exact or sub_norm.startswith(excl_prefixes):
                                    continue
                            subdirs.append(sub_str)

                        elif entry.is_file(follow_symlinks=False):
                            name = entry.name
                            dot_pos = name.rfind(".")
                            ext = name[dot_pos:].lower() if dot_pos != -1 else ""

                            # Filter by mode (EXACT mode includes extensionless files too)
                            if is_img_mode:
                                if not ext or ext not in IMAGE_EXTENSIONS:
                                    continue
                            elif is_vid_mode:
                                if not ext or ext not in VIDEO_EXTENSIONS:
                                    continue

                            # entry.stat(follow_symlinks=False) uses cached WIN32_FIND_DATA on Windows
                            st = entry.stat(follow_symlinks=False)
                            size = st.st_size
                            if size == 0:
                                continue

                            entry_path = entry.path
                            path_key = norm_case(entry_path)
                            # On Windows, DirEntry.stat always has st_dev=0, st_ino=0 (filled lazily in L2)
                            vol_id = 0 if is_nt else (getattr(st, "st_dev", 0) or 0)
                            f_id = 0 if is_nt else (getattr(st, "st_ino", 0) or 0)

                            local_items.append((
                                path_key,
                                FileRecord(
                                    path=Path(entry_path),
                                    size=size,
                                    mtime=st.st_mtime,
                                    is_reference=is_ref,
                                    name=name,
                                    ext=ext,
                                    volume_id=vol_id,
                                    file_id=f_id,
                                ),
                            ))
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            pass

        return local_items, subdirs

    def _merge_local(local_items: List[Tuple[str, FileRecord]]) -> None:
        nonlocal total_scanned, total_bytes
        if not local_items:
            return
        with merge_lock:
            for path_key, rec in local_items:
                if path_key in seen_paths:
                    continue
                seen_paths.add(path_key)
                records.append(rec)
                total_scanned += 1
                total_bytes += rec.size
            _maybe_report()

    def _crawl_tree_parallel(initial_subdirs: List[str], is_ref: bool) -> None:
        """Traverse nested subdirectories concurrently with a small worker pool."""
        num_workers = min(6, max(2, (os.cpu_count() or 4) // 2), len(initial_subdirs))
        pending: deque[str] = deque(initial_subdirs)
        active = 0
        cond = threading.Condition()

        def _worker():
            nonlocal active
            while True:
                if stop_check and stop_check():
                    with cond:
                        pending.clear()
                        cond.notify_all()
                    return
                with cond:
                    while not pending and active > 0:
                        if stop_check and stop_check():
                            return
                        cond.wait(timeout=0.05)
                    if not pending:
                        return
                    dir_str = pending.popleft()
                    active += 1

                try:
                    local_items, subdirs = _scan_one_dir(dir_str, is_ref)
                    if local_items:
                        _merge_local(local_items)
                finally:
                    with cond:
                        active -= 1
                        if subdirs and not (stop_check and stop_check()):
                            pending.extend(subdirs)
                        cond.notify_all()

        threads = [
            threading.Thread(target=_worker, daemon=True, name=f"CrawlWorker-{i}")
            for i in range(num_workers)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    for sp in scan_paths:
        if stop_check and stop_check():
            return records
        root_dir = sp.path.resolve()
        if not root_dir.exists():
            if folder_counts_out is not None:
                folder_counts_out[os.path.normpath(str(sp.path))] = 0
            continue

        before_count = len(records)
        root_str = str(root_dir)
        _maybe_report()

        # Scan root synchronously first: flat folders finish with zero thread-spawn overhead
        root_items, root_subdirs = _scan_one_dir(root_str, sp.is_reference)
        _merge_local(root_items)

        if root_subdirs and not (stop_check and stop_check()):
            if len(root_subdirs) == 1:
                # Follow single-branch chains synchronously until branching occurs
                stack = list(root_subdirs)
                while stack and not (stop_check and stop_check()):
                    cur = stack.pop()
                    items, subs = _scan_one_dir(cur, sp.is_reference)
                    _merge_local(items)
                    if len(subs) >= 2:
                        _crawl_tree_parallel(subs, sp.is_reference)
                    elif subs:
                        stack.extend(subs)
            else:
                _crawl_tree_parallel(root_subdirs, sp.is_reference)

        if folder_counts_out is not None and not (stop_check and stop_check()):
            folder_counts_out[os.path.normpath(str(sp.path))] = len(records) - before_count

    if stop_check and stop_check():
        return records

    _maybe_report(force=True)
    return records
