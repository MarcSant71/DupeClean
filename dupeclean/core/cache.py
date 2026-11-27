"""
Gerenciador de Cache Persistente (SQLite WAL).
Stores hashes and visual embeddings to avoid unnecessary reprocessing,
enabling near-instant rescans and full progress retention on cancellation.
"""

import json
import os
import queue
import shutil
import sqlite3
import sys
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple, Optional, Any, Callable, Set, Union
import xxhash

from dupeclean.core.models import FileRecord, ScanMode, ScanPath
from dupeclean.core.db_schema import (
    SchemaInitResult,
    ensure_database_schema,
    reset_database_preserving_settings,
)
from dupeclean.core.scanner import get_volume_capabilities, is_solid_state_drive


class AsyncBatchWriter:
    """Thread-safe asynchronous batch writer decoupling worker pipelines from SQLite WAL commits."""

    def __init__(
        self,
        flush_fn: Callable[[List[Any]], None],
        batch_size: int = 1024,
        flush_interval: float = 0.5,
        max_queue: int = 50000,
        name: str = "AsyncCacheWriter",
        session_context: Optional[Callable[[], Any]] = None,
    ):
        self._flush_fn = flush_fn
        self._session_context = session_context
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._max_queue = max_queue
        self._buf: deque = deque()
        self._cond = threading.Condition()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=name)
        self._thread.start()

    def add(self, entry: Any) -> None:
        with self._cond:
            while len(self._buf) >= self._max_queue and not self._stop_event.is_set():
                self._cond.wait(timeout=0.05)
            if self._stop_event.is_set():
                return
            self._buf.append(entry)
            if len(self._buf) == 1 or len(self._buf) >= self.batch_size:
                self._cond.notify()

    def add_batch(self, entries: Iterable[Any]) -> None:
        items = entries if isinstance(entries, (list, tuple)) else list(entries)
        if not items:
            return
        with self._cond:
            while len(self._buf) >= self._max_queue and not self._stop_event.is_set():
                self._cond.wait(timeout=0.05)
            if self._stop_event.is_set():
                return
            self._buf.extend(items)
            self._cond.notify()

    def _run_loop(self, flush_cb: Callable[[List[Any]], None]) -> None:
        batch: List[Any] = []
        last_flush = time.perf_counter()
        while True:
            with self._cond:
                while not self._buf and not self._stop_event.is_set():
                    if batch:
                        remaining = self.flush_interval - (time.perf_counter() - last_flush)
                        if remaining <= 0:
                            break
                        self._cond.wait(timeout=min(0.05, remaining))
                    else:
                        self._cond.wait(timeout=0.05)

                while self._buf and len(batch) < self.batch_size:
                    batch.append(self._buf.popleft())

                if len(self._buf) < self._max_queue:
                    self._cond.notify_all()

                done = self._stop_event.is_set() and not self._buf

            if not batch:
                if done:
                    break
                continue

            now = time.perf_counter()
            if len(batch) >= self.batch_size or (now - last_flush) >= self.flush_interval or done:
                try:
                    flush_cb(batch)
                except Exception:
                    pass
                batch.clear()
                last_flush = now
                if done:
                    break

        if batch:
            try:
                flush_cb(batch)
            except Exception:
                pass

    def _run(self) -> None:
        if self._session_context is not None:
            try:
                with self._session_context() as session_flush:
                    self._run_loop(session_flush)
                return
            except Exception:
                pass
        self._run_loop(self._flush_fn)

    def close(self) -> None:
        with self._cond:
            self._stop_event.set()
            self._cond.notify_all()
        if self._thread.is_alive():
            self._thread.join(timeout=60.0)


class _CacheProgressTracker:
    """Track monotonic, throttled progress across multiple cache tiers (L1/L2/L3)."""

    def __init__(
        self,
        callback: Optional[Callable[[str, int, int], None]],
        total_recs: int,
        base_label: str,
        min_records: int = 2000,
    ):
        self._callback = callback
        self.total = total_recs
        self.base_label = base_label
        self.enabled = bool(callback) and total_recs > min_records
        self._last_curr = 0
        self._last_emit = 0.0
        self._lock = threading.Lock()

    def emit_range(
        self,
        sub_label: str,
        start_frac: float,
        end_frac: float,
        done: int,
        sub_total: int,
        force: bool = False,
    ) -> None:
        """Emit strictly monotonic progress within [start_frac, end_frac] (capped < total)."""
        if not self.enabled or not self._callback or sub_total <= 0:
            return
        with self._lock:
            now = time.perf_counter()
            frac = min(1.0, max(0.0, done / sub_total))
            target = int((start_frac + frac * (end_frac - start_frac)) * self.total)
            target = max(1, min(self.total - 1, target))
            curr = max(self._last_curr, target)
            if not force and (now - self._last_emit < 0.08):
                return
            self._last_curr = curr
            self._last_emit = now
            self._callback(sub_label, curr, self.total)

    def finish(self) -> None:
        """Emit 100% of the cache stage only after all tiers complete."""
        if not self.enabled or not self._callback:
            return
        with self._lock:
            self._last_curr = self.total
            self._last_emit = time.perf_counter()
            self._callback(f"{self.base_label} ({self.total:,}/{self.total:,})...", self.total, self.total)


def _stat_record_file_id(rec: FileRecord) -> None:
    """Collect real volume_id and file_id via os.stat() assuming an already-validated volume."""
    if rec.volume_id > 0 and rec.file_id > 0:
        return
    try:
        st = os.stat(rec.path, follow_symlinks=False)
        rec.volume_id = getattr(st, "st_dev", 0) or 0
        rec.file_id = getattr(st, "st_ino", 0) or 0
    except OSError:
        pass


def populate_record_file_id(rec: FileRecord) -> None:
    """Coleta volume_id e file_id reais via os.stat() apenas em volumes NTFS/ReFS."""
    if rec.volume_id > 0 and rec.file_id > 0:
        return
    _, supports_fid = get_volume_capabilities(rec.path)
    if not supports_fid:
        return
    _stat_record_file_id(rec)


def compute_prefix_hash_from_bytes(data: bytes) -> str:
    """Compute the fast signature (Head 4KB + Tail 4KB or <=8KB total) from bytes in RAM."""
    if len(data) <= 8192:
        return xxhash.xxh64(data).hexdigest()
    h = xxhash.xxh64()
    h.update(data[:4096])
    h.update(data[-4096:])
    return h.hexdigest()


def compute_prefix_hash_from_stream(f, size: int) -> str:
    """Compute the fast signature (Head 4KB + Tail 4KB or <=8KB total) using an already-open stream."""
    if size <= 8192:
        data = f.read(8192)
        return xxhash.xxh64(data).hexdigest()
    head = f.read(4096)
    try:
        f.seek(-4096, os.SEEK_END)
        tail = f.read(4096)
    except OSError:
        tail = b""
    h = xxhash.xxh64()
    h.update(head)
    if tail:
        h.update(tail)
    return h.hexdigest()


def _open_random_binary(path: Path, size: int):
    """
    Open a file for small random-access reads (Head 4KB + Tail 4KB).
    Uses os.O_RANDOM on Windows so the Cache Manager does not trigger 64-256KB
    speculative read-ahead (critical on HDDs and iSCSI/network storage).
    """
    buffering = 4096 if size > 8192 else 8192
    if hasattr(os, "O_RANDOM"):
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | os.O_RANDOM
        fd = os.open(os.fspath(path), flags)
        try:
            return os.fdopen(fd, "rb", buffering=buffering)
        except Exception:
            os.close(fd)
            raise
    return open(path, "rb", buffering=buffering)


def compute_prefix_hash(path: Path, size: int) -> Optional[str]:
    """
    Compute the fast signature (Head 4KB + Tail 4KB or <=8KB total) via xxHash64.
    Used for Tier 3 (L3 Content Sieve) on exFAT/FAT32 volumes or cross-drive copies.
    """
    try:
        with _open_random_binary(path, size) as f:
            return compute_prefix_hash_from_stream(f, size)
    except (OSError, PermissionError):
        return None


def _compute_prefix_hash_and_fstat(rec: FileRecord) -> None:
    """
    Compute prefix_hash and, on NTFS/ReFS volumes, capture (volume_id, file_id)
    from the same open handle via os.fstat() to eliminate a second metadata pass.
    """
    try:
        with _open_random_binary(rec.path, rec.size) as f:
            rec.prefix_hash = compute_prefix_hash_from_stream(f, rec.size)
            if (rec.volume_id == 0 or rec.file_id == 0) and get_volume_capabilities(rec.path)[1]:
                try:
                    st = os.fstat(f.fileno())
                    rec.volume_id = getattr(st, "st_dev", 0) or 0
                    rec.file_id = getattr(st, "st_ino", 0) or 0
                except OSError:
                    pass
    except (OSError, PermissionError):
        rec.prefix_hash = None


def _get_drive_volume_id(path: Path, drive_vol_cache: Dict[str, int]) -> int:
    """
    Return st_dev for a Windows drive letter in 1 syscall per drive (cached).
    Returns 0 for UNC/non-Windows/inaccessible roots so callers fail safe.
    """
    if os.name != "nt":
        return 0
    drive = os.path.splitdrive(str(path))[0]
    if len(drive) != 2 or drive[1] != ":":
        return 0
    key = drive.upper()
    vol_id = drive_vol_cache.get(key)
    if vol_id is None:
        try:
            vol_id = getattr(os.stat(key + "\\"), "st_dev", 0) or 0
        except OSError:
            vol_id = 0
        drive_vol_cache[key] = vol_id
    return vol_id


def _select_cache_io_workers(records: Sequence[FileRecord]) -> int:
    """
    Choose thread concurrency based on underlying storage medium:
    - Local single-spindle mechanical HDD (seek penalty): 4 workers to avoid head thrashing
    - SSD / NVMe: up to 16 workers
    - iSCSI / network / unknown (high RTT RAID/NAS): up to 12 workers to keep queue depth full
    """
    cpu = os.cpu_count() or 4
    seen_drives: Set[str] = set()
    saw_ssd = False
    saw_unknown = False
    for r in records:
        drive = os.path.splitdrive(str(r.path))[0].upper()
        if drive in seen_drives:
            continue
        seen_drives.add(drive)
        ssd = is_solid_state_drive(r.path)
        if ssd is False:
            return 4
        if ssd is True:
            saw_ssd = True
        else:
            saw_unknown = True
        if len(seen_drives) >= 8:
            break
    if saw_ssd and not saw_unknown:
        return min(16, max(4, cpu))
    return min(12, max(6, cpu // 2))


def _migrate_legacy_db_files(src_db: Path, dst_db: Path) -> bool:
    """Move a legacy SQLite database and any WAL/SHM/journal sidecars to dst_db if dst_db does not exist."""
    if not src_db.exists() or dst_db.exists():
        return False
    shutil.move(str(src_db), str(dst_db))
    for ext in ("-wal", "-shm", "-journal"):
        old_aux = src_db.with_name(f"{src_db.name}{ext}")
        if old_aux.exists():
            shutil.move(str(old_aux), str(dst_db.with_name(f"{dst_db.name}{ext}")))
    return True


def get_default_cache_db_path() -> Path:
    """Return the centralized database.sqlite path (data/ folder), migrating legacy DB files automatically."""
    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).parent
    else:
        base_dir = Path(__file__).resolve().parent.parent.parent  # Raiz do DupeClean

    data_dir = base_dir / "data"
    old_dupeclean_db = data_dir / "DupeClean.db"
    old_cache = base_dir / "cache" / "cache.db"

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        new_db = data_dir / "database.sqlite"

        # Transparent migration from DupeClean.db (or legacy cache/cache.db) into database.sqlite
        if not _migrate_legacy_db_files(old_dupeclean_db, new_db):
            if _migrate_legacy_db_files(old_cache, new_db):
                try:
                    (base_dir / "cache").rmdir()
                except Exception:
                    pass

        return new_db
    except Exception:
        appdata = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "DupeClean" / "data"
        appdata.mkdir(parents=True, exist_ok=True)
        fallback_db = appdata / "database.sqlite"
        _migrate_legacy_db_files(appdata / "DupeClean.db", fallback_db)
        return fallback_db


class CacheManager:
    """
    Gerenciador de cache SQLite de alta performance, thread-safe e resiliente a bibliotecas massivas.
    Configured with WAL mode, mmap I/O, safe transactional commit/rollback, and strict connection closing.
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        status_callback: Optional[Callable[[str], None]] = None,
    ):
        self.db_path = db_path or get_default_cache_db_path()
        self.schema_result: SchemaInitResult = self._init_db(status_callback=status_callback)

    def _open_configured_connection(self) -> sqlite3.Connection:
        """Open and configure a SQLite WAL connection with high-throughput pragmas."""
        conn = sqlite3.connect(str(self.db_path), timeout=60.0)
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 60000;")  # 60 segundos de espera para locks
        conn.execute("PRAGMA cache_size = -64000;")   # 64 MB RAM page cache (eliminates repeated I/O)
        conn.execute("PRAGMA temp_store = MEMORY;")   # Temporary structures in RAM
        conn.execute("PRAGMA mmap_size = 268435456;") # 256 MB zero-copy memory mapping
        return conn

    @contextmanager
    def _get_connection(self):
        """
        Gerenciador de contexto que garante commit em sucesso, rollback em erro
        and mandatory connection closing to eliminate lock leaks on Windows.
        """
        conn = self._open_configured_connection()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_batch_writer(
        self,
        kind: str,
        batch_size: int = 1024,
        flush_interval: float = 0.5,
        max_queue: int = 50000,
        name: str = "AsyncCacheWriter",
    ) -> AsyncBatchWriter:
        """
        Create an AsyncBatchWriter that keeps a single SQLite WAL connection warm across
        batches (preserving the 64 MB page cache and avoiding per-batch PRAGMA churn),
        committing after every batch and closing cleanly on writer.close().
        """
        if kind == "exact":
            sql = """
                INSERT INTO cache_exact (path, size, mtime, prefix_hash, full_hash, volume_id, file_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size=excluded.size,
                    mtime=excluded.mtime,
                    prefix_hash=COALESCE(excluded.prefix_hash, cache_exact.prefix_hash),
                    full_hash=COALESCE(excluded.full_hash, cache_exact.full_hash),
                    volume_id=CASE WHEN excluded.volume_id != 0 THEN excluded.volume_id ELSE cache_exact.volume_id END,
                    file_id=CASE WHEN excluded.file_id != 0 THEN excluded.file_id ELSE cache_exact.file_id END;
            """
            fallback_fn = self.save_exact_batch
            def _norm(entries: List[Tuple]) -> List[Tuple]:
                return [e[:7] if len(e) >= 7 else (e[0], e[1], e[2], e[3], e[4], 0, 0) for e in entries]
        elif kind == "images":
            sql = """
                INSERT INTO cache_images (path, size, mtime, embedding, volume_id, file_id, prefix_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size=excluded.size,
                    mtime=excluded.mtime,
                    embedding=excluded.embedding,
                    volume_id=CASE WHEN excluded.volume_id != 0 THEN excluded.volume_id ELSE cache_images.volume_id END,
                    file_id=CASE WHEN excluded.file_id != 0 THEN excluded.file_id ELSE cache_images.file_id END,
                    prefix_hash=COALESCE(excluded.prefix_hash, cache_images.prefix_hash);
            """
            fallback_fn = self.save_image_batch
            def _norm(entries: List[Tuple]) -> List[Tuple]:
                return [e[:7] if len(e) >= 7 else (e[0], e[1], e[2], e[3], 0, 0, None) for e in entries]
        elif kind == "videos":
            sql = """
                INSERT INTO cache_videos (path, size, mtime, duration, embedding, volume_id, file_id, prefix_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size=excluded.size,
                    mtime=excluded.mtime,
                    duration=excluded.duration,
                    embedding=excluded.embedding,
                    volume_id=CASE WHEN excluded.volume_id != 0 THEN excluded.volume_id ELSE cache_videos.volume_id END,
                    file_id=CASE WHEN excluded.file_id != 0 THEN excluded.file_id ELSE cache_videos.file_id END,
                    prefix_hash=COALESCE(excluded.prefix_hash, cache_videos.prefix_hash);
            """
            fallback_fn = self.save_video_batch
            def _norm(entries: List[Tuple]) -> List[Tuple]:
                return [e[:8] if len(e) >= 8 else (e[0], e[1], e[2], e[3], e[4], 0, 0, None) for e in entries]
        else:
            raise ValueError(f"Unknown cache writer kind: {kind}")

        @contextmanager
        def _session():
            conn = self._open_configured_connection()
            try:
                def _flush(entries: List[Tuple]) -> None:
                    if not entries:
                        return
                    norm_entries = _norm(entries)
                    try:
                        conn.executemany(sql, norm_entries)
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                yield _flush
            finally:
                conn.close()

        return AsyncBatchWriter(
            fallback_fn,
            batch_size=batch_size,
            flush_interval=flush_interval,
            max_queue=max_queue,
            name=name,
            session_context=_session,
        )

    def _init_db(self, status_callback: Optional[Callable[[str], None]] = None) -> SchemaInitResult:
        """Validate and adapt the database.sqlite schema to the current version via db_schema."""
        return ensure_database_schema(self.db_path, progress_callback=status_callback)

    # -------------------------------------------------------------------------
    # 0. Application Settings and State Management (app_settings)
    # -------------------------------------------------------------------------

    def save_setting(self, key: str, value: Any):
        """Save a setting or state (JSON-serialized if complex)."""
        val_str = json.dumps(value) if not isinstance(value, str) else value
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?);",
                (key, val_str)
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        """Retrieve a setting from app_settings, deserializing JSON if possible."""
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT value FROM app_settings WHERE key = ?;", (key,))
            row = cursor.fetchone()
            if not row:
                return default
            val_str = row[0]
            try:
                return json.loads(val_str)
            except Exception:
                return val_str

    def estimate_file_count(self, scan_paths: List[ScanPath], mode: ScanMode) -> int:
        """
        Estima instantaneamente (em <1ms) a contagem de arquivos das pastas selecionadas
        using scan history and persistent SQLite metadata.
        Elimina completamente o modo indeterminado (barra que fica quicando) sem tocar no disco.
        """
        if not scan_paths:
            return 0

        # 1. Try retrieving from recent folder history in app_settings
        saved_counts = self.get_setting("last_folder_counts", {})
        total_estimate = 0
        missing_paths = []

        for sp in scan_paths:
            norm_key = str(sp.path.resolve()).rstrip("\\/")
            if norm_key in saved_counts and saved_counts[norm_key] > 0:
                total_estimate += saved_counts[norm_key]
            else:
                missing_paths.append(sp)

        if not missing_paths and total_estimate > 0:
            return total_estimate

        # 2. Fallback: Indexed prefix range count in SQLite (O(log N + K) covering PK index SEARCH)
        table = "cache_images" if mode == ScanMode.IMAGES else ("cache_videos" if mode == ScanMode.VIDEOS else "cache_exact")

        try:
            with self._get_connection() as conn:
                for sp in missing_paths:
                    low, high = self._prefix_range_bounds(str(sp.path.resolve()).rstrip("\\/") + "\\")
                    cursor = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE path >= ? AND path < ?;", (low, high))
                    count = cursor.fetchone()[0]
                    if count == 0:
                        alt_table = "cache_exact" if table != "cache_exact" else "cache_images"
                        cursor = conn.execute(f"SELECT COUNT(*) FROM {alt_table} WHERE path >= ? AND path < ?;", (low, high))
                        count = cursor.fetchone()[0]
                    total_estimate += count
        except Exception:
            pass

        return total_estimate

    @staticmethod
    def _prefix_range_bounds(prefix: str) -> Tuple[str, str]:
        """Return half-open [low, high) string bounds enabling O(log N + K) PK index SEARCH instead of LIKE SCAN."""
        return prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)

    def save_folder_counts(self, scan_paths: List[ScanPath], total_count: int):
        """Save the last observed file count for scanned folders."""
        if not scan_paths or total_count <= 0:
            return
        try:
            saved_counts = self.get_setting("last_folder_counts", {}) or {}
            if len(scan_paths) == 1:
                norm_key = str(scan_paths[0].path.resolve()).rstrip("\\/")
                saved_counts[norm_key] = total_count
            self.save_setting("last_folder_counts", saved_counts)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Shared Helpers (Tri-Hybrid Architecture L1 -> L2 Lazy -> L3 Parallel)
    # -------------------------------------------------------------------------

    def _table_has_rows(self, conn: sqlite3.Connection, table: str, condition: str = "1=1") -> bool:
        """Check in O(1) via index whether at least one eligible record exists in the table."""
        cur = conn.execute(f"SELECT 1 FROM {table} WHERE {condition} LIMIT 1;")
        return cur.fetchone() is not None

    def _populate_file_ids_parallel(
        self,
        records: List[FileRecord],
        stop_check: Optional[Callable[[], bool]] = None,
        assume_supported: bool = False,
        tracker: Optional[_CacheProgressTracker] = None,
        progress_range: Tuple[float, float] = (0.65, 0.80),
        progress_label: str = "Checking cache (file IDs)...",
    ) -> None:
        """Populate (volume_id, file_id) via os.stat() in parallel batches only where needed."""
        if assume_supported:
            to_stat = [r for r in records if r.volume_id == 0 or r.file_id == 0]
        else:
            to_stat = [
                r for r in records
                if (r.volume_id == 0 or r.file_id == 0) and get_volume_capabilities(r.path)[1]
            ]
        if not to_stat or (stop_check and stop_check()):
            return
        if len(to_stat) <= 256:
            for r in to_stat:
                if stop_check and stop_check():
                    return
                _stat_record_file_id(r)
            return

        # Sort by directory path to preserve spatial/MFT locality on HDDs and iSCSI
        to_stat.sort(key=lambda r: str(r.path))
        chunk_size = 2000
        chunks = [to_stat[i:i + chunk_size] for i in range(0, len(to_stat), chunk_size)]
        total_stat = len(to_stat)

        def _stat_chunk(batch: List[FileRecord]) -> int:
            for rec in batch:
                if stop_check and stop_check():
                    return 0
                _stat_record_file_id(rec)
            return len(batch)

        workers = _select_cache_io_workers(to_stat)
        done = 0
        s_frac, e_frac = progress_range
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for processed in pool.map(_stat_chunk, chunks):
                if stop_check and stop_check():
                    return
                done += processed
                if tracker:
                    tracker.emit_range(
                        f"{progress_label} ({done:,}/{total_stat:,})...",
                        s_frac, e_frac, done, total_stat,
                    )

    def _populate_prefix_hashes_parallel(
        self,
        records: List[FileRecord],
        stop_check: Optional[Callable[[], bool]] = None,
        tracker: Optional[_CacheProgressTracker] = None,
        progress_range: Tuple[float, float] = (0.91, 0.97),
        progress_label: str = "Checking cache signatures",
    ) -> None:
        """Compute prefix_hash (Head 4KB + Tail 4KB) and NTFS fstat in parallel for L3 candidates."""
        to_hash = [r for r in records if r.prefix_hash is None]
        if not to_hash or (stop_check and stop_check()):
            return

        def _hash_one(rec: FileRecord) -> None:
            if stop_check and stop_check():
                return
            _compute_prefix_hash_and_fstat(rec)

        if len(to_hash) <= 32:
            for r in to_hash:
                if stop_check and stop_check():
                    return
                _hash_one(r)
            return

        # Sort by directory path to preserve spatial locality on HDDs and iSCSI
        to_hash.sort(key=lambda r: str(r.path))
        chunk_size = 512
        chunks = [to_hash[i:i + chunk_size] for i in range(0, len(to_hash), chunk_size)]
        total_hash = len(to_hash)

        def _hash_chunk(batch: List[FileRecord]) -> int:
            for rec in batch:
                if stop_check and stop_check():
                    return 0
                _hash_one(rec)
            return len(batch)

        workers = _select_cache_io_workers(to_hash)
        done = 0
        s_frac, e_frac = progress_range
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for processed in pool.map(_hash_chunk, chunks):
                if stop_check and stop_check():
                    return
                done += processed
                if tracker:
                    tracker.emit_range(
                        f"{progress_label} ({done:,}/{total_hash:,})...",
                        s_frac, e_frac, done, total_hash,
                    )

    def _prefilter_known_sizes(
        self,
        conn: sqlite3.Connection,
        table: str,
        records: List[FileRecord],
        condition: str,
        tracker: Optional[_CacheProgressTracker] = None,
        progress_range: Tuple[float, float] = (0.55, 0.65),
        progress_label: str = "Checking cache",
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> Set[int]:
        """Query existing sizes in SQLite using idx_*_size before touching disk."""
        candidate_sizes = list({r.size for r in records})
        known_sizes: Set[int] = set()
        chunk_size = 900
        total_sizes = len(candidate_sizes)
        s_frac, e_frac = progress_range
        for i in range(0, total_sizes, chunk_size):
            if stop_check and stop_check():
                return known_sizes
            chunk_s = candidate_sizes[i:i + chunk_size]
            placeholders = ",".join("?" for _ in chunk_s)
            cur = conn.execute(
                f"SELECT DISTINCT size FROM {table} WHERE size IN ({placeholders});",
                chunk_s
            )
            known_sizes.update(row[0] for row in cur.fetchall())
            if tracker and total_sizes > 1800:
                done = min(i + chunk_size, total_sizes)
                tracker.emit_range(
                    f"{progress_label} ({len(records):,} pending)...",
                    s_frac, e_frac, done, total_sizes,
                )
        return known_sizes

    def _backfill_l1_file_ids(
        self,
        conn: sqlite3.Connection,
        table: str,
        backfill_recs: List[FileRecord],
        stop_check: Optional[Callable[[], bool]] = None,
        tracker: Optional[_CacheProgressTracker] = None,
    ) -> None:
        """Atualiza em lote (volume_id, file_id) para registros legados acertados no L1."""
        if not backfill_recs or (stop_check and stop_check()):
            return
        self._populate_file_ids_parallel(
            backfill_recs,
            stop_check=stop_check,
            tracker=tracker,
            progress_range=(0.50, 0.55),
            progress_label="Indexing cache file IDs",
        )
        updates = [
            (r.volume_id, r.file_id, str(r.path))
            for r in backfill_recs
            if r.volume_id > 0 and r.file_id > 0
        ]
        if updates:
            conn.executemany(
                f"UPDATE {table} SET volume_id = ?, file_id = ? WHERE path = ?;",
                updates
            )

    def _find_l2_candidates(
        self,
        conn: sqlite3.Connection,
        table: str,
        uncached: List[FileRecord],
        stop_check: Optional[Callable[[], bool]] = None,
        tracker: Optional[_CacheProgressTracker] = None,
    ) -> List[FileRecord]:
        """Select L1 misses eligible for NTFS/ReFS on volumes present in DB and collect File ID."""
        if not uncached or (stop_check and stop_check()):
            return []
        # Covering scan of partial index idx_*_fileid: skip entire drives absent from SQLite
        known_volumes = {
            row[0]
            for row in conn.execute(
                f"SELECT DISTINCT volume_id FROM {table} WHERE file_id != 0;"
            ).fetchall()
            if row[0]
        }
        if not known_volumes:
            return []

        drive_vol_cache: Dict[str, int] = {}
        drive_eligible_cache: Dict[str, bool] = {}
        vol_eligible: List[FileRecord] = []
        for r in uncached:
            if r.volume_id > 0 and r.file_id > 0:
                if r.volume_id in known_volumes:
                    vol_eligible.append(r)
                continue
            drive = os.path.splitdrive(str(r.path))[0].upper()
            if drive:
                eligible = drive_eligible_cache.get(drive)
                if eligible is None:
                    if not get_volume_capabilities(r.path)[1]:
                        eligible = False
                    else:
                        drive_vol = _get_drive_volume_id(r.path, drive_vol_cache)
                        eligible = (drive_vol == 0) or (drive_vol in known_volumes)
                    drive_eligible_cache[drive] = eligible
                if eligible:
                    vol_eligible.append(r)
            elif get_volume_capabilities(r.path)[1]:
                vol_eligible.append(r)

        if not vol_eligible or (stop_check and stop_check()):
            return []

        # Filter by sizes present in SQLite before issuing per-file os.stat() calls
        known_sizes = self._prefilter_known_sizes(
            conn, table, vol_eligible, "file_id != 0",
            tracker=tracker,
            progress_range=(0.55, 0.65),
            progress_label=tracker.base_label if tracker else "Checking cache",
            stop_check=stop_check,
        )
        if not known_sizes or (stop_check and stop_check()):
            return []
        candidates = [r for r in vol_eligible if r.size in known_sizes]
        if not candidates or (stop_check and stop_check()):
            return []
        self._populate_file_ids_parallel(
            candidates,
            stop_check=stop_check,
            assume_supported=True,
            tracker=tracker,
            progress_range=(0.65, 0.80),
            progress_label="Checking cache file IDs",
        )
        return [
            r for r in candidates
            if r.volume_id > 0 and r.file_id > 0 and r.volume_id in known_volumes
        ]

    _ALL_CACHE_TABLES = ("cache_exact", "cache_images", "cache_videos")

    @staticmethod
    def _is_volume_mounted(path_str: str, dir_cache: Dict[str, bool]) -> bool:
        """Return False if a drive letter is currently unplugged/unmounted, protecting offline external cache."""
        drive = os.path.splitdrive(path_str)[0]
        if not drive:
            return True
        root = drive + "\\"
        mounted = dir_cache.get(root)
        if mounted is None:
            mounted = os.path.isdir(root)
            dir_cache[root] = mounted
        return mounted

    @staticmethod
    def _path_exists_cached(path_str: str, dir_cache: Dict[str, bool]) -> bool:
        """Check path existence while caching parent directory lookups (turns renamed folders into 1 syscall)."""
        parent = os.path.dirname(path_str)
        if parent:
            parent_ok = dir_cache.get(parent)
            if parent_ok is None:
                parent_ok = os.path.isdir(parent)
                dir_cache[parent] = parent_ok
            if not parent_ok:
                return False
        return os.path.exists(path_str)

    def _cleanup_dead_paths(
        self,
        conn: sqlite3.Connection,
        table: str,
        stale_candidates: Set[str],
        active_paths: Set[str],
        dir_cache: Optional[Dict[str, bool]] = None,
    ) -> None:
        """Remove old paths from SQLite only if their volume is mounted and they no longer exist on disk."""
        if not stale_candidates:
            return
        d_cache = dir_cache if dir_cache is not None else {}
        to_delete = [
            (p,) for p in stale_candidates
            if p not in active_paths and self._is_volume_mounted(p, d_cache) and not self._path_exists_cached(p, d_cache)
        ]
        if to_delete:
            conn.executemany(f"DELETE FROM {table} WHERE path = ?;", to_delete)

    @staticmethod
    def _apply_l2_in_place_renames(
        conn: sqlite3.Connection,
        table: str,
        renames: List[Tuple[str, int, float, int, int, str]],
    ) -> None:
        """Update moved/renamed paths in-place without rewriting embedding BLOBs or issuing separate DELETEs."""
        if renames:
            conn.executemany(
                f"""
                UPDATE OR REPLACE {table}
                SET path = ?, size = ?, mtime = ?, volume_id = ?, file_id = ?
                WHERE path = ?;
                """,
                renames,
            )

    # -------------------------------------------------------------------------
    # 1. Cache para Modo EXATO (xxHash64)
    # -------------------------------------------------------------------------

    def get_exact_cache(
        self,
        records: List[FileRecord],
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None
    ) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
        """
        Return dictionary {path: (prefix_hash, full_hash)} for valid records.
        Executa arquitetura L1 -> L2 Lazy:
        - L1: Direct lookup by path + size + mtime (zero I/O), with legacy backfill.
        - L2: On-demand NTFS File ID (st_dev + st_ino) resilience for L1 misses only.
        Invalidates if size or mtime changed. Supports atomic cancellation via stop_check.
        """
        results: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        if not records:
            return results

        chunk_size = 900
        total_recs = len(records)
        backfill_recs: List[FileRecord] = []
        tracker = _CacheProgressTracker(progress_callback, total_recs, "Checking cache")

        with self._get_connection() as conn:
            # Cold-start fast-path: skip hundreds of IN-queries when cache table is empty
            if not self._table_has_rows(conn, "cache_exact"):
                tracker.emit_range(
                    f"Checking cache ({total_recs:,}/{total_recs:,})...",
                    0.0, 0.50, total_recs, total_recs,
                    force=True,
                )
                tracker.finish()
                return results

            # 1. Tier 1 (L1 - In-place Path): Fast direct path lookup
            for i in range(0, total_recs, chunk_size):
                if stop_check and stop_check():
                    return results
                chunk = records[i:i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                paths = [str(r.path) for r in chunk]
                cursor = conn.execute(
                    f"SELECT path, size, mtime, prefix_hash, full_hash, volume_id, file_id FROM cache_exact WHERE path IN ({placeholders})",
                    paths
                )
                record_map = {str(r.path): r for r in chunk}
                for row in cursor.fetchall():
                    c_path, c_size, c_mtime, p_hash, f_hash, c_vol_id, c_f_id = row
                    rec = record_map.get(c_path)
                    if rec and rec.size == c_size and abs(rec.mtime - c_mtime) < 0.001:
                        results[c_path] = (p_hash, f_hash)
                        if c_vol_id == 0 or c_f_id == 0:
                            backfill_recs.append(rec)

                done = min(i + chunk_size, total_recs)
                if done == total_recs or i % 2700 == 0:
                    tracker.emit_range(
                        f"Checking cache ({done:,}/{total_recs:,})...",
                        0.0, 0.50, done, total_recs,
                        force=(done == total_recs),
                    )

            if backfill_recs:
                self._backfill_l1_file_ids(conn, "cache_exact", backfill_recs, stop_check=stop_check, tracker=tracker)

            # 2. Tier 2 (L2 - NTFS File ID Lazy): Resilience for renamed/moved folders and files on the same volume
            uncached = [r for r in records if str(r.path) not in results]
            l2_candidates = self._find_l2_candidates(conn, "cache_exact", uncached, stop_check=stop_check, tracker=tracker)
            if l2_candidates and not (stop_check and stop_check()):
                l2_matches: List[Tuple[str, FileRecord, Optional[str], Optional[str]]] = []
                active_new_paths: Set[str] = set()
                total_l2 = len(l2_candidates)

                for i in range(0, total_l2, chunk_size):
                    if stop_check and stop_check():
                        return results
                    chunk = l2_candidates[i:i + chunk_size]
                    unique_fids = list({r.file_id for r in chunk})
                    placeholders = ",".join("?" for _ in unique_fids)
                    l2_map: Dict[Tuple[int, int], List[FileRecord]] = defaultdict(list)
                    for r in chunk:
                        l2_map[(r.file_id, r.volume_id)].append(r)

                    cursor = conn.execute(
                        f"SELECT path, size, mtime, prefix_hash, full_hash, volume_id, file_id FROM cache_exact WHERE file_id IN ({placeholders}) AND file_id != 0",
                        unique_fids
                    )
                    for row in cursor.fetchall():
                        c_path, c_size, c_mtime, p_hash, f_hash, c_vol_id, c_f_id = row
                        for rec in l2_map.get((c_f_id, c_vol_id), ()):
                            if rec.size == c_size and abs(rec.mtime - c_mtime) < 0.001:
                                new_path_str = str(rec.path)
                                results[new_path_str] = (p_hash, f_hash)
                                active_new_paths.add(new_path_str)
                                l2_matches.append((c_path, rec, p_hash, f_hash))

                    done_l2 = min(i + chunk_size, total_l2)
                    tracker.emit_range(
                        f"Checking cache moved files ({done_l2:,}/{total_l2:,})...",
                        0.80, 0.96, done_l2, total_l2,
                        force=(done_l2 == total_l2),
                    )

                dir_cache: Dict[str, bool] = {}
                renames: List[Tuple[str, int, float, int, int, str]] = []
                renamed_sources: Set[str] = set()
                upserts = []
                stale_candidates: Set[str] = set()
                for c_path, rec, p_hash, f_hash in l2_matches:
                    new_path_str = str(rec.path)
                    if (
                        c_path != new_path_str
                        and c_path not in active_new_paths
                        and c_path not in renamed_sources
                        and not self._path_exists_cached(c_path, dir_cache)
                    ):
                        renamed_sources.add(c_path)
                        renames.append((new_path_str, rec.size, rec.mtime, rec.volume_id, rec.file_id, c_path))
                    else:
                        upserts.append((new_path_str, rec.size, rec.mtime, p_hash, f_hash, rec.volume_id, rec.file_id))
                        if c_path != new_path_str:
                            stale_candidates.add(c_path)

                self._apply_l2_in_place_renames(conn, "cache_exact", renames)
                if upserts:
                    conn.executemany("""
                        INSERT INTO cache_exact (path, size, mtime, prefix_hash, full_hash, volume_id, file_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(path) DO UPDATE SET
                            size=excluded.size, mtime=excluded.mtime,
                            prefix_hash=COALESCE(excluded.prefix_hash, cache_exact.prefix_hash),
                            full_hash=COALESCE(excluded.full_hash, cache_exact.full_hash),
                            volume_id=excluded.volume_id, file_id=excluded.file_id;
                    """, upserts)
                if stale_candidates:
                    self._cleanup_dead_paths(conn, "cache_exact", stale_candidates, active_new_paths, dir_cache=dir_cache)
        tracker.finish()
        return results

    def save_exact_batch(self, entries: List[Tuple]):
        """Salva ou atualiza hashes em lote no cache exato."""
        if not entries:
            return
        norm_entries = []
        for e in entries:
            if len(e) >= 7:
                norm_entries.append(e[:7])
            else:
                norm_entries.append((e[0], e[1], e[2], e[3], e[4], 0, 0))

        with self._get_connection() as conn:
            conn.executemany("""
                INSERT INTO cache_exact (path, size, mtime, prefix_hash, full_hash, volume_id, file_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size=excluded.size,
                    mtime=excluded.mtime,
                    prefix_hash=COALESCE(excluded.prefix_hash, cache_exact.prefix_hash),
                    full_hash=COALESCE(excluded.full_hash, cache_exact.full_hash),
                    volume_id=CASE WHEN excluded.volume_id != 0 THEN excluded.volume_id ELSE cache_exact.volume_id END,
                    file_id=CASE WHEN excluded.file_id != 0 THEN excluded.file_id ELSE cache_exact.file_id END;
            """, norm_entries)

    # -------------------------------------------------------------------------
    # 2. Cache para Modo IMAGENS (Embeddings 2D-DCT FP16)
    # -------------------------------------------------------------------------

    def get_image_cache(
        self,
        records: List[FileRecord],
        expected_bytes: Optional[int] = None,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None
    ) -> Dict[str, bytes]:
        """
        Return dictionary {path: embedding_bytes} for images with valid cache.
        Valida tamanho em bytes do embedding para garantir compatibilidade estrutural.
        Runs tri-hybrid architecture L1 -> L2 Lazy -> L3 Parallel:
        - L1: Direct lookup by path + size + mtime (zero I/O), with legacy backfill.
        - L2: On-demand NTFS File ID (st_dev + st_ino) resilience for L1 misses.
        - L3: Content Sieve paralelo por tamanho + prefix_hash (xxHash64) em lote para exFAT/outros volumes.
        Suporta cancelamento imediato via stop_check.
        """
        results: Dict[str, bytes] = {}
        if not records:
            return results

        chunk_size = 900
        total_recs = len(records)
        backfill_recs: List[FileRecord] = []
        tracker = _CacheProgressTracker(progress_callback, total_recs, "Checking cache")

        with self._get_connection() as conn:
            # Cold-start fast-path: skip hundreds of IN-queries when cache table is empty
            if not self._table_has_rows(conn, "cache_images"):
                tracker.emit_range(
                    f"Checking cache ({total_recs:,}/{total_recs:,})...",
                    0.0, 0.50, total_recs, total_recs,
                    force=True,
                )
                tracker.finish()
                return results

            # 1. Tier 1 (L1 - In-place Path): Fast direct path lookup
            for i in range(0, total_recs, chunk_size):
                if stop_check and stop_check():
                    return results
                chunk = records[i:i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                paths = [str(r.path) for r in chunk]
                cursor = conn.execute(
                    f"SELECT path, size, mtime, embedding, volume_id, file_id, prefix_hash FROM cache_images WHERE path IN ({placeholders})",
                    paths
                )
                record_map = {str(r.path): r for r in chunk}
                for row in cursor.fetchall():
                    c_path, c_size, c_mtime, emb_blob, c_vol_id, c_f_id, c_prefix = row
                    rec = record_map.get(c_path)
                    if rec and rec.size == c_size and abs(rec.mtime - c_mtime) < 0.001:
                        is_tombstone = len(emb_blob) == 0
                        if is_tombstone or expected_bytes is None or len(emb_blob) == expected_bytes:
                            results[c_path] = emb_blob
                            if c_vol_id == 0 or c_f_id == 0:
                                backfill_recs.append(rec)

                done = min(i + chunk_size, total_recs)
                if done == total_recs or i % 2700 == 0:
                    tracker.emit_range(
                        f"Checking cache ({done:,}/{total_recs:,})...",
                        0.0, 0.50, done, total_recs,
                        force=(done == total_recs),
                    )

            if backfill_recs:
                self._backfill_l1_file_ids(conn, "cache_images", backfill_recs, stop_check=stop_check, tracker=tracker)

            # 2. Tier 2 (L2 - NTFS File ID Lazy): Resilience for moved folders and files on the same volume
            uncached = [r for r in records if str(r.path) not in results]
            l2_candidates = self._find_l2_candidates(conn, "cache_images", uncached, stop_check=stop_check, tracker=tracker)
            if l2_candidates and not (stop_check and stop_check()):
                l2_matches: List[Tuple[str, FileRecord, bytes, Optional[str]]] = []
                active_new_paths: Set[str] = set()
                total_l2 = len(l2_candidates)

                for i in range(0, total_l2, chunk_size):
                    if stop_check and stop_check():
                        return results
                    chunk = l2_candidates[i:i + chunk_size]
                    unique_fids = list({r.file_id for r in chunk})
                    placeholders = ",".join("?" for _ in unique_fids)
                    l2_map: Dict[Tuple[int, int], List[FileRecord]] = defaultdict(list)
                    for r in chunk:
                        l2_map[(r.file_id, r.volume_id)].append(r)

                    cursor = conn.execute(
                        f"SELECT path, size, mtime, embedding, volume_id, file_id, prefix_hash FROM cache_images WHERE file_id IN ({placeholders}) AND file_id != 0",
                        unique_fids
                    )
                    for row in cursor.fetchall():
                        c_path, c_size, c_mtime, emb_blob, c_vol_id, c_f_id, c_prefix = row
                        is_tombstone = len(emb_blob) == 0
                        if not is_tombstone and expected_bytes is not None and len(emb_blob) != expected_bytes:
                            continue
                        for rec in l2_map.get((c_f_id, c_vol_id), ()):
                            if rec.size == c_size and abs(rec.mtime - c_mtime) < 0.001:
                                new_path_str = str(rec.path)
                                results[new_path_str] = emb_blob
                                active_new_paths.add(new_path_str)
                                l2_matches.append((c_path, rec, emb_blob, c_prefix))

                    done_l2 = min(i + chunk_size, total_l2)
                    tracker.emit_range(
                        f"Checking cache moved files ({done_l2:,}/{total_l2:,})...",
                        0.80, 0.88, done_l2, total_l2,
                        force=(done_l2 == total_l2),
                    )

                dir_cache: Dict[str, bool] = {}
                renames: List[Tuple[str, int, float, int, int, str]] = []
                renamed_sources: Set[str] = set()
                upserts = []
                stale_candidates: Set[str] = set()
                for c_path, rec, emb_blob, c_prefix in l2_matches:
                    new_path_str = str(rec.path)
                    if (
                        c_path != new_path_str
                        and c_path not in active_new_paths
                        and c_path not in renamed_sources
                        and not self._path_exists_cached(c_path, dir_cache)
                    ):
                        renamed_sources.add(c_path)
                        renames.append((new_path_str, rec.size, rec.mtime, rec.volume_id, rec.file_id, c_path))
                    else:
                        upserts.append((new_path_str, rec.size, rec.mtime, emb_blob, rec.volume_id, rec.file_id, c_prefix))
                        if c_path != new_path_str:
                            stale_candidates.add(c_path)

                self._apply_l2_in_place_renames(conn, "cache_images", renames)
                if upserts:
                    conn.executemany("""
                        INSERT INTO cache_images (path, size, mtime, embedding, volume_id, file_id, prefix_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(path) DO UPDATE SET
                            size=excluded.size, mtime=excluded.mtime, embedding=excluded.embedding,
                            volume_id=excluded.volume_id, file_id=excluded.file_id,
                            prefix_hash=COALESCE(excluded.prefix_hash, cache_images.prefix_hash);
                    """, upserts)
                if stale_candidates:
                    self._cleanup_dead_paths(conn, "cache_images", stale_candidates, active_new_paths, dir_cache=dir_cache)

            # 3. Tier 3 (L3 - Parallel Batch Content Sieve): For exFAT, FAT32, or cross-volume copies
            uncached_l3 = [r for r in records if str(r.path) not in results]
            if uncached_l3 and not (stop_check and stop_check()) and self._table_has_rows(conn, "cache_images", "prefix_hash IS NOT NULL"):
                known_sizes = self._prefilter_known_sizes(
                    conn, "cache_images", uncached_l3, "prefix_hash IS NOT NULL",
                    tracker=tracker,
                    progress_range=(0.88, 0.91),
                    progress_label="Checking cache signatures",
                    stop_check=stop_check,
                )
                if known_sizes and not (stop_check and stop_check()):
                    sieve_candidates = [r for r in uncached_l3 if r.size in known_sizes]
                    self._populate_prefix_hashes_parallel(
                        sieve_candidates,
                        stop_check=stop_check,
                        tracker=tracker,
                        progress_range=(0.91, 0.97),
                        progress_label="Checking cache signatures",
                    )
                    hashed_candidates = [r for r in sieve_candidates if r.prefix_hash]
                    l3_matched: List[Tuple[FileRecord, bytes]] = []
                    stale_l3_candidates: Set[str] = set()
                    active_l3_paths: Set[str] = set()
                    total_l3 = len(hashed_candidates)

                    for i in range(0, total_l3, chunk_size):
                        if stop_check and stop_check():
                            return results
                        chunk = hashed_candidates[i:i + chunk_size]
                        unique_hashes = list({r.prefix_hash for r in chunk if r.prefix_hash})
                        if not unique_hashes:
                            continue
                        placeholders = ",".join("?" for _ in unique_hashes)
                        cursor = conn.execute(
                            f"SELECT path, size, prefix_hash, embedding FROM cache_images WHERE prefix_hash IN ({placeholders}) AND prefix_hash IS NOT NULL",
                            unique_hashes
                        )
                        match_map: Dict[Tuple[int, str], Tuple[str, bytes]] = {}
                        for r_path, r_size, r_phash, emb_blob in cursor.fetchall():
                            is_tombstone = len(emb_blob) == 0
                            if is_tombstone or expected_bytes is None or len(emb_blob) == expected_bytes:
                                match_map.setdefault((r_size, r_phash), (r_path, emb_blob))

                        for rec in chunk:
                            matched = match_map.get((rec.size, rec.prefix_hash))  # type: ignore[arg-type]
                            if matched is not None:
                                src_path, emb_blob = matched
                                new_path_str = str(rec.path)
                                results[new_path_str] = emb_blob
                                active_l3_paths.add(new_path_str)
                                if src_path != new_path_str:
                                    stale_l3_candidates.add(src_path)
                                l3_matched.append((rec, emb_blob))

                        done_l3 = min(i + chunk_size, total_l3)
                        tracker.emit_range(
                            f"Checking cache signatures ({done_l3:,}/{total_l3:,})...",
                            0.97, 0.99, done_l3, total_l3,
                            force=(done_l3 == total_l3),
                        )

                    if l3_matched:
                        self._populate_file_ids_parallel([r for r, _ in l3_matched], stop_check=stop_check)
                        l3_upserts = [
                            (str(rec.path), rec.size, rec.mtime, emb_blob, rec.volume_id, rec.file_id, rec.prefix_hash)
                            for rec, emb_blob in l3_matched
                        ]
                        conn.executemany("""
                            INSERT INTO cache_images (path, size, mtime, embedding, volume_id, file_id, prefix_hash)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(path) DO UPDATE SET
                                size=excluded.size, mtime=excluded.mtime, embedding=excluded.embedding,
                                volume_id=CASE WHEN excluded.volume_id != 0 THEN excluded.volume_id ELSE cache_images.volume_id END,
                                file_id=CASE WHEN excluded.file_id != 0 THEN excluded.file_id ELSE cache_images.file_id END,
                                prefix_hash=excluded.prefix_hash;
                        """, l3_upserts)
                        self._cleanup_dead_paths(conn, "cache_images", stale_l3_candidates, active_l3_paths)
        tracker.finish()
        return results

    def get_image_cache_l1_and_indexes(
        self,
        records: List[FileRecord],
        expected_bytes: Optional[int] = None,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[
        Dict[str, bytes],
        Dict[Tuple[int, int], Tuple[str, int, float, bytes, Optional[str]]],
        Dict[Tuple[int, str], Tuple[str, bytes]],
        Set[int],
        Set[int],
    ]:
        """
        Zero-disk-I/O Stage-2 cache lookup for the unified streaming image pipeline:
        1. L1 direct path lookup in SQLite (path + size + mtime).
        2. Preload lightweight in-memory L2 (volume_id, file_id) and L3 (size, prefix_hash)
           lookup indexes ONLY for sizes present in L1 misses — with ZERO per-file os.stat()
           and ZERO file open() calls during Stage 2.
        L2 and L3 checks then execute inline inside Stage 3's single open handle per file.
        """
        l1_results: Dict[str, bytes] = {}
        l2_index: Dict[Tuple[int, int], Tuple[str, int, float, bytes, Optional[str]]] = {}
        l3_index: Dict[Tuple[int, str], Tuple[str, bytes]] = {}
        l3_known_sizes: Set[int] = set()
        l2_known_volumes: Set[int] = set()

        if not records:
            return l1_results, l2_index, l3_index, l3_known_sizes, l2_known_volumes

        chunk_size = 900
        total_recs = len(records)
        tracker = _CacheProgressTracker(progress_callback, total_recs, "Checking cache")

        with self._get_connection() as conn:
            if not self._table_has_rows(conn, "cache_images"):
                tracker.finish()
                return l1_results, l2_index, l3_index, l3_known_sizes, l2_known_volumes

            # 1. Tier 1 (L1): Direct path lookup (pure SQLite PK lookup, zero file I/O)
            for i in range(0, total_recs, chunk_size):
                if stop_check and stop_check():
                    return l1_results, l2_index, l3_index, l3_known_sizes, l2_known_volumes
                chunk = records[i:i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                paths = [str(r.path) for r in chunk]
                cursor = conn.execute(
                    f"SELECT path, size, mtime, embedding, volume_id, file_id, prefix_hash FROM cache_images WHERE path IN ({placeholders})",
                    paths,
                )
                record_map = {str(r.path): r for r in chunk}
                for c_path, c_size, c_mtime, emb_blob, c_vol_id, c_f_id, c_prefix in cursor.fetchall():
                    rec = record_map.get(c_path)
                    if rec and rec.size == c_size and abs(rec.mtime - c_mtime) < 0.001:
                        is_tombstone = len(emb_blob) == 0
                        if is_tombstone or expected_bytes is None or len(emb_blob) == expected_bytes:
                            l1_results[c_path] = emb_blob
                            if c_prefix:
                                rec.prefix_hash = c_prefix
                                l3_index.setdefault((c_size, c_prefix), (c_path, emb_blob))
                                l3_known_sizes.add(c_size)
                            if c_vol_id > 0 and c_f_id > 0:
                                rec.volume_id = c_vol_id
                                rec.file_id = c_f_id

                done = min(i + chunk_size, total_recs)
                if done == total_recs or i % 2700 == 0:
                    tracker.emit_range(
                        f"Checking cache ({done:,}/{total_recs:,})...",
                        0.0, 0.75, done, total_recs,
                        force=(done == total_recs),
                    )

            # 2. Preload in-memory L2 & L3 lookup indexes for L1 misses (pure SQLite, zero file I/O)
            misses = [r for r in records if str(r.path) not in l1_results]
            if misses and not (stop_check and stop_check()):
                db_volumes = {
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT volume_id FROM cache_images WHERE file_id != 0;"
                    ).fetchall()
                    if row[0]
                }
                drive_vol_cache: Dict[str, int] = {}
                active_l2_vols: Set[int] = set()
                if db_volumes:
                    seen_drives: Set[str] = set()
                    has_unknown_vol = False
                    for r in misses:
                        drv = os.path.splitdrive(str(r.path))[0].upper()
                        if drv in seen_drives:
                            continue
                        seen_drives.add(drv)
                        if not get_volume_capabilities(r.path)[1]:
                            continue
                        d_vol = _get_drive_volume_id(r.path, drive_vol_cache)
                        if d_vol == 0:
                            has_unknown_vol = True
                        elif d_vol in db_volumes:
                            active_l2_vols.add(d_vol)
                    if has_unknown_vol:
                        active_l2_vols = set(db_volumes)

                miss_sizes = list({r.size for r in misses})
                total_sizes = len(miss_sizes)
                # Cap preloaded index rows to ~250k (~375 MB max RAM) for low-RAM systems
                max_preload_rows = 250_000
                loaded_rows = 0

                for i in range(0, total_sizes, chunk_size):
                    if stop_check and stop_check() or loaded_rows >= max_preload_rows:
                        break
                    chunk_s = miss_sizes[i:i + chunk_size]
                    placeholders = ",".join("?" for _ in chunk_s)
                    cursor = conn.execute(
                        f"SELECT path, size, mtime, volume_id, file_id, prefix_hash, embedding "
                        f"FROM cache_images WHERE size IN ({placeholders});",
                        chunk_s,
                    )
                    for c_path, c_size, c_mtime, c_vol_id, c_f_id, c_prefix, emb_blob in cursor.fetchall():
                        is_tombstone = len(emb_blob) == 0
                        if not is_tombstone and expected_bytes is not None and len(emb_blob) != expected_bytes:
                            continue
                        used = False
                        if c_f_id != 0 and c_vol_id in active_l2_vols:
                            if (c_vol_id, c_f_id) not in l2_index:
                                l2_index[(c_vol_id, c_f_id)] = (c_path, c_size, c_mtime, emb_blob, c_prefix)
                                l2_known_volumes.add(c_vol_id)
                                used = True
                        if c_prefix:
                            if (c_size, c_prefix) not in l3_index:
                                l3_index[(c_size, c_prefix)] = (c_path, emb_blob)
                                l3_known_sizes.add(c_size)
                                used = True
                        if used:
                            loaded_rows += 1
                            if loaded_rows >= max_preload_rows:
                                break

                    done_s = min(i + chunk_size, total_sizes)
                    if total_sizes > 1800 and (done_s == total_sizes or i % 2700 == 0):
                        tracker.emit_range(
                            f"Checking cache index ({done_s:,}/{total_sizes:,})...",
                            0.75, 0.99, done_s, total_sizes,
                            force=(done_s == total_sizes),
                        )

        tracker.finish()
        return l1_results, l2_index, l3_index, l3_known_sizes, l2_known_volumes

    def cleanup_stale_paths(self, table: str, stale_candidates: Set[str], active_paths: Set[str]) -> None:
        """Remove dead/moved source paths from a cache table after inline L2/L3 relocations."""
        if not stale_candidates:
            return
        with self._get_connection() as conn:
            self._cleanup_dead_paths(conn, table, stale_candidates, active_paths)

    def save_image_batch(self, entries: List[Tuple]):
        """Salva lote de embeddings de imagem no cache."""
        if not entries:
            return
        norm_entries = []
        for e in entries:
            if len(e) >= 7:
                norm_entries.append(e[:7])
            else:
                norm_entries.append((e[0], e[1], e[2], e[3], 0, 0, None))

        with self._get_connection() as conn:
            conn.executemany("""
                INSERT INTO cache_images (path, size, mtime, embedding, volume_id, file_id, prefix_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size=excluded.size,
                    mtime=excluded.mtime,
                    embedding=excluded.embedding,
                    volume_id=CASE WHEN excluded.volume_id != 0 THEN excluded.volume_id ELSE cache_images.volume_id END,
                    file_id=CASE WHEN excluded.file_id != 0 THEN excluded.file_id ELSE cache_images.file_id END,
                    prefix_hash=COALESCE(excluded.prefix_hash, cache_images.prefix_hash);
            """, norm_entries)

    # -------------------------------------------------------------------------
    # 3. Cache for VIDEOS Mode (Duration + FP16 Composite Embeddings)
    # -------------------------------------------------------------------------

    def get_video_cache(
        self,
        records: List[FileRecord],
        expected_bytes: Optional[int] = None,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None
    ) -> Dict[str, Tuple[float, bytes]]:
        """
        Return dictionary {path: (duration, embedding_bytes)} for valid cached videos.
        Valida tamanho em bytes do embedding para garantir compatibilidade estrutural.
        Runs tri-hybrid architecture L1 -> L2 Lazy -> L3 Parallel:
        - L1: Direct lookup by path + size + mtime (zero I/O), with legacy backfill.
        - L2: On-demand NTFS File ID (st_dev + st_ino) resilience for L1 misses.
        - L3: Content Sieve paralelo por tamanho + prefix_hash (xxHash64) em lote para exFAT/outros volumes.
        Suporta cancelamento imediato via stop_check.
        """
        results: Dict[str, Tuple[float, bytes]] = {}
        if not records:
            return results

        chunk_size = 900
        total_recs = len(records)
        backfill_recs: List[FileRecord] = []
        tracker = _CacheProgressTracker(progress_callback, total_recs, "Checking video cache")

        with self._get_connection() as conn:
            # Cold-start fast-path: skip hundreds of IN-queries when cache table is empty
            if not self._table_has_rows(conn, "cache_videos"):
                tracker.emit_range(
                    f"Checking video cache ({total_recs:,}/{total_recs:,})...",
                    0.0, 0.50, total_recs, total_recs,
                    force=True,
                )
                tracker.finish()
                return results

            # 1. Tier 1 (L1 - In-place Path): Fast direct path lookup
            for i in range(0, total_recs, chunk_size):
                if stop_check and stop_check():
                    return results
                chunk = records[i:i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                paths = [str(r.path) for r in chunk]
                cursor = conn.execute(
                    f"SELECT path, size, mtime, duration, embedding, volume_id, file_id, prefix_hash FROM cache_videos WHERE path IN ({placeholders})",
                    paths
                )
                record_map = {str(r.path): r for r in chunk}
                for row in cursor.fetchall():
                    c_path, c_size, c_mtime, dur, emb_blob, c_vol_id, c_f_id, c_prefix = row
                    rec = record_map.get(c_path)
                    if rec and rec.size == c_size and abs(rec.mtime - c_mtime) < 0.001:
                        if dur == -1.0 and len(emb_blob) == 0:
                            results[c_path] = (-1.0, b"")
                            if c_vol_id == 0 or c_f_id == 0:
                                backfill_recs.append(rec)
                        elif expected_bytes is None or len(emb_blob) == expected_bytes:
                            results[c_path] = (dur, emb_blob)
                            if c_vol_id == 0 or c_f_id == 0:
                                backfill_recs.append(rec)

                done = min(i + chunk_size, total_recs)
                if done == total_recs or i % 2700 == 0:
                    tracker.emit_range(
                        f"Checking video cache ({done:,}/{total_recs:,})...",
                        0.0, 0.50, done, total_recs,
                        force=(done == total_recs),
                    )

            if backfill_recs:
                self._backfill_l1_file_ids(conn, "cache_videos", backfill_recs, stop_check=stop_check, tracker=tracker)

            # 2. Tier 2 (L2 - NTFS File ID Lazy): Resilience for moved folders and files on the same volume
            uncached = [r for r in records if str(r.path) not in results]
            l2_candidates = self._find_l2_candidates(conn, "cache_videos", uncached, stop_check=stop_check, tracker=tracker)
            if l2_candidates and not (stop_check and stop_check()):
                l2_matches: List[Tuple[str, FileRecord, float, bytes, Optional[str]]] = []
                active_new_paths: Set[str] = set()
                total_l2 = len(l2_candidates)

                for i in range(0, total_l2, chunk_size):
                    if stop_check and stop_check():
                        return results
                    chunk = l2_candidates[i:i + chunk_size]
                    unique_fids = list({r.file_id for r in chunk})
                    placeholders = ",".join("?" for _ in unique_fids)
                    l2_map: Dict[Tuple[int, int], List[FileRecord]] = defaultdict(list)
                    for r in chunk:
                        l2_map[(r.file_id, r.volume_id)].append(r)

                    cursor = conn.execute(
                        f"SELECT path, size, mtime, duration, embedding, volume_id, file_id, prefix_hash FROM cache_videos WHERE file_id IN ({placeholders}) AND file_id != 0",
                        unique_fids
                    )
                    for row in cursor.fetchall():
                        c_path, c_size, c_mtime, dur, emb_blob, c_vol_id, c_f_id, c_prefix = row
                        is_valid_tombstone = (dur == -1.0 and len(emb_blob) == 0)
                        if not is_valid_tombstone and expected_bytes is not None and len(emb_blob) != expected_bytes:
                            continue
                        for rec in l2_map.get((c_f_id, c_vol_id), ()):
                            if rec.size == c_size and abs(rec.mtime - c_mtime) < 0.001:
                                new_path_str = str(rec.path)
                                results[new_path_str] = (dur, emb_blob)
                                active_new_paths.add(new_path_str)
                                l2_matches.append((c_path, rec, dur, emb_blob, c_prefix))

                    done_l2 = min(i + chunk_size, total_l2)
                    tracker.emit_range(
                        f"Checking video cache moved files ({done_l2:,}/{total_l2:,})...",
                        0.80, 0.88, done_l2, total_l2,
                        force=(done_l2 == total_l2),
                    )

                dir_cache: Dict[str, bool] = {}
                renames: List[Tuple[str, int, float, int, int, str]] = []
                renamed_sources: Set[str] = set()
                upserts = []
                stale_candidates: Set[str] = set()
                for c_path, rec, dur, emb_blob, c_prefix in l2_matches:
                    new_path_str = str(rec.path)
                    if (
                        c_path != new_path_str
                        and c_path not in active_new_paths
                        and c_path not in renamed_sources
                        and not self._path_exists_cached(c_path, dir_cache)
                    ):
                        renamed_sources.add(c_path)
                        renames.append((new_path_str, rec.size, rec.mtime, rec.volume_id, rec.file_id, c_path))
                    else:
                        upserts.append((new_path_str, rec.size, rec.mtime, dur, emb_blob, rec.volume_id, rec.file_id, c_prefix))
                        if c_path != new_path_str:
                            stale_candidates.add(c_path)

                self._apply_l2_in_place_renames(conn, "cache_videos", renames)
                if upserts:
                    conn.executemany("""
                        INSERT INTO cache_videos (path, size, mtime, duration, embedding, volume_id, file_id, prefix_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(path) DO UPDATE SET
                            size=excluded.size, mtime=excluded.mtime, duration=excluded.duration,
                            embedding=excluded.embedding, volume_id=excluded.volume_id, file_id=excluded.file_id,
                            prefix_hash=COALESCE(excluded.prefix_hash, cache_videos.prefix_hash);
                    """, upserts)
                if stale_candidates:
                    self._cleanup_dead_paths(conn, "cache_videos", stale_candidates, active_new_paths, dir_cache=dir_cache)

            # 3. Tier 3 (L3 - Parallel Batch Content Sieve): For exFAT, FAT32, or cross-volume copies
            uncached_l3 = [r for r in records if str(r.path) not in results]
            if uncached_l3 and not (stop_check and stop_check()) and self._table_has_rows(conn, "cache_videos", "prefix_hash IS NOT NULL"):
                known_sizes = self._prefilter_known_sizes(
                    conn, "cache_videos", uncached_l3, "prefix_hash IS NOT NULL",
                    tracker=tracker,
                    progress_range=(0.88, 0.91),
                    progress_label="Checking video cache signatures",
                    stop_check=stop_check,
                )
                if known_sizes and not (stop_check and stop_check()):
                    sieve_candidates = [r for r in uncached_l3 if r.size in known_sizes]
                    self._populate_prefix_hashes_parallel(
                        sieve_candidates,
                        stop_check=stop_check,
                        tracker=tracker,
                        progress_range=(0.91, 0.97),
                        progress_label="Checking video cache signatures",
                    )
                    hashed_candidates = [r for r in sieve_candidates if r.prefix_hash]
                    l3_matched: List[Tuple[FileRecord, float, bytes]] = []
                    stale_l3_candidates: Set[str] = set()
                    active_l3_paths: Set[str] = set()
                    total_l3 = len(hashed_candidates)

                    for i in range(0, total_l3, chunk_size):
                        if stop_check and stop_check():
                            return results
                        chunk = hashed_candidates[i:i + chunk_size]
                        unique_hashes = list({r.prefix_hash for r in chunk if r.prefix_hash})
                        if not unique_hashes:
                            continue
                        placeholders = ",".join("?" for _ in unique_hashes)
                        cursor = conn.execute(
                            f"SELECT path, size, prefix_hash, mtime, duration, embedding FROM cache_videos WHERE prefix_hash IN ({placeholders}) AND prefix_hash IS NOT NULL",
                            unique_hashes
                        )
                        match_map: Dict[Tuple[int, str], List[Tuple[str, float, float, bytes]]] = defaultdict(list)
                        for r_path, r_size, r_phash, r_mtime, dur, emb_blob in cursor.fetchall():
                            is_valid_tombstone = (dur == -1.0 and len(emb_blob) == 0)
                            if is_valid_tombstone or expected_bytes is None or len(emb_blob) == expected_bytes:
                                match_map[(r_size, r_phash)].append((r_path, r_mtime, dur, emb_blob))

                        for rec in chunk:
                            candidates_row = match_map.get((rec.size, rec.prefix_hash))  # type: ignore[arg-type]
                            if not candidates_row:
                                continue
                            is_dav = rec.ext == ".dav" or str(rec.path).lower().endswith(".dav")
                            matched_entry = None
                            for r_path, r_mtime, dur, emb_blob in candidates_row:
                                # Pre-allocated CCTV .dav files require mtime proximity to avoid empty-container collisions
                                if is_dav and abs(rec.mtime - r_mtime) > 2.0:
                                    continue
                                matched_entry = (r_path, dur, emb_blob)
                                break

                            if matched_entry is not None:
                                src_path, dur, emb_blob = matched_entry
                                new_path_str = str(rec.path)
                                results[new_path_str] = (dur, emb_blob)
                                active_l3_paths.add(new_path_str)
                                if src_path != new_path_str:
                                    stale_l3_candidates.add(src_path)
                                l3_matched.append((rec, dur, emb_blob))

                        done_l3 = min(i + chunk_size, total_l3)
                        tracker.emit_range(
                            f"Checking video cache signatures ({done_l3:,}/{total_l3:,})...",
                            0.97, 0.99, done_l3, total_l3,
                            force=(done_l3 == total_l3),
                        )

                    if l3_matched:
                        self._populate_file_ids_parallel([r for r, _, _ in l3_matched], stop_check=stop_check)
                        l3_upserts = [
                            (str(rec.path), rec.size, rec.mtime, dur, emb_blob, rec.volume_id, rec.file_id, rec.prefix_hash)
                            for rec, dur, emb_blob in l3_matched
                        ]
                        conn.executemany("""
                            INSERT INTO cache_videos (path, size, mtime, duration, embedding, volume_id, file_id, prefix_hash)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(path) DO UPDATE SET
                                size=excluded.size, mtime=excluded.mtime, duration=excluded.duration,
                                embedding=excluded.embedding,
                                volume_id=CASE WHEN excluded.volume_id != 0 THEN excluded.volume_id ELSE cache_videos.volume_id END,
                                file_id=CASE WHEN excluded.file_id != 0 THEN excluded.file_id ELSE cache_videos.file_id END,
                                prefix_hash=excluded.prefix_hash;
                        """, l3_upserts)
                        self._cleanup_dead_paths(conn, "cache_videos", stale_l3_candidates, active_l3_paths)
        tracker.finish()
        return results

    def get_video_cache_l1_and_indexes(
        self,
        records: List[FileRecord],
        expected_bytes: Optional[int] = None,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> Tuple[
        Dict[str, Tuple[float, bytes]],
        Dict[Tuple[int, int], Tuple[str, int, float, float, bytes, Optional[str]]],
        Dict[Tuple[int, str], List[Tuple[str, float, float, bytes]]],
        Set[int],
        Set[int],
    ]:
        """
        Zero-disk-I/O Stage-2 cache lookup for the unified streaming video pipeline:
        1. L1 direct path lookup in SQLite (path + size + mtime).
        2. Preload lightweight in-memory L2 (volume_id, file_id) and L3 (size, prefix_hash)
           lookup indexes ONLY for sizes present in L1 misses — preserving CCTV .dav mtime
           proximity safety with ZERO per-file os.stat() and ZERO file open() calls in Stage 2.
        """
        l1_results: Dict[str, Tuple[float, bytes]] = {}
        l2_index: Dict[Tuple[int, int], Tuple[str, int, float, float, bytes, Optional[str]]] = {}
        l3_index: Dict[Tuple[int, str], List[Tuple[str, float, float, bytes]]] = defaultdict(list)
        l3_known_sizes: Set[int] = set()
        l2_known_volumes: Set[int] = set()

        if not records:
            return l1_results, l2_index, l3_index, l3_known_sizes, l2_known_volumes

        chunk_size = 900
        total_recs = len(records)
        tracker = _CacheProgressTracker(progress_callback, total_recs, "Checking video cache")

        with self._get_connection() as conn:
            if not self._table_has_rows(conn, "cache_videos"):
                tracker.finish()
                return l1_results, l2_index, l3_index, l3_known_sizes, l2_known_volumes

            # 1. Tier 1 (L1): Direct path lookup (pure SQLite PK lookup, zero file I/O)
            for i in range(0, total_recs, chunk_size):
                if stop_check and stop_check():
                    return l1_results, l2_index, l3_index, l3_known_sizes, l2_known_volumes
                chunk = records[i:i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                paths = [str(r.path) for r in chunk]
                cursor = conn.execute(
                    f"SELECT path, size, mtime, duration, embedding, volume_id, file_id, prefix_hash FROM cache_videos WHERE path IN ({placeholders})",
                    paths,
                )
                record_map = {str(r.path): r for r in chunk}
                for c_path, c_size, c_mtime, dur, emb_blob, c_vol_id, c_f_id, c_prefix in cursor.fetchall():
                    rec = record_map.get(c_path)
                    if rec and rec.size == c_size and abs(rec.mtime - c_mtime) < 0.001:
                        is_valid_tombstone = (dur == -1.0 and len(emb_blob) == 0)
                        if is_valid_tombstone or expected_bytes is None or len(emb_blob) == expected_bytes:
                            l1_results[c_path] = (dur, emb_blob)
                            if c_prefix:
                                rec.prefix_hash = c_prefix
                                l3_index[(c_size, c_prefix)].append((c_path, c_mtime, dur, emb_blob))
                                l3_known_sizes.add(c_size)
                            if c_vol_id > 0 and c_f_id > 0:
                                rec.volume_id = c_vol_id
                                rec.file_id = c_f_id

                done = min(i + chunk_size, total_recs)
                if done == total_recs or i % 2700 == 0:
                    tracker.emit_range(
                        f"Checking video cache ({done:,}/{total_recs:,})...",
                        0.0, 0.75, done, total_recs,
                        force=(done == total_recs),
                    )

            # 2. Preload in-memory L2 & L3 lookup indexes for L1 misses (pure SQLite, zero file I/O)
            misses = [r for r in records if str(r.path) not in l1_results]
            if misses and not (stop_check and stop_check()):
                db_volumes = {
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT volume_id FROM cache_videos WHERE file_id != 0;"
                    ).fetchall()
                    if row[0]
                }
                drive_vol_cache: Dict[str, int] = {}
                active_l2_vols: Set[int] = set()
                if db_volumes:
                    seen_drives: Set[str] = set()
                    has_unknown_vol = False
                    for r in misses:
                        drv = os.path.splitdrive(str(r.path))[0].upper()
                        if drv in seen_drives:
                            continue
                        seen_drives.add(drv)
                        if not get_volume_capabilities(r.path)[1]:
                            continue
                        d_vol = _get_drive_volume_id(r.path, drive_vol_cache)
                        if d_vol == 0:
                            has_unknown_vol = True
                        elif d_vol in db_volumes:
                            active_l2_vols.add(d_vol)
                    if has_unknown_vol:
                        active_l2_vols = set(db_volumes)

                miss_sizes = list({r.size for r in misses})
                total_sizes = len(miss_sizes)
                # Cap preloaded index rows to ~120k (~920 MB max RAM for 7.68 KB video embeddings)
                max_preload_rows = 120_000
                loaded_rows = 0

                for i in range(0, total_sizes, chunk_size):
                    if (stop_check and stop_check()) or loaded_rows >= max_preload_rows:
                        break
                    chunk_s = miss_sizes[i:i + chunk_size]
                    placeholders = ",".join("?" for _ in chunk_s)
                    cursor = conn.execute(
                        f"SELECT path, size, mtime, duration, volume_id, file_id, prefix_hash, embedding "
                        f"FROM cache_videos WHERE size IN ({placeholders});",
                        chunk_s,
                    )
                    for c_path, c_size, c_mtime, dur, c_vol_id, c_f_id, c_prefix, emb_blob in cursor.fetchall():
                        is_valid_tombstone = (dur == -1.0 and len(emb_blob) == 0)
                        if not is_valid_tombstone and expected_bytes is not None and len(emb_blob) != expected_bytes:
                            continue
                        used = False
                        if c_f_id != 0 and c_vol_id in active_l2_vols:
                            if (c_vol_id, c_f_id) not in l2_index:
                                l2_index[(c_vol_id, c_f_id)] = (c_path, c_size, c_mtime, dur, emb_blob, c_prefix)
                                l2_known_volumes.add(c_vol_id)
                                used = True
                        if c_prefix:
                            key = (c_size, c_prefix)
                            # Keep up to 4 entries per (size, prefix_hash) so .dav mtime checks stay accurate
                            if len(l3_index[key]) < 4:
                                l3_index[key].append((c_path, c_mtime, dur, emb_blob))
                                l3_known_sizes.add(c_size)
                                used = True
                        if used:
                            loaded_rows += 1
                            if loaded_rows >= max_preload_rows:
                                break

                    done_s = min(i + chunk_size, total_sizes)
                    if total_sizes > 1800 and (done_s == total_sizes or i % 2700 == 0):
                        tracker.emit_range(
                            f"Checking video cache index ({done_s:,}/{total_sizes:,})...",
                            0.75, 0.99, done_s, total_sizes,
                            force=(done_s == total_sizes),
                        )

        tracker.finish()
        return l1_results, l2_index, l3_index, l3_known_sizes, l2_known_volumes

    def save_video_batch(self, entries: List[Tuple]):
        """Save batch of decoded videos to cache."""
        if not entries:
            return
        norm_entries = []
        for e in entries:
            if len(e) >= 8:
                norm_entries.append(e[:8])
            else:
                norm_entries.append((e[0], e[1], e[2], e[3], e[4], 0, 0, None))

        with self._get_connection() as conn:
            conn.executemany("""
                INSERT INTO cache_videos (path, size, mtime, duration, embedding, volume_id, file_id, prefix_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size=excluded.size,
                    mtime=excluded.mtime,
                    duration=excluded.duration,
                    embedding=excluded.embedding,
                    volume_id=CASE WHEN excluded.volume_id != 0 THEN excluded.volume_id ELSE cache_videos.volume_id END,
                    file_id=CASE WHEN excluded.file_id != 0 THEN excluded.file_id ELSE cache_videos.file_id END,
                    prefix_hash=COALESCE(excluded.prefix_hash, cache_videos.prefix_hash);
            """, norm_entries)

    # -------------------------------------------------------------------------
    # 4. Statistics and Maintenance
    # -------------------------------------------------------------------------

    def remove_paths(self, paths: Iterable[Union[Path, str]]) -> None:
        """Delete orphaned cache entries for removed files across all cache tables."""
        unique_paths = list(dict.fromkeys(str(p) for p in paths))
        if not unique_paths:
            return
        chunk_size = 900
        with self._get_connection() as conn:
            for i in range(0, len(unique_paths), chunk_size):
                chunk = unique_paths[i:i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                for table in self._ALL_CACHE_TABLES:
                    conn.execute(f"DELETE FROM {table} WHERE path IN ({placeholders});", chunk)

    def relocate_paths(self, moves: Sequence[Tuple[Union[Path, str], Union[Path, str]]]) -> None:
        """Update cached paths in-place across all tables when files are moved (no BLOB rewrite)."""
        if not moves:
            return
        updates: List[Tuple[str, int, int, str]] = []
        for src, dst in moves:
            src_str = str(src)
            dst_path = Path(dst)
            dst_str = str(dst_path)
            if src_str == dst_str:
                continue
            vol_id, f_id = 0, 0
            _, supports_fid = get_volume_capabilities(dst_path)
            if supports_fid:
                try:
                    st = os.stat(dst_path, follow_symlinks=False)
                    vol_id = getattr(st, "st_dev", 0) or 0
                    f_id = getattr(st, "st_ino", 0) or 0
                except OSError:
                    pass
            updates.append((dst_str, vol_id, f_id, src_str))

        if not updates:
            return
        with self._get_connection() as conn:
            for table in self._ALL_CACHE_TABLES:
                conn.executemany(
                    f"UPDATE OR REPLACE {table} SET path = ?, volume_id = ?, file_id = ? WHERE path = ?;",
                    updates,
                )

    def prune_scanned_roots(
        self,
        scan_paths: Sequence[ScanPath],
        excluded_paths: Sequence[Path],
        active_records: Sequence[FileRecord],
        mode: ScanMode,
        vacuum_threshold: float = 0.25,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> int:
        """
        Prune orphaned cache rows under scanned root folders for the active mode only.
        Respects excluded folders, verifies each root is currently mounted, and runs
        a conditional VACUUM when freed pages exceed vacuum_threshold.
        """
        if stop_check and stop_check():
            return 0
        table_map = {
            ScanMode.EXACT: "cache_exact",
            ScanMode.IMAGES: "cache_images",
            ScanMode.VIDEOS: "cache_videos",
        }
        table = table_map.get(mode)
        if not table or not scan_paths:
            return 0

        root_prefixes: List[str] = []
        for sp in scan_paths:
            try:
                resolved = sp.path.resolve()
                if not resolved.is_dir():
                    continue
                prefix = str(resolved).rstrip("\\/") + "\\"
                root_prefixes.append(prefix)
            except OSError:
                continue

        if not root_prefixes:
            return 0

        norm_fn = str.lower if os.name == "nt" else lambda s: s
        active_norm = {norm_fn(str(r.path)) for r in active_records}
        excl_exact: Set[str] = set()
        excl_prefixes: List[str] = []
        for ep in excluded_paths:
            try:
                ep_norm = norm_fn(os.path.normpath(str(ep.resolve()))).rstrip("\\/")
            except OSError:
                ep_norm = norm_fn(os.path.normpath(str(ep))).rstrip("\\/")
            if ep_norm:
                excl_exact.add(ep_norm)
                excl_prefixes.append(ep_norm + "\\")
        has_exclusions = bool(excl_exact or excl_prefixes)

        dead_paths: List[str] = []
        should_vacuum = False

        with self._get_connection() as conn:
            seen_candidates: Set[str] = set()
            for prefix in root_prefixes:
                prefix_norm = norm_fn(prefix)
                low, high = self._prefix_range_bounds(prefix)
                cur = conn.execute(
                    f"SELECT path FROM {table} WHERE path >= ? AND path < ?;",
                    (low, high),
                )
                for (db_path_str,) in cur.fetchall():
                    if stop_check and stop_check():
                        return 0
                    if db_path_str in seen_candidates:
                        continue
                    norm_p = norm_fn(db_path_str)
                    if not norm_p.startswith(prefix_norm):
                        continue
                    if norm_p in active_norm:
                        continue
                    if has_exclusions and (
                        norm_p in excl_exact or any(norm_p.startswith(ep) for ep in excl_prefixes)
                    ):
                        continue
                    seen_candidates.add(db_path_str)
                    dead_paths.append(db_path_str)

            if dead_paths and not (stop_check and stop_check()):
                chunk_size = 900
                for i in range(0, len(dead_paths), chunk_size):
                    if stop_check and stop_check():
                        break
                    chunk = dead_paths[i:i + chunk_size]
                    placeholders = ",".join("?" for _ in chunk)
                    conn.execute(f"DELETE FROM {table} WHERE path IN ({placeholders});", chunk)

                if not (stop_check and stop_check()):
                    page_count = conn.execute("PRAGMA page_count;").fetchone()[0]
                    freelist_count = conn.execute("PRAGMA freelist_count;").fetchone()[0]
                    if (
                        page_count > 0
                        and freelist_count >= 16
                        and (freelist_count / page_count) >= vacuum_threshold
                    ):
                        should_vacuum = True

        if should_vacuum and not (stop_check and stop_check()):
            v_conn = sqlite3.connect(str(self.db_path), timeout=60.0)
            try:
                v_conn.isolation_level = None
                v_conn.execute("VACUUM;")
            finally:
                v_conn.close()

        return len(dead_paths)

    def get_stats(self) -> Dict[str, Any]:
        """Retorna contagem de registros em cache e tamanho do banco em MB."""
        stats = {"exact_count": 0, "images_count": 0, "videos_count": 0, "db_size_mb": 0.0}
        with self._get_connection() as conn:
            stats["exact_count"] = conn.execute("SELECT COUNT(*) FROM cache_exact").fetchone()[0]
            stats["images_count"] = conn.execute("SELECT COUNT(*) FROM cache_images").fetchone()[0]
            stats["videos_count"] = conn.execute("SELECT COUNT(*) FROM cache_videos").fetchone()[0]
        if self.db_path.exists():
            stats["db_size_mb"] = round(self.db_path.stat().st_size / (1024 * 1024), 2)
        return stats

    def checkpoint(self):
        """Executa PRAGMA optimize e checkpoint manual do WAL para manter estatísticas e truncar o log."""
        try:
            with self._get_connection() as conn:
                conn.execute("PRAGMA optimize;")
                conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
        except Exception:
            pass

    def clear(self, mode: Optional[ScanMode] = None):
        """Clear cached data for a specific mode or for all modes."""
        if mode is None:
            # Recreate the database preserving app_settings (instantaneous file unlink vs. slow row-by-row DELETE + VACUUM)
            self.schema_result = reset_database_preserving_settings(
                self.db_path, reason="user_clear"
            )
            return

        table_map = {
            ScanMode.EXACT: "cache_exact",
            ScanMode.IMAGES: "cache_images",
            ScanMode.VIDEOS: "cache_videos",
        }
        target_table = table_map.get(mode)
        if not target_table:
            return

        from dupeclean.core.db_schema import _create_all_tables, _ensure_indexes

        with self._get_connection() as conn:
            conn.execute(f"DROP TABLE IF EXISTS {target_table};")
            _create_all_tables(conn)
            _ensure_indexes(conn)

        # VACUUM must run outside a transaction on a clean connection
        conn = sqlite3.connect(str(self.db_path), timeout=60.0)
        try:
            conn.isolation_level = None
            conn.execute("VACUUM;")
        finally:
            conn.close()
