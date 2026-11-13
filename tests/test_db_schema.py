"""
Testes unitários do validador e migrador de schema (dupeclean.core.db_schema).
Cobre criação inicial, fast-path, migração de bancos legados, auto-reparo de índices,
reset seguro preservando app_settings e proteção contra bancos de versões futuras.
"""

import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from dupeclean.core.cache import CacheManager
from dupeclean.core.db_schema import (
    CURRENT_SCHEMA_VERSION,
    EXPECTED_INDEXES,
    EXPECTED_TABLES,
    FutureSchemaError,
    ensure_database_schema,
    reset_database_preserving_settings,
)


class TestDbSchema(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="dupeclean_schema_")
        self.db_path = Path(self.temp_dir) / "DupeClean.db"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_fresh_db_creation_and_fast_path(self):
        """Banco novo é criado na versão atual; reabertura subsequente usa fast-path ('ok')."""
        res1 = ensure_database_schema(self.db_path)
        self.assertEqual(res1.status, "created")
        self.assertEqual(res1.to_version, CURRENT_SCHEMA_VERSION)

        with closing(sqlite3.connect(str(self.db_path))) as conn:
            uv = conn.execute("PRAGMA user_version;").fetchone()[0]
            self.assertEqual(uv, CURRENT_SCHEMA_VERSION)
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
                ).fetchall()
            }
            indexes = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index';").fetchall()
            }
            self.assertTrue(set(EXPECTED_TABLES.keys()).issubset(tables))
            self.assertTrue(EXPECTED_INDEXES.issubset(indexes))

        res2 = ensure_database_schema(self.db_path)
        self.assertEqual(res2.status, "ok")
        self.assertEqual(res2.from_version, CURRENT_SCHEMA_VERSION)

    def test_legacy_db_migration_preserves_valid_data_and_settings(self):
        """Banco v0 legado sem user_version nem colunas L2/L3 é adaptado para v1 mantendo dados válidos."""
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            conn.executescript(
                """
                CREATE TABLE cache_exact (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    prefix_hash TEXT,
                    full_hash TEXT
                );
                CREATE TABLE cache_images (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    embedding BLOB NOT NULL
                );
                CREATE TABLE cache_videos (
                    path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    duration REAL NOT NULL,
                    embedding BLOB NOT NULL
                );
                CREATE TABLE app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT INTO cache_exact VALUES ('C:\\\\a.bin', 100, 1.0, 'old_pre', 'full_1');"
            )
            conn.execute(
                "INSERT INTO cache_images VALUES ('C:\\\\ok.jpg', 200, 1.0, ?);",
                (b"\x01" * 1536,),
            )
            conn.execute(
                "INSERT INTO cache_images VALUES ('C:\\\\bad.jpg', 200, 1.0, ?);",
                (b"\x02" * 512,),
            )
            conn.execute(
                "INSERT INTO cache_videos VALUES ('C:\\\\tomb.mp4', 300, 1.0, -1.0, ?);",
                (b"",),
            )
            conn.execute(
                "INSERT INTO cache_videos VALUES ('C:\\\\bad.mp4', 300, 1.0, 12.0, ?);",
                (b"\x03" * 1024,),
            )
            conn.execute(
                "INSERT INTO app_settings VALUES ('saved_folders', '{\"included\": [\"C:\\\\Photos\"]}');"
            )
            conn.commit()

        cm = CacheManager(self.db_path)
        self.assertEqual(cm.schema_result.status, "migrated")
        self.assertEqual(cm.schema_result.from_version, 0)
        self.assertEqual(cm.schema_result.to_version, CURRENT_SCHEMA_VERSION)

        with closing(sqlite3.connect(str(self.db_path))) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version;").fetchone()[0], CURRENT_SCHEMA_VERSION)
            exact_row = conn.execute(
                "SELECT full_hash, prefix_hash, volume_id, file_id FROM cache_exact WHERE path='C:\\\\a.bin';"
            ).fetchone()
            self.assertEqual(exact_row[0], "full_1")
            self.assertIsNone(exact_row[1])  # prefix_hash v1 invalidado para v2_head_tail
            self.assertEqual((exact_row[2], exact_row[3]), (0, 0))

            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cache_images;").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cache_videos;").fetchone()[0], 1)
            self.assertEqual(cm.get_setting("saved_folders"), {"included": ["C:\\Photos"]})

    def test_v1_to_v2_migration_invalidates_only_image_cache(self):
        """Migração v1->v2 limpa apenas cache_images preservando cache_exact, cache_videos e settings."""
        CacheManager(self.db_path).save_setting("saved_folders", {"included": ["F:\\Photos"]})
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            conn.execute("INSERT INTO cache_exact (path, size, mtime, prefix_hash, full_hash) VALUES ('C:\\\\a.bin', 100, 1.0, 'p', 'f');")
            conn.execute("INSERT INTO cache_images (path, size, mtime, embedding) VALUES ('C:\\\\img.jpg', 200, 1.0, ?);", (b"\x01" * 1536,))
            conn.execute("INSERT INTO cache_videos (path, size, mtime, duration, embedding) VALUES ('C:\\\\vid.mp4', 300, 1.0, 10.0, ?);", (b"\x02" * 7680,))
            conn.execute("PRAGMA user_version = 1;")
            conn.commit()

        cm = CacheManager(self.db_path)
        self.assertEqual(cm.schema_result.status, "migrated")
        self.assertEqual(cm.schema_result.from_version, 1)
        self.assertEqual(cm.schema_result.to_version, CURRENT_SCHEMA_VERSION)
        stats = cm.get_stats()
        self.assertEqual(stats["exact_count"], 1)
        self.assertEqual(stats["images_count"], 0)
        self.assertEqual(stats["videos_count"], 1)
        self.assertEqual(cm.get_setting("saved_folders"), {"included": ["F:\\Photos"]})

    def test_missing_index_is_repaired_on_current_version(self):
        """Índice ausente em banco v1 é recriado automaticamente sem zerar cache."""
        CacheManager(self.db_path)
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            conn.execute("DROP INDEX idx_exact_fileid;")
            conn.commit()

        res = ensure_database_schema(self.db_path)
        self.assertEqual(res.status, "migrated")
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            idxs = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index';").fetchall()
            }
            self.assertIn("idx_exact_fileid", idxs)

    def test_incompatible_schema_resets_cache_preserving_app_settings(self):
        """Banco estruturalmente incompatível é zerado mantendo saved_folders e sem arquivos .bak."""
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            # cache_exact incompatível (sem coluna mtime e PK diferente)
            conn.execute("CREATE TABLE cache_exact (id INTEGER PRIMARY KEY, path TEXT);")
            conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
            conn.execute("INSERT INTO app_settings VALUES ('saved_folders', '{\"included\": [\"D:\\\\Media\"]}');")
            conn.commit()

        cm = CacheManager(self.db_path)
        self.assertEqual(cm.schema_result.status, "reset")
        self.assertTrue(cm.schema_result.preserved_settings)
        self.assertEqual(cm.get_setting("saved_folders"), {"included": ["D:\\Media"]})
        self.assertEqual(list(Path(self.temp_dir).glob("*.bak*")), [])

    def test_corrupted_db_with_stale_wal_recovers_cleanly(self):
        """Arquivo .db corrompido (com -wal residual) é recriado limpo sem travar startup."""
        self.db_path.write_bytes(b"NOT_A_VALID_SQLITE_HEADER_1234567890" * 16)
        wal_path = self.db_path.with_name(f"{self.db_path.name}-wal")
        wal_path.write_bytes(b"STALE_WAL_GARBAGE")

        cm = CacheManager(self.db_path)
        self.assertEqual(cm.schema_result.status, "reset")
        self.assertFalse(cm.schema_result.preserved_settings)
        self.assertEqual(cm.get_stats()["exact_count"], 0)

    def test_future_schema_raises_without_modifying_file_and_resets_explicitly(self):
        """Banco de versão futura (> X) dispara FutureSchemaError sem alterar o arquivo original."""
        CacheManager(self.db_path).save_setting("saved_folders", {"included": ["E:\\Keep"]})
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            conn.execute("INSERT INTO cache_exact (path, size, mtime) VALUES ('x', 1, 1.0);")
            conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 5};")
            conn.commit()

        with self.assertRaises(FutureSchemaError) as ctx:
            CacheManager(self.db_path)
        self.assertEqual(ctx.exception.db_version, CURRENT_SCHEMA_VERSION + 5)

        # Confirma que dados originais não foram tocados antes de confirmação explícita
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cache_exact;").fetchone()[0], 1)

        reset_res = reset_database_preserving_settings(self.db_path, reason="test")
        self.assertEqual(reset_res.status, "reset")
        self.assertTrue(reset_res.preserved_settings)
        cm = CacheManager(self.db_path)
        self.assertEqual(cm.get_setting("saved_folders"), {"included": ["E:\\Keep"]})
        self.assertEqual(cm.get_stats()["exact_count"], 0)


if __name__ == "__main__":
    unittest.main()
