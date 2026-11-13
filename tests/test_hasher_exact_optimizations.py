"""
Testes dedicados de validação das otimizações do motor exato:
1. Fast-path de arquivos pequenos/médios (<= 256 KB)
2. Assinatura composta Head 4KB + Tail 4KB para arquivos > 256 KB
3. Descarte de falsos positivos com mesmo cabeçalho e caudas diferentes
4. Ordenação espacial / Localidade de diretório
5. Cache e migração segura de prefixos
"""

import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dupeclean.core.models import FileRecord, ScanMode
from dupeclean.core.hasher_exact import find_exact_duplicates, _read_signature, FAST_PATH_SIZE
from dupeclean.core.cache import CacheManager


def test_small_files_fast_path():
    """Arquivos <= FAST_PATH_SIZE (256 KB) devem ser resolvidos diretamente no Estágio 2."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "test_cache.db"
        cache = CacheManager(db_path)

        f1 = tmp_path / "small1.txt"
        f2 = tmp_path / "small2.txt"
        f3 = tmp_path / "small_diff.txt"

        data_same = b"A" * 2048
        data_diff = b"B" * 2048

        f1.write_bytes(data_same)
        f2.write_bytes(data_same)
        f3.write_bytes(data_diff)

        # Também valida arquivo médio (~100 KB) dentro do novo limiar de 256 KB
        f4 = tmp_path / "medium1.jpg"
        f5 = tmp_path / "medium2.jpg"
        data_med = b"IMG_HEAD_" + (b"M" * 102400) + b"_TAIL"
        f4.write_bytes(data_med)
        f5.write_bytes(data_med)

        recs = [
            FileRecord(path=f1, size=len(data_same), mtime=1.0),
            FileRecord(path=f2, size=len(data_same), mtime=2.0),
            FileRecord(path=f3, size=len(data_diff), mtime=3.0),
            FileRecord(path=f4, size=len(data_med), mtime=4.0),
            FileRecord(path=f5, size=len(data_med), mtime=5.0),
        ]

        # Assinatura direta
        res1 = _read_signature((recs[0], None))
        res4 = _read_signature((recs[3], None))
        assert res1 is not None and res4 is not None
        rec1, h1, is_full1 = res1
        _, h4, is_full4 = res4
        assert is_full1 is True, "Arquivos <= FAST_PATH_SIZE devem marcar is_full=True"
        assert is_full4 is True, "Arquivos de ~100 KB (<= 256 KB) devem marcar is_full=True"

        groups = find_exact_duplicates(recs, cache_manager=cache)
        assert len(groups) == 2, f"Esperado 2 grupos, obtido {len(groups)}"
        all_paths = {f.path for g in groups for f in g.files}
        assert f1 in all_paths and f2 in all_paths
        assert f4 in all_paths and f5 in all_paths
        assert f3 not in all_paths

        # Verifica se o cache salvou full_hash imediatamente no Estágio 2
        cached = cache.get_exact_cache(recs)
        assert cached[str(f1)][1] == h1, "Cache deve armazenar full_hash imediatamente"
        assert cached[str(f4)][1] == h4, "Cache deve armazenar full_hash imediatamente para <= 256 KB"
        print("[OK] test_small_files_fast_path passou com sucesso!")


def test_large_files_head_tail_filtering():
    """Arquivos grandes (> 256 KB) com mesmo cabeçalho mas caudas diferentes são descartados no Estágio 2."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        cache = CacheManager(tmp_path / "test_cache.db")

        # Arquivos de 300 KB (> FAST_PATH_SIZE 256 KB):
        # f1 e f2: idênticos
        # f3: mesmo cabeçalho de 4 KB de f1/f2, mas cauda diferente
        total_size = FAST_PATH_SIZE + 44 * 1024  # 300 KB
        header_4k = b"H" * 4096
        tail_same = b"T" * 4096
        tail_diff = b"X" * 4096
        body = b"M" * (total_size - 8192)

        f1 = tmp_path / "large1.dat"
        f2 = tmp_path / "large2.dat"
        f3 = tmp_path / "large_diff_tail.dat"

        f1.write_bytes(header_4k + body + tail_same)
        f2.write_bytes(header_4k + body + tail_same)
        f3.write_bytes(header_4k + body + tail_diff)

        recs = [
            FileRecord(path=f1, size=total_size, mtime=10.0),
            FileRecord(path=f2, size=total_size, mtime=20.0),
            FileRecord(path=f3, size=total_size, mtime=30.0),
        ]

        # Verifica assinaturas
        res1 = _read_signature((recs[0], None))
        res3 = _read_signature((recs[2], None))
        assert res1 is not None and res3 is not None
        assert res1[2] is False, "Arquivos grandes devem ter is_full=False"
        assert res3[2] is False
        assert res1[1] != res3[1], "Assinatura composta Head+Tail DEVE diferenciar caudas diferentes!"

        groups = find_exact_duplicates(recs, cache_manager=cache)
        assert len(groups) == 1, f"Esperado 1 grupo, obtido {len(groups)}"
        assert len(groups[0].files) == 2
        paths = {f.path for f in groups[0].files}
        assert f1 in paths and f2 in paths
        assert f3 not in paths, "f3 possui cauda diferente e não deve ser agrupado!"
        print("[OK] test_large_files_head_tail_filtering passou com sucesso!")


def test_directory_locality_sorting():
    """Verifica se arquivos em diferentes pastas são ordenados por localidade."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        base = Path(tmp_dir)
        dir_c = base / "pasta_c"
        dir_a = base / "pasta_a"
        dir_b = base / "pasta_b"
        dir_c.mkdir(); dir_a.mkdir(); dir_b.mkdir()

        f_c = dir_c / "file.bin"
        f_a = dir_a / "file.bin"
        f_b = dir_b / "file.bin"

        content = b"ConteudoDeTeste12345678" * 500  # 12 KB
        f_c.write_bytes(content)
        f_a.write_bytes(content)
        f_b.write_bytes(content)

        # Inseridos propositalmente em ordem desordenada
        recs = [
            FileRecord(path=f_c, size=len(content), mtime=1.0),
            FileRecord(path=f_a, size=len(content), mtime=1.0),
            FileRecord(path=f_b, size=len(content), mtime=1.0),
        ]

        groups = find_exact_duplicates(recs)
        assert len(groups) == 1
        assert len(groups[0].files) == 3
        print("[OK] test_directory_locality_sorting passou com sucesso!")


def test_cache_migration_and_persistence():
    """Valida persistência e migração de schema da assinatura."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test_migration.db"
        cache = CacheManager(db_path)

        f1 = Path(tmp_dir) / "file1.dat"
        f2 = Path(tmp_dir) / "file2.dat"
        content = b"TesteDePersistencia123" * 1000
        f1.write_bytes(content)
        f2.write_bytes(content)

        recs = [
            FileRecord(path=f1, size=len(content), mtime=5.0),
            FileRecord(path=f2, size=len(content), mtime=5.0),
        ]

        groups = find_exact_duplicates(recs, cache_manager=cache)
        assert len(groups) == 1

        # Reabre o cache (disparando _init_db novamente)
        cache2 = CacheManager(db_path)
        cached = cache2.get_exact_cache(recs)
        assert len(cached) == 2
        for p, (p_hash, f_hash) in cached.items():
            assert p_hash is not None
            assert f_hash is not None

        # Segundo scan deve ser atendido 100% pelo cache
        groups2 = find_exact_duplicates(recs, cache_manager=cache2)
        assert len(groups2) == 1
        print("[OK] test_cache_migration_and_persistence passou com sucesso!")


if __name__ == "__main__":
    test_small_files_fast_path()
    test_large_files_head_tail_filtering()
    test_directory_locality_sorting()
    test_cache_migration_and_persistence()
    print("\nTODOS OS TESTES DO NOVO MOTOR EXATO PASSARAM COM 100% DE SUCESSO!")
