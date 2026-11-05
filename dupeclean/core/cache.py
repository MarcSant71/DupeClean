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
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any, Callable
import numpy as np
import torch

from dupeclean.core.models import FileRecord, ScanMode, ScanPath


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
                    full_hash TEXT
                );

                CREATE TABLE IF NOT EXISTS cache_images (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    embedding BLOB NOT NULL
                );

                CREATE TABLE IF NOT EXISTS cache_videos (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    duration REAL NOT NULL,
                    embedding BLOB NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            # Remove índices secundários redundantes (PRIMARY KEY já provê índice B-Tree único ótimo)
            conn.execute("DROP INDEX IF EXISTS idx_exact_lookup;")
            conn.execute("DROP INDEX IF EXISTS idx_images_lookup;")
            conn.execute("DROP INDEX IF EXISTS idx_videos_lookup;")
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
    # 1. Cache para Modo EXATO (xxHash64)
    # -------------------------------------------------------------------------

    def get_exact_cache(
        self,
        records: List[FileRecord],
        stop_check: Optional[Callable[[], bool]] = None
    ) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
        """
        Retorna dicionário {caminho: (prefix_hash, full_hash)} para registros válidos.
        Invalida caso o tamanho ou mtime tenham mudado. Suporta cancelamento atômico via stop_check.
        """
        results: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        if not records:
            return results

        chunk_size = 900
        with self._get_connection() as conn:
            for i in range(0, len(records), chunk_size):
                if stop_check and stop_check():
                    return results
                chunk = records[i:i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                paths = [str(r.path) for r in chunk]
                cursor = conn.execute(
                    f"SELECT path, size, mtime, prefix_hash, full_hash FROM cache_exact WHERE path IN ({placeholders})",
                    paths
                )
                record_map = {str(r.path): r for r in chunk}
                for row in cursor.fetchall():
                    c_path, c_size, c_mtime, p_hash, f_hash = row
                    rec = record_map.get(c_path)
                    if rec and rec.size == c_size and abs(rec.mtime - c_mtime) < 0.001:
                        results[c_path] = (p_hash, f_hash)
        return results

    def save_exact_batch(self, entries: List[Tuple[str, int, float, Optional[str], Optional[str]]]):
        """Salva ou atualiza hashes em lote no cache exato."""
        if not entries:
            return
        with self._get_connection() as conn:
            conn.executemany("""
                INSERT INTO cache_exact (path, size, mtime, prefix_hash, full_hash)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size=excluded.size,
                    mtime=excluded.mtime,
                    prefix_hash=COALESCE(excluded.prefix_hash, cache_exact.prefix_hash),
                    full_hash=COALESCE(excluded.full_hash, cache_exact.full_hash);
            """, entries)

    # -------------------------------------------------------------------------
    # 2. Cache para Modo IMAGENS (Embeddings 2D-DCT FP16)
    # -------------------------------------------------------------------------

    def get_image_cache(
        self,
        records: List[FileRecord],
        progress_callback: Optional[Callable[[str, int, int], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None
    ) -> Dict[str, bytes]:
        """
        Retorna dicionário {caminho: embedding_bytes} para imagens com cache válido.
        Suporta cancelamento imediato via stop_check.
        """
        results: Dict[str, bytes] = {}
        if not records:
            return results

        chunk_size = 900
        total_recs = len(records)
        with self._get_connection() as conn:
            for i in range(0, total_recs, chunk_size):
                if stop_check and stop_check():
                    return results
                chunk = records[i:i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                paths = [str(r.path) for r in chunk]
                cursor = conn.execute(
                    f"SELECT path, size, mtime, embedding FROM cache_images WHERE path IN ({placeholders})",
                    paths
                )
                record_map = {str(r.path): r for r in chunk}
                for row in cursor.fetchall():
                    c_path, c_size, c_mtime, emb_blob = row
                    rec = record_map.get(c_path)
                    if rec and rec.size == c_size and abs(rec.mtime - c_mtime) < 0.001:
                        results[c_path] = emb_blob

                if progress_callback and total_recs > 2000 and (i % 2700 == 0 or i + chunk_size >= total_recs):
                    done = min(i + chunk_size, total_recs)
                    progress_callback(f"Checking cache ({done:,}/{total_recs:,})...", done, total_recs)

        return results

    def save_image_batch(self, entries: List[Tuple[str, int, float, bytes]]):
        """Salva lote de embeddings de imagem no cache."""
        if not entries:
            return
        with self._get_connection() as conn:
            conn.executemany("""
                INSERT INTO cache_images (path, size, mtime, embedding)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size=excluded.size,
                    mtime=excluded.mtime,
                    embedding=excluded.embedding;
            """, entries)

    # -------------------------------------------------------------------------
    # 3. Cache para Modo VÍDEOS (Duração + Embeddings Compostos FP16)
    # -------------------------------------------------------------------------

    def get_video_cache(
        self,
        records: List[FileRecord],
        stop_check: Optional[Callable[[], bool]] = None
    ) -> Dict[str, Tuple[float, bytes]]:
        """
        Retorna dicionário {caminho: (duracao, embedding_bytes)} para vídeos válidos no cache.
        Suporta cancelamento imediato via stop_check.
        """
        results: Dict[str, Tuple[float, bytes]] = {}
        if not records:
            return results

        chunk_size = 900
        with self._get_connection() as conn:
            for i in range(0, len(records), chunk_size):
                if stop_check and stop_check():
                    return results
                chunk = records[i:i + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                paths = [str(r.path) for r in chunk]
                cursor = conn.execute(
                    f"SELECT path, size, mtime, duration, embedding FROM cache_videos WHERE path IN ({placeholders})",
                    paths
                )
                record_map = {str(r.path): r for r in chunk}
                for row in cursor.fetchall():
                    c_path, c_size, c_mtime, dur, emb_blob = row
                    rec = record_map.get(c_path)
                    if rec and rec.size == c_size and abs(rec.mtime - c_mtime) < 0.001:
                        results[c_path] = (dur, emb_blob)
        return results

    def save_video_batch(self, entries: List[Tuple[str, int, float, float, bytes]]):
        """Salva lote de vídeos decodificados no cache."""
        if not entries:
            return
        with self._get_connection() as conn:
            conn.executemany("""
                INSERT INTO cache_videos (path, size, mtime, duration, embedding)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size=excluded.size,
                    mtime=excluded.mtime,
                    duration=excluded.duration,
                    embedding=excluded.embedding;
            """, entries)

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
