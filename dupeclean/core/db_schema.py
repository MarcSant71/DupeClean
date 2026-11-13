"""
Validador, contrato estrutural e gerenciador de migrações do banco DupeClean.db.
Garante no startup que bancos antigos ou restaurados atendam à estrutura esperada
pela versão atual do executável, migrando com segurança ou reiniciando o cache
quando incompatíveis/corrompidos (preservando configurações do usuário).
"""

import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

CURRENT_SCHEMA_VERSION = 2

# Contrato estrutural esperado pelo executável atual: {tabela: (coluna_pk, {colunas})}
EXPECTED_TABLES: Dict[str, Tuple[str, Set[str]]] = {
    "cache_exact": (
        "path",
        {"path", "size", "mtime", "prefix_hash", "full_hash", "volume_id", "file_id"},
    ),
    "cache_images": (
        "path",
        {"path", "size", "mtime", "embedding", "volume_id", "file_id", "prefix_hash"},
    ),
    "cache_videos": (
        "path",
        {"path", "size", "mtime", "duration", "embedding", "volume_id", "file_id", "prefix_hash"},
    ),
    "app_settings": (
        "key",
        {"key", "value"},
    ),
}

EXPECTED_INDEXES: Set[str] = {
    "idx_exact_size",
    "idx_exact_fileid",
    "idx_exact_prefix_h",
    "idx_images_size",
    "idx_images_fileid",
    "idx_images_prefix_h",
    "idx_videos_size",
    "idx_videos_fileid",
    "idx_videos_prefix_h",
}

LEGACY_INDEXES: Tuple[str, ...] = (
    "idx_exact_lookup",
    "idx_images_lookup",
    "idx_videos_lookup",
    "idx_exact_prefix",
    "idx_images_prefix",
    "idx_videos_prefix",
)

# Colunas aditivas compatíveis com ALTER TABLE ADD COLUMN
_ADDITIVE_COLUMNS: Dict[str, List[Tuple[str, str]]] = {
    "cache_exact": [
        ("volume_id", "INTEGER DEFAULT 0"),
        ("file_id", "INTEGER DEFAULT 0"),
    ],
    "cache_images": [
        ("volume_id", "INTEGER DEFAULT 0"),
        ("file_id", "INTEGER DEFAULT 0"),
        ("prefix_hash", "TEXT"),
    ],
    "cache_videos": [
        ("volume_id", "INTEGER DEFAULT 0"),
        ("file_id", "INTEGER DEFAULT 0"),
        ("prefix_hash", "TEXT"),
    ],
}


@dataclass
class SchemaInitResult:
    """Resultado da validação/adaptação do schema na inicialização."""

    status: str  # "ok" | "created" | "migrated" | "reset"
    from_version: int
    to_version: int
    reason: Optional[str] = None
    preserved_settings: bool = False


class FutureSchemaError(Exception):
    """Lançada quando o .db no disco pertence a uma versão mais nova que a suportada."""

    def __init__(self, db_version: int, expected_version: int, db_path: Path):
        self.db_version = db_version
        self.expected_version = expected_version
        self.db_path = db_path
        super().__init__(
            f"Database schema v{db_version} is newer than supported v{expected_version}."
        )


def _apply_structural_pragmas(conn: sqlite3.Connection, is_fresh: bool = False) -> None:
    """Aplica parâmetros WAL e page_size (page_size somente na criação do arquivo)."""
    if is_fresh:
        conn.execute("PRAGMA page_size = 8192;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA journal_size_limit = 67108864;")
    conn.execute("PRAGMA wal_autocheckpoint = 2000;")


def _create_all_tables(conn: sqlite3.Connection) -> None:
    """Cria todas as tabelas do contrato estrutural caso não existam."""
    conn.executescript(
        """
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
        """
    )


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    """Remove índices obsoletos e garante todos os índices parciais atuais."""
    for idx in LEGACY_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {idx};")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_exact_size ON cache_exact(size);")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_exact_fileid ON cache_exact(file_id, volume_id) WHERE file_id != 0;"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_exact_prefix_h ON cache_exact(prefix_hash, size) WHERE prefix_hash IS NOT NULL;"
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_images_size ON cache_images(size);")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_images_fileid ON cache_images(file_id, volume_id) WHERE file_id != 0;"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_images_prefix_h ON cache_images(prefix_hash, size) WHERE prefix_hash IS NOT NULL;"
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_size ON cache_videos(size);")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_videos_fileid ON cache_videos(file_id, volume_id) WHERE file_id != 0;"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_videos_prefix_h ON cache_videos(prefix_hash, size) WHERE prefix_hash IS NOT NULL;"
    )


def _inspect_schema(conn: sqlite3.Connection) -> Tuple[bool, bool, str]:
    """
    Verifica tabelas, chaves primárias, colunas obrigatórias e índices.
    Retorna (tables_and_cols_ok, indexes_ok, detail_reason).
    """
    existing_tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%';"
        ).fetchall()
    }

    for table_name, (expected_pk, expected_cols) in EXPECTED_TABLES.items():
        if table_name not in existing_tables:
            return False, False, f"missing table '{table_name}'"

        info = conn.execute(f"PRAGMA table_info({table_name});").fetchall()
        col_names = {row[1] for row in info}
        pk_cols = {row[1] for row in info if row[5] > 0}

        missing_cols = expected_cols - col_names
        if missing_cols:
            return False, False, f"missing columns in '{table_name}': {sorted(missing_cols)}"
        if pk_cols != {expected_pk}:
            return False, False, f"incompatible PK in '{table_name}': {sorted(pk_cols)}"

    existing_indexes = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index';").fetchall()
    }
    missing_indexes = EXPECTED_INDEXES - existing_indexes
    if missing_indexes:
        return True, False, f"missing indexes: {sorted(missing_indexes)}"

    return True, True, "ok"


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Adiciona colunas evolutivas previstas em tabelas já existentes."""
    for table_name, cols_to_add in _ADDITIVE_COLUMNS.items():
        existing_cols = {
            row[1] for row in conn.execute(f"PRAGMA table_info({table_name});").fetchall()
        }
        for col_name, col_def in cols_to_add:
            if col_name not in existing_cols:
                conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def};")


def _set_signature_version_v2(conn: sqlite3.Connection, invalidate_if_needed: bool) -> None:
    """Garante marcação v2_head_tail para assinaturas exatas Head 4KB + Tail 4KB."""
    if invalidate_if_needed:
        cur = conn.execute("SELECT value FROM app_settings WHERE key = 'signature_version';")
        row = cur.fetchone()
        if not row or row[0] != "v2_head_tail":
            conn.execute("UPDATE cache_exact SET prefix_hash = NULL;")
    conn.execute(
        """
        INSERT INTO app_settings (key, value) VALUES ('signature_version', 'v2_head_tail')
        ON CONFLICT(key) DO UPDATE SET value = excluded.value;
        """
    )


def _migrate_0_to_1(conn: sqlite3.Connection) -> None:
    """Adapta bancos legados sem PRAGMA user_version para a estrutura v1."""
    _create_all_tables(conn)
    _add_missing_columns(conn)
    _ensure_indexes(conn)

    # Purga embeddings de dimensões obsoletas preservando tombstones (duration == -1.0)
    conn.execute("DELETE FROM cache_images WHERE length(embedding) != 1536 AND length(embedding) != 0;")
    conn.execute("DELETE FROM cache_videos WHERE length(embedding) != 7680 AND duration != -1.0;")
    _set_signature_version_v2(conn, invalidate_if_needed=True)


def _migrate_1_to_2(conn: sqlite3.Connection) -> None:
    """Invalida apenas cache_images após recalibração visual do motor de imagens (EXIF, alpha, Hamming, ALPHA=1.2)."""
    conn.execute("DELETE FROM cache_images;")


MIGRATIONS: Dict[int, Callable[[sqlite3.Connection], None]] = {
    0: _migrate_0_to_1,
    1: _migrate_1_to_2,
}


def _rescue_app_settings(db_path: Path) -> List[Tuple[str, str]]:
    """Tenta resgatar preferências do usuário em app_settings antes de recriar o banco."""
    if not db_path.exists():
        return []
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        rows = conn.execute(
            "SELECT key, value FROM app_settings WHERE key != 'signature_version';"
        ).fetchall()
        return [(str(k), str(v)) for k, v in rows]
    except Exception:
        return []
    finally:
        if conn is not None:
            with suppress(Exception):
                conn.close()


def _unlink_db_files(db_path: Path) -> None:
    """Remove o .db e arquivos auxiliares (-wal, -shm, -journal) para recriação limpa."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        target = db_path if not suffix else db_path.with_name(f"{db_path.name}{suffix}")
        if target.exists():
            target.unlink()


def _create_fresh_db(db_path: Path, rescued_settings: Optional[List[Tuple[str, str]]] = None) -> None:
    """Cria um banco limpo na versão atual e restaura app_settings resgatadas."""
    conn = sqlite3.connect(str(db_path), timeout=60.0)
    try:
        _apply_structural_pragmas(conn, is_fresh=True)
        _create_all_tables(conn)
        _ensure_indexes(conn)
        _set_signature_version_v2(conn, invalidate_if_needed=False)
        if rescued_settings:
            conn.executemany(
                "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?);",
                rescued_settings,
            )
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION};")
        conn.commit()
    finally:
        conn.close()


def _recreate_tables_in_place(db_path: Path, rescued_settings: List[Tuple[str, str]]) -> None:
    """Fallback caso o arquivo esteja travado para unlink: derruba e recria tabelas na própria base."""
    conn = sqlite3.connect(str(db_path), timeout=60.0)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%';"
            ).fetchall()
        ]
        for tbl in tables:
            conn.execute(f'DROP TABLE IF EXISTS "{tbl}";')
        _apply_structural_pragmas(conn, is_fresh=False)
        _create_all_tables(conn)
        _ensure_indexes(conn)
        _set_signature_version_v2(conn, invalidate_if_needed=False)
        if rescued_settings:
            conn.executemany(
                "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?);",
                rescued_settings,
            )
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION};")
        conn.commit()
    finally:
        conn.close()


def reset_database_preserving_settings(db_path: Path, reason: str = "reset") -> SchemaInitResult:
    """Recria o banco do zero preservando app_settings legíveis e sem manter backup antigo."""
    rescued = _rescue_app_settings(db_path)
    try:
        _unlink_db_files(db_path)
        _create_fresh_db(db_path, rescued_settings=rescued)
    except PermissionError:
        _recreate_tables_in_place(db_path, rescued)
    return SchemaInitResult(
        status="reset",
        from_version=0,
        to_version=CURRENT_SCHEMA_VERSION,
        reason=reason,
        preserved_settings=bool(rescued),
    )


def ensure_database_schema(db_path: Path) -> SchemaInitResult:
    """
    Valida no startup se o arquivo .db atende ao schema do executável atual.
    - Se vazio/inexistente: cria na versão atual.
    - Se versão antiga compatível: aplica migrações e atualiza user_version silenciosamente.
    - Se versão futura (> CURRENT_SCHEMA_VERSION): lança FutureSchemaError sem modificar o arquivo.
    - Se corrompido ou estruturalmente incompatível: preserva app_settings e recria limpo.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    known_from_version = 0
    conn: Optional[sqlite3.Connection] = None

    try:
        conn = sqlite3.connect(str(db_path), timeout=60.0)
        known_from_version = int(conn.execute("PRAGMA user_version;").fetchone()[0])

        if known_from_version > CURRENT_SCHEMA_VERSION:
            raise FutureSchemaError(known_from_version, CURRENT_SCHEMA_VERSION, db_path)

        existing_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%';"
            ).fetchall()
        }

        # 1. Banco recém-criado ou vazio
        if not existing_tables and known_from_version == 0:
            conn.close()
            conn = None
            _create_fresh_db(db_path)
            return SchemaInitResult(
                status="created",
                from_version=0,
                to_version=CURRENT_SCHEMA_VERSION,
            )

        _apply_structural_pragmas(conn, is_fresh=False)

        # 2. Fast-path: já está marcado na versão atual
        if known_from_version == CURRENT_SCHEMA_VERSION:
            tables_ok, indexes_ok, detail = _inspect_schema(conn)
            if tables_ok and indexes_ok:
                conn.commit()
                return SchemaInitResult(
                    status="ok",
                    from_version=known_from_version,
                    to_version=CURRENT_SCHEMA_VERSION,
                )
            if tables_ok and not indexes_ok:
                _ensure_indexes(conn)
                conn.commit()
                return SchemaInitResult(
                    status="migrated",
                    from_version=known_from_version,
                    to_version=CURRENT_SCHEMA_VERSION,
                    reason=detail,
                )
            raise ValueError(f"Schema marked v{known_from_version} failed validation: {detail}")

        # 3. Versão antiga (0 <= db_ver < CURRENT_SCHEMA_VERSION): verifica integridade e migra
        qc = conn.execute("PRAGMA quick_check(1);").fetchone()
        if not qc or qc[0] != "ok":
            raise sqlite3.DatabaseError(f"Integrity check failed: {qc}")

        ver = known_from_version
        while ver < CURRENT_SCHEMA_VERSION:
            migrator = MIGRATIONS.get(ver)
            if migrator is None:
                raise ValueError(f"No migration path from schema v{ver} to v{ver + 1}.")
            migrator(conn)
            ver += 1

        tables_ok, indexes_ok, detail = _inspect_schema(conn)
        if not (tables_ok and indexes_ok):
            raise ValueError(f"Post-migration validation failed: {detail}")

        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION};")
        conn.commit()
        return SchemaInitResult(
            status="migrated",
            from_version=known_from_version,
            to_version=CURRENT_SCHEMA_VERSION,
        )

    except FutureSchemaError:
        raise
    except (sqlite3.DatabaseError, ValueError) as exc:
        if conn is not None:
            with suppress(Exception):
                conn.close()
            conn = None
        result = reset_database_preserving_settings(db_path, reason=str(exc))
        result.from_version = known_from_version
        return result
    finally:
        if conn is not None:
            with suppress(Exception):
                conn.close()
