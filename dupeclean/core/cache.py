"""
Gerenciador de Cache Persistente (SQLite WAL).
Armazena hashes e embeddings visuais para evitar reprocessamento desnecessário,
permitindo escaneamentos quase instantâneos e retenção total de progresso em cancelamentos.
"""

import os
import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
import numpy as np
import torch

from dupeclean.core.models import FileRecord, ScanMode


def get_default_cache_db_path() -> Path:
    """Retorna o caminho centralizado do banco de cache, criando a pasta se necessário."""
    base_dir = Path(__file__).resolve().parent.parent.parent  # Raiz do DupeClean
    cache_dir = base_dir / "cache"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / "cache.db"
    except Exception:
        appdata = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "DupeClean"
        appdata.mkdir(parents=True, exist_ok=True)
        return appdata / "cache.db"


class CacheManager:
    """
    Gerenciador thread-safe de cache SQLite em modo WAL.
    Suporta transações em lote e consultas rápidas por caminho, tamanho e data de modificação.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or get_default_cache_db_path()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Cria e configura conexão SQLite otimizada com WAL."""
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA cache_size=-64000;")  # 64 MB de cache em RAM
        conn.execute("PRAGMA temp_store=MEMORY;")
        return conn

    def _init_db(self):
        """Cria as tabelas e índices de cache se não existirem."""
        with self._get_connection() as conn:
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

                CREATE INDEX IF NOT EXISTS idx_exact_lookup ON cache_exact(path, size, mtime);
                CREATE INDEX IF NOT EXISTS idx_images_lookup ON cache_images(path, size, mtime);
                CREATE INDEX IF NOT EXISTS idx_videos_lookup ON cache_videos(path, size, mtime);
            """)

    # -------------------------------------------------------------------------
    # 1. Cache para Modo EXATO (xxHash64)
    # -------------------------------------------------------------------------

    def get_exact_cache(self, records: List[FileRecord]) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
        """
        Retorna dicionário {caminho: (prefix_hash, full_hash)} para registros válidos.
        Invalida caso o tamanho ou mtime tenham mudado.
        """
        results: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        if not records:
            return results

        chunk_size = 900
        with self._get_connection() as conn:
            for i in range(0, len(records), chunk_size):
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

    def get_image_cache(self, records: List[FileRecord]) -> Dict[str, bytes]:
        """
        Retorna dicionário {caminho: embedding_bytes} para imagens com cache válido.
        """
        results: Dict[str, bytes] = {}
        if not records:
            return results

        chunk_size = 900
        with self._get_connection() as conn:
            for i in range(0, len(records), chunk_size):
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

    def get_video_cache(self, records: List[FileRecord]) -> Dict[str, Tuple[float, bytes]]:
        """
        Retorna dicionário {caminho: (duracao, embedding_bytes)} para vídeos válidos no cache.
        """
        results: Dict[str, Tuple[float, bytes]] = {}
        if not records:
            return results

        chunk_size = 900
        with self._get_connection() as conn:
            for i in range(0, len(records), chunk_size):
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
        # VACUUM deve ser executado fora de transação
        conn = self._get_connection()
        try:
            conn.isolation_level = None
            conn.execute("VACUUM;")
        finally:
            conn.close()
