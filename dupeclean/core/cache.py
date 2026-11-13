"""
Gerenciador de Cache Persistente (SQLite WAL).
Armazena hashes e embeddings visuais para evitar reprocessamento desnecessário,
permitindo escaneamentos quase instantâneos e retenção total de progresso em cancelamentos.
"""

import json
import os
import shutil
import sqlite3
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any, Callable, Set
import numpy as np
import torch
import xxhash

from dupeclean.core.models import FileRecord, ScanMode, ScanPath
from dupeclean.core.scanner import get_volume_capabilities


def _stat_record_file_id(rec: FileRecord) -> None:
    """Coleta volume_id e file_id reais via os.stat() assumindo volume já validado."""
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


def compute_prefix_hash(path: Path, size: int) -> Optional[str]:
    """
    Calcula a assinatura rápida (Head 4KB + Tail 4KB ou <=8KB total) via xxHash64.
    Utilizada para o Tier 3 (L3 Content Sieve) em volumes exFAT/FAT32 ou cópias entre unidades.
    """
    try:
        with open(path, "rb") as f:
            if size <= 8192:
                data = f.read(8192)
                return xxhash.xxh64(data).hexdigest()
            else:
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
    except (OSError, PermissionError):
        return None


def get_default_cache_db_path() -> Path:
    """Retorna o caminho centralizado do banco de dados DupeClean.db (pasta data/), com migração automática."""
    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).parent
    else:
        base_dir = Path(__file__).resolve().parent.parent.parent  # Raiz do DupeClean

    data_dir = base_dir / "data"
    old_cache = base_dir / "cache" / "cache.db"

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        new_db = data_dir / "DupeClean.db"

        # Migração transparente do cache antigo se existir
        if old_cache.exists() and not new_db.exists():
            shutil.move(str(old_cache), str(new_db))
            for ext in ["-wal", "-shm"]:
                old_aux = base_dir / "cache" / f"cache.db{ext}"
                if old_aux.exists():
                    shutil.move(str(old_aux), str(data_dir / f"DupeClean.db{ext}"))
            try:
                (base_dir / "cache").rmdir()
            except Exception:
                pass

        return new_db
    except Exception:
        appdata = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "DupeClean" / "data"
        appdata.mkdir(parents=True, exist_ok=True)
        return appdata / "DupeClean.db"


class CacheManager:
    """
    Gerenciador de cache SQLite de alta performance, thread-safe e resiliente a bibliotecas massivas.
    Configurado com modo WAL, mmap I/O, commit/rollback transacional seguro e fechamento estrito de conexões.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or get_default_cache_db_path()
        self._init_db()

    @contextmanager
    def _get_connection(self):
        """
        Gerenciador de contexto que garante commit em sucesso, rollback em erro
        e fechamento obrigatório da conexão para eliminar leaks de locks no Windows.
        """
        conn = sqlite3.connect(str(self.db_path), timeout=60.0)
        try:
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA busy_timeout = 60000;")  # 60 segundos de espera para locks
            conn.execute("PRAGMA cache_size = -64000;")   # 64 MB de cache de páginas em RAM (elimina I/O repetido)
            conn.execute("PRAGMA temp_store = MEMORY;")   # Estruturas temporárias em memória RAM
            conn.execute("PRAGMA mmap_size = 268435456;") # 256 MB zero-copy memory mapping
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        """Cria e configura o banco com parâmetros industriais de alta performance e resiliência."""
        conn = sqlite3.connect(str(self.db_path), timeout=60.0)
        try:
            # Pragmas estruturais que devem ser configurados na inicialização do arquivo
            conn.execute("PRAGMA page_size = 8192;")  # Páginas de 8 KB para alta eficiência em DBs gigantes
            conn.execute("PRAGMA journal_mode = WAL;")  # Write-Ahead Logging
            conn.execute("PRAGMA journal_size_limit = 67108864;")  # Limita arquivo WAL a 64 MB (evita inchaço)
            conn.execute("PRAGMA wal_autocheckpoint = 2000;")  # Checkpoint regular a cada 2000 páginas
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS cache_exact (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    prefix_hash TEXT,
                    full_hash TEXT,
                    volume_id INTEGER DEFAULT 0,
                    file_id INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS cache_images (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    embedding BLOB NOT NULL,
                    volume_id INTEGER DEFAULT 0,
                    file_id INTEGER DEFAULT 0,
                    prefix_hash TEXT
                );

                CREATE TABLE IF NOT EXISTS cache_videos (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    duration REAL NOT NULL,
                    embedding BLOB NOT NULL,
                    volume_id INTEGER DEFAULT 0,
                    file_id INTEGER DEFAULT 0,
                    prefix_hash TEXT
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)

            # Migração transparente de colunas para bancos pré-existentes
            def _add_col(table: str, col: str, col_type: str):
                cur = conn.execute(f"PRAGMA table_info({table});")
                cols = {row[1] for row in cur.fetchall()}
                if col not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type};")

            _add_col("cache_exact", "volume_id", "INTEGER DEFAULT 0")
            _add_col("cache_exact", "file_id", "INTEGER DEFAULT 0")

            _add_col("cache_images", "volume_id", "INTEGER DEFAULT 0")
            _add_col("cache_images", "file_id", "INTEGER DEFAULT 0")
            _add_col("cache_images", "prefix_hash", "TEXT")

            _add_col("cache_videos", "volume_id", "INTEGER DEFAULT 0")
            _add_col("cache_videos", "file_id", "INTEGER DEFAULT 0")
            _add_col("cache_videos", "prefix_hash", "TEXT")

            # Índices parciais de altíssima performance para L2 (File ID), L3 (Prefix Hash) e pré-filtro por tamanho
            conn.execute("CREATE INDEX IF NOT EXISTS idx_exact_size ON cache_exact(size);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_exact_fileid ON cache_exact(file_id, volume_id) WHERE file_id != 0;")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_exact_prefix_h ON cache_exact(prefix_hash, size) WHERE prefix_hash IS NOT NULL;")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_images_size ON cache_images(size);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_images_fileid ON cache_images(file_id, volume_id) WHERE file_id != 0;")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_images_prefix_h ON cache_images(prefix_hash, size) WHERE prefix_hash IS NOT NULL;")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_size ON cache_videos(size);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_fileid ON cache_videos(file_id, volume_id) WHERE file_id != 0;")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_prefix_h ON cache_videos(prefix_hash, size) WHERE prefix_hash IS NOT NULL;")

            # Remove índices secundários obsoletos
            conn.execute("DROP INDEX IF EXISTS idx_exact_lookup;")
            conn.execute("DROP INDEX IF EXISTS idx_images_lookup;")
            conn.execute("DROP INDEX IF EXISTS idx_videos_lookup;")
            conn.execute("DROP INDEX IF EXISTS idx_exact_prefix;")
            conn.execute("DROP INDEX IF EXISTS idx_images_prefix;")
            conn.execute("DROP INDEX IF EXISTS idx_videos_prefix;")
            # Purga embeddings de versões obsoletas (preserva tombstones com duration == -1.0)
            conn.execute("DELETE FROM cache_images WHERE length(embedding) != 1536 AND length(embedding) != 0;")
            conn.execute("DELETE FROM cache_videos WHERE length(embedding) != 7680 AND duration != -1.0;")

            # Migração de versão da assinatura exata (v1 4KB head -> v2 Head 4KB + Tail 4KB):
            # Invalida prefix_hash uma única vez para recalcular assinaturas compostas sem perder nenhum full_hash
            cur = conn.execute("SELECT value FROM app_settings WHERE key = 'signature_version';")
            row = cur.fetchone()
            if not row or row[0] != "v2_head_tail":
                conn.execute("UPDATE cache_exact SET prefix_hash = NULL;")
                conn.execute("""
                    INSERT INTO app_settings (key, value) VALUES ('signature_version', 'v2_head_tail')
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value;
                """)

            conn.commit()
        finally:
            conn.close()

    # -------------------------------------------------------------------------
    # 0. Gerenciamento de Configurações e Estado da Aplicação (app_settings)
    # -------------------------------------------------------------------------

    def save_setting(self, key: str, value: Any):
        """Salva uma configuração ou estado (serializado em JSON se complexo)."""
        val_str = json.dumps(value) if not isinstance(value, str) else value
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?);",
                (key, val_str)
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        """Recupera uma configuração da tabela app_settings, desserializando JSON se possível."""
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
        utilizando o histórico de escaneamentos e metadados persistentes em SQLite.
        Elimina completamente o modo indeterminado (barra que fica quicando) sem tocar no disco.
        """
        if not scan_paths:
            return 0

        # 1. Tenta recuperar do histórico recente de pastas em app_settings
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

        # 2. Fallback: Consulta contagem nos caches existentes via LIKE em SQLite (<1ms)
        table = "cache_images" if mode == ScanMode.IMAGES else ("cache_videos" if mode == ScanMode.VIDEOS else "cache_exact")

        try:
            with self._get_connection() as conn:
                for sp in missing_paths:
                    prefix = str(sp.path.resolve()).rstrip("\\/") + "\\%"
                    cursor = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE path LIKE ?;", (prefix,))
                    count = cursor.fetchone()[0]
                    if count == 0:
                        alt_table = "cache_exact" if table != "cache_exact" else "cache_images"
                        cursor = conn.execute(f"SELECT COUNT(*) FROM {alt_table} WHERE path LIKE ?;", (prefix,))
                        count = cursor.fetchone()[0]
                    total_estimate += count
        except Exception:
            pass

        return total_estimate

    def save_folder_counts(self, scan_paths: List[ScanPath], total_count: int):
        """Salva a última contagem de arquivos observada para as pastas escaneadas."""
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
    # Helpers Compartilhados (Arquitetura Tríbrida L1 -> L2 Lazy -> L3 Parallel)
    # -------------------------------------------------------------------------

    def _populate_file_ids_parallel(
        self,
        records: List[FileRecord],
        stop_check: Optional[Callable[[], bool]] = None,
        assume_supported: bool = False
    ) -> None:
        """Preenche (volume_id, file_id) via os.stat() em lotes paralelos apenas onde necessário."""
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

        chunk_size = 2000
        chunks = [to_stat[i:i + chunk_size] for i in range(0, len(to_stat), chunk_size)]

        def _stat_chunk(batch: List[FileRecord]) -> None:
            for rec in batch:
                if stop_check and stop_check():
                    return
                _stat_record_file_id(rec)

        workers = min(16, max(4, os.cpu_count() or 4))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_stat_chunk, chunks))

    def _populate_prefix_hashes_parallel(
        self,
        records: List[FileRecord],
        stop_check: Optional[Callable[[], bool]] = None
    ) -> None:
        """Calcula prefix_hash (Head 4KB + Tail 4KB) em paralelo para candidatos do L3."""
        to_hash = [r for r in records if r.prefix_hash is None]
        if not to_hash or (stop_check and stop_check()):
            return

        def _hash_one(rec: FileRecord) -> None:
            if stop_check and stop_check():
                return
            rec.prefix_hash = compute_prefix_hash(rec.path, rec.size)

        if len(to_hash) <= 32:
            for r in to_hash:
                if stop_check and stop_check():
                    return
                _hash_one(r)
            return

        workers = min(16, max(4, os.cpu_count() or 4))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_hash_one, to_hash))

    def _prefilter_known_sizes(
        self,
        conn: sqlite3.Connection,
        table: str,
        records: List[FileRecord],
        condition: str
    ) -> Set[int]:
        """Consulta tamanhos existentes em SQLite usando o índice idx_*_size antes de tocar no disco."""
        candidate_sizes = list({r.size for r in records})
        known_sizes: Set[int] = set()
        chunk_size = 900
        for i in range(0, len(candidate_sizes), chunk_size):
            chunk_s = candidate_sizes[i:i + chunk_size]
            placeholders = ",".join("?" for _ in chunk_s)
            cur = conn.execute(
                f"SELECT DISTINCT size FROM {table} WHERE size IN ({placeholders}) AND {condition};",
                chunk_s
            )
            known_sizes.update(row[0] for row in cur.fetchall())
        return known_sizes

    def _backfill_l1_file_ids(
        self,
        conn: sqlite3.Connection,
        table: str,
        backfill_recs: List[FileRecord],
        stop_check: Optional[Callable[[], bool]] = None
    ) -> None:
        """Atualiza em lote (volume_id, file_id) para registros legados acertados no L1."""
        if not backfill_recs or (stop_check and stop_check()):
            return
        self._populate_file_ids_parallel(backfill_recs, stop_check=stop_check)
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
        stop_check: Optional[Callable[[], bool]] = None
    ) -> List[FileRecord]:
        """Seleciona misses do L1 elegíveis a NTFS/ReFS com tamanho presente no banco e coleta File ID."""
        if not uncached or (stop_check and stop_check()):
            return []
        eligible = [
            r for r in uncached
            if (r.volume_id > 0 and r.file_id > 0) or get_volume_capabilities(r.path)[1]
        ]
        if not eligible:
            return []
        known_sizes = self._prefilter_known_sizes(conn, table, eligible, "file_id != 0")
        if not known_sizes:
            return []
        candidates = [r for r in eligible if r.size in known_sizes]
        self._populate_file_ids_parallel(candidates, stop_check=stop_check, assume_supported=True)
        return [r for r in candidates if r.volume_id > 0 and r.file_id > 0]

    def _cleanup_dead_paths(
        self,
        conn: sqlite3.Connection,
        table: str,
        stale_candidates: Set[str],
        active_paths: Set[str]
    ) -> None:
        """Remove caminhos antigos do SQLite apenas se não existirem mais no disco (protege hardlinks)."""
        to_delete = [
            (p,) for p in stale_candidates
            if p not in active_paths and not os.path.exists(p)
        ]
        if to_delete:
            conn.executemany(f"DELETE FROM {table} WHERE path = ?;", to_delete)

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
        Retorna dicionário {caminho: (prefix_hash, full_hash)} para registros válidos.
        Executa arquitetura L1 -> L2 Lazy:
        - L1: Verificação direta por caminho + tamanho + mtime (zero I/O), com backfill legado.
        - L2: Resiliência NTFS por File ID (st_dev + st_ino) sob demanda apenas para misses de L1.
        Invalida caso o tamanho ou mtime tenham mudado. Suporta cancelamento atômico via stop_check.
        """
        results: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        if not records:
            return results

        chunk_size = 900
        total_recs = len(records)
        backfill_recs: List[FileRecord] = []

        with self._get_connection() as conn:
            # 1. Tier 1 (L1 - In-place Path): Consulta rápida por caminho direto
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

                if progress_callback and total_recs > 2000 and (i % 9000 == 0 or i + chunk_size >= total_recs):
                    done = min(i + chunk_size, total_recs)
                    progress_callback(f"Checking cache ({done:,}/{total_recs:,})...", 0, 0)

            if backfill_recs:
                self._backfill_l1_file_ids(conn, "cache_exact", backfill_recs, stop_check=stop_check)

            # 2. Tier 2 (L2 - NTFS File ID Lazy): Resiliência a pastas e arquivos renomeados/movidos no mesmo volume
            uncached = [r for r in records if str(r.path) not in results]
            if progress_callback and len(uncached) > 2000:
                progress_callback(f"Checking moved files ({len(uncached):,})...", 0, 0)
            l2_candidates = self._find_l2_candidates(conn, "cache_exact", uncached, stop_check=stop_check)
            if l2_candidates and not (stop_check and stop_check()):
                upserts = []
                stale_candidates: Set[str] = set()
                active_new_paths: Set[str] = set()

                for i in range(0, len(l2_candidates), chunk_size):
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
                                upserts.append((new_path_str, rec.size, rec.mtime, p_hash, f_hash, rec.volume_id, rec.file_id))
                                if c_path != new_path_str:
                                    stale_candidates.add(c_path)

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
                self._cleanup_dead_paths(conn, "cache_exact", stale_candidates, active_new_paths)
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
        Retorna dicionário {caminho: embedding_bytes} para imagens com cache válido.
        Valida tamanho em bytes do embedding para garantir compatibilidade estrutural.
        Executa arquitetura tríbrida L1 -> L2 Lazy -> L3 Paralelo:
        - L1: Verificação direta por caminho + tamanho + mtime (zero I/O), com backfill legado.
        - L2: Resiliência NTFS por File ID (st_dev + st_ino) sob demanda para misses de L1.
        - L3: Content Sieve paralelo por tamanho + prefix_hash (xxHash64) em lote para exFAT/outros volumes.
        Suporta cancelamento imediato via stop_check.
        """
        results: Dict[str, bytes] = {}
        if not records:
            return results

        chunk_size = 900
        total_recs = len(records)
        backfill_recs: List[FileRecord] = []

        with self._get_connection() as conn:
            # 1. Tier 1 (L1 - In-place Path): Consulta rápida por caminho direto
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
                        if expected_bytes is None or len(emb_blob) == expected_bytes:
                            results[c_path] = emb_blob
                            if c_vol_id == 0 or c_f_id == 0:
                                backfill_recs.append(rec)

                if progress_callback and total_recs > 2000 and (i % 2700 == 0 or i + chunk_size >= total_recs):
                    done = min(i + chunk_size, total_recs)
                    progress_callback(f"Checking cache ({done:,}/{total_recs:,})...", done, total_recs)

            if backfill_recs:
                self._backfill_l1_file_ids(conn, "cache_images", backfill_recs, stop_check=stop_check)

            # 2. Tier 2 (L2 - NTFS File ID Lazy): Resiliência a pastas e arquivos movidos no mesmo volume
            uncached = [r for r in records if str(r.path) not in results]
            l2_candidates = self._find_l2_candidates(conn, "cache_images", uncached, stop_check=stop_check)
            if l2_candidates and not (stop_check and stop_check()):
                upserts = []
                stale_candidates: Set[str] = set()
                active_new_paths: Set[str] = set()

                for i in range(0, len(l2_candidates), chunk_size):
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
                        if expected_bytes is not None and len(emb_blob) != expected_bytes:
                            continue
                        for rec in l2_map.get((c_f_id, c_vol_id), ()):
                            if rec.size == c_size and abs(rec.mtime - c_mtime) < 0.001:
                                new_path_str = str(rec.path)
                                results[new_path_str] = emb_blob
                                active_new_paths.add(new_path_str)
                                upserts.append((new_path_str, rec.size, rec.mtime, emb_blob, rec.volume_id, rec.file_id, c_prefix))
                                if c_path != new_path_str:
                                    stale_candidates.add(c_path)

                if upserts:
                    conn.executemany("""
                        INSERT INTO cache_images (path, size, mtime, embedding, volume_id, file_id, prefix_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(path) DO UPDATE SET
                            size=excluded.size, mtime=excluded.mtime, embedding=excluded.embedding,
                            volume_id=excluded.volume_id, file_id=excluded.file_id,
                            prefix_hash=COALESCE(excluded.prefix_hash, cache_images.prefix_hash);
                    """, upserts)
                self._cleanup_dead_paths(conn, "cache_images", stale_candidates, active_new_paths)

            # 3. Tier 3 (L3 - Content Sieve Paralelo em Lote): Para exFAT, FAT32 ou cópias entre volumes
            uncached_l3 = [r for r in records if str(r.path) not in results]
            if uncached_l3 and not (stop_check and stop_check()):
                known_sizes = self._prefilter_known_sizes(conn, "cache_images", uncached_l3, "prefix_hash IS NOT NULL")
                if known_sizes:
                    sieve_candidates = [r for r in uncached_l3 if r.size in known_sizes]
                    self._populate_prefix_hashes_parallel(sieve_candidates, stop_check=stop_check)
                    hashed_candidates = [r for r in sieve_candidates if r.prefix_hash]
                    l3_upserts = []

                    for i in range(0, len(hashed_candidates), chunk_size):
                        if stop_check and stop_check():
                            return results
                        chunk = hashed_candidates[i:i + chunk_size]
                        unique_hashes = list({r.prefix_hash for r in chunk if r.prefix_hash})
                        if not unique_hashes:
                            continue
                        placeholders = ",".join("?" for _ in unique_hashes)
                        cursor = conn.execute(
                            f"SELECT size, prefix_hash, embedding FROM cache_images WHERE prefix_hash IN ({placeholders}) AND prefix_hash IS NOT NULL",
                            unique_hashes
                        )
                        match_map: Dict[Tuple[int, str], bytes] = {}
                        for r_size, r_phash, emb_blob in cursor.fetchall():
                            if expected_bytes is None or len(emb_blob) == expected_bytes:
                                match_map.setdefault((r_size, r_phash), emb_blob)

                        for rec in chunk:
                            emb_blob = match_map.get((rec.size, rec.prefix_hash))  # type: ignore[arg-type]
                            if emb_blob is not None:
                                populate_record_file_id(rec)
                                new_path_str = str(rec.path)
                                results[new_path_str] = emb_blob
                                l3_upserts.append((new_path_str, rec.size, rec.mtime, emb_blob, rec.volume_id, rec.file_id, rec.prefix_hash))

                    if l3_upserts:
                        conn.executemany("""
                            INSERT INTO cache_images (path, size, mtime, embedding, volume_id, file_id, prefix_hash)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(path) DO UPDATE SET
                                size=excluded.size, mtime=excluded.mtime, embedding=excluded.embedding,
                                volume_id=CASE WHEN excluded.volume_id != 0 THEN excluded.volume_id ELSE cache_images.volume_id END,
                                file_id=CASE WHEN excluded.file_id != 0 THEN excluded.file_id ELSE cache_images.file_id END,
                                prefix_hash=excluded.prefix_hash;
                        """, l3_upserts)
        return results

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
    # 3. Cache para Modo VÍDEOS (Duração + Embeddings Compostos FP16)
    # -------------------------------------------------------------------------

    def get_video_cache(
        self,
        records: List[FileRecord],
        expected_bytes: Optional[int] = None,
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None
    ) -> Dict[str, Tuple[float, bytes]]:
        """
        Retorna dicionário {caminho: (duracao, embedding_bytes)} para vídeos válidos no cache.
        Valida tamanho em bytes do embedding para garantir compatibilidade estrutural.
        Executa arquitetura tríbrida L1 -> L2 Lazy -> L3 Paralelo:
        - L1: Verificação direta por caminho + tamanho + mtime (zero I/O), com backfill legado.
        - L2: Resiliência NTFS por File ID (st_dev + st_ino) sob demanda para misses de L1.
        - L3: Content Sieve paralelo por tamanho + prefix_hash (xxHash64) em lote para exFAT/outros volumes.
        Suporta cancelamento imediato via stop_check.
        """
        results: Dict[str, Tuple[float, bytes]] = {}
        if not records:
            return results

        chunk_size = 900
        total_recs = len(records)
        backfill_recs: List[FileRecord] = []

        with self._get_connection() as conn:
            # 1. Tier 1 (L1 - In-place Path): Consulta rápida por caminho direto
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

                if progress_callback and total_recs > 2000 and (i % 2700 == 0 or i + chunk_size >= total_recs):
                    done = min(i + chunk_size, total_recs)
                    progress_callback(f"Checking video cache ({done:,}/{total_recs:,})...", done, total_recs)

            if backfill_recs:
                self._backfill_l1_file_ids(conn, "cache_videos", backfill_recs, stop_check=stop_check)

            # 2. Tier 2 (L2 - NTFS File ID Lazy): Resiliência a pastas e arquivos movidos no mesmo volume
            uncached = [r for r in records if str(r.path) not in results]
            l2_candidates = self._find_l2_candidates(conn, "cache_videos", uncached, stop_check=stop_check)
            if l2_candidates and not (stop_check and stop_check()):
                upserts = []
                stale_candidates: Set[str] = set()
                active_new_paths: Set[str] = set()

                for i in range(0, len(l2_candidates), chunk_size):
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
                                upserts.append((new_path_str, rec.size, rec.mtime, dur, emb_blob, rec.volume_id, rec.file_id, c_prefix))
                                if c_path != new_path_str:
                                    stale_candidates.add(c_path)

                if upserts:
                    conn.executemany("""
                        INSERT INTO cache_videos (path, size, mtime, duration, embedding, volume_id, file_id, prefix_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(path) DO UPDATE SET
                            size=excluded.size, mtime=excluded.mtime, duration=excluded.duration,
                            embedding=excluded.embedding, volume_id=excluded.volume_id, file_id=excluded.file_id,
                            prefix_hash=COALESCE(excluded.prefix_hash, cache_videos.prefix_hash);
                    """, upserts)
                self._cleanup_dead_paths(conn, "cache_videos", stale_candidates, active_new_paths)

            # 3. Tier 3 (L3 - Content Sieve Paralelo em Lote): Para exFAT, FAT32 ou cópias entre volumes
            uncached_l3 = [r for r in records if str(r.path) not in results]
            if uncached_l3 and not (stop_check and stop_check()):
                known_sizes = self._prefilter_known_sizes(conn, "cache_videos", uncached_l3, "prefix_hash IS NOT NULL")
                if known_sizes:
                    sieve_candidates = [r for r in uncached_l3 if r.size in known_sizes]
                    self._populate_prefix_hashes_parallel(sieve_candidates, stop_check=stop_check)
                    hashed_candidates = [r for r in sieve_candidates if r.prefix_hash]
                    l3_upserts = []

                    for i in range(0, len(hashed_candidates), chunk_size):
                        if stop_check and stop_check():
                            return results
                        chunk = hashed_candidates[i:i + chunk_size]
                        unique_hashes = list({r.prefix_hash for r in chunk if r.prefix_hash})
                        if not unique_hashes:
                            continue
                        placeholders = ",".join("?" for _ in unique_hashes)
                        cursor = conn.execute(
                            f"SELECT size, prefix_hash, mtime, duration, embedding FROM cache_videos WHERE prefix_hash IN ({placeholders}) AND prefix_hash IS NOT NULL",
                            unique_hashes
                        )
                        match_map: Dict[Tuple[int, str], List[Tuple[float, float, bytes]]] = defaultdict(list)
                        for r_size, r_phash, r_mtime, dur, emb_blob in cursor.fetchall():
                            is_valid_tombstone = (dur == -1.0 and len(emb_blob) == 0)
                            if is_valid_tombstone or expected_bytes is None or len(emb_blob) == expected_bytes:
                                match_map[(r_size, r_phash)].append((r_mtime, dur, emb_blob))

                        for rec in chunk:
                            candidates_row = match_map.get((rec.size, rec.prefix_hash))  # type: ignore[arg-type]
                            if not candidates_row:
                                continue
                            is_dav = rec.ext == ".dav" or str(rec.path).lower().endswith(".dav")
                            matched_entry = None
                            for r_mtime, dur, emb_blob in candidates_row:
                                # Arquivos .dav pré-alocados de CFTV exigem proximidade de mtime para evitar colisão de container vazio
                                if is_dav and abs(rec.mtime - r_mtime) > 2.0:
                                    continue
                                matched_entry = (dur, emb_blob)
                                break

                            if matched_entry is not None:
                                dur, emb_blob = matched_entry
                                populate_record_file_id(rec)
                                new_path_str = str(rec.path)
                                results[new_path_str] = (dur, emb_blob)
                                l3_upserts.append((new_path_str, rec.size, rec.mtime, dur, emb_blob, rec.volume_id, rec.file_id, rec.prefix_hash))

                    if l3_upserts:
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
        return results

    def save_video_batch(self, entries: List[Tuple]):
        """Salva lote de vídeos decodificados no cache."""
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
    # 4. Estatísticas e Manutenção
    # -------------------------------------------------------------------------

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
        """Executa checkpoint manual do WAL para truncar o arquivo de log."""
        try:
            with self._get_connection() as conn:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
        except Exception:
            pass

    def clear(self, mode: Optional[ScanMode] = None):
        """Limpa dados do cache para um modo específico ou para todos."""
        with self._get_connection() as conn:
            if mode == ScanMode.EXACT:
                conn.execute("DELETE FROM cache_exact;")
            elif mode == ScanMode.IMAGES:
                conn.execute("DELETE FROM cache_images;")
            elif mode == ScanMode.VIDEOS:
                conn.execute("DELETE FROM cache_videos;")
            else:
                conn.execute("DELETE FROM cache_exact;")
                conn.execute("DELETE FROM cache_images;")
                conn.execute("DELETE FROM cache_videos;")

        # VACUUM deve ser executado fora de transação em conexão limpa
        conn = sqlite3.connect(str(self.db_path), timeout=60.0)
        try:
            conn.isolation_level = None
            conn.execute("VACUUM;")
        finally:
            conn.close()
