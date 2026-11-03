"""
Deduplicador Exato (Bit-a-bit) de Alta Performance com xxHash.
Algoritmo de 3 estágios:
1. Agrupamento por tamanho (tamanhos únicos são descartados sem tocar no disco).
2. Hash rápido dos primeiros 4 KB (descarta arquivos do mesmo tamanho com conteúdo diferente).
3. Hash completo de 64 bits em streaming para confirmação definitiva.
"""

from collections import defaultdict
from typing import List, Callable, Optional, Tuple
import xxhash
from dupeclean.core.models import FileRecord, DuplicateGroup, ScanMode
from dupeclean.core.cache import CacheManager


def find_exact_duplicates(
    records: List[FileRecord],
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    cache_manager: Optional[CacheManager] = None
) -> List[DuplicateGroup]:
    """
    Executa a identificação de duplicatas exatas usando xxHash64 com cache SQLite persistente.
    """
    cache = cache_manager or CacheManager()

    # Estágio 1: Agrupar por tamanho
    by_size = defaultdict(list)
    for rec in records:
        by_size[rec.size].append(rec)

    # Filtrar apenas tamanhos com mais de 1 arquivo
    candidates_by_size = [files for size, files in by_size.items() if len(files) > 1]
    total_candidates = sum(len(f) for f in candidates_by_size)

    if total_candidates == 0 or (stop_check and stop_check()):
        return []

    # Consultar cache para todos os candidatos
    flat_candidates = [f for grp in candidates_by_size for f in grp]
    cached_exact = cache.get_exact_cache(flat_candidates)
    cache_hits_prefix = sum(1 for f in flat_candidates if str(f.path) in cached_exact and cached_exact[str(f.path)][0])

    if progress_callback and cache_hits_prefix > 0:
        progress_callback(f"Loading cached headers ({cache_hits_prefix}/{total_candidates})...", cache_hits_prefix, total_candidates)

    # Stage 2: Header hash (first 4 KB)
    by_header_hash = defaultdict(list)
    processed = 0
    new_prefix_entries: List[Tuple[str, int, float, Optional[str], Optional[str]]] = []

    for file_group in candidates_by_size:
        for rec in file_group:
            if stop_check and stop_check():
                if new_prefix_entries:
                    cache.save_exact_batch(new_prefix_entries)
                return []

            processed += 1
            if progress_callback and processed % 200 == 0:
                progress_callback("Comparing headers...", processed, total_candidates)

            p_key = str(rec.path)
            cached_data = cached_exact.get(p_key)
            if cached_data and cached_data[0]:
                by_header_hash[(rec.size, cached_data[0])].append(rec)
                continue

            try:
                with open(rec.path, "rb") as f:
                    chunk = f.read(4096)
                    h = xxhash.xxh64(chunk).hexdigest()
                    by_header_hash[(rec.size, h)].append(rec)
                    new_prefix_entries.append((p_key, rec.size, rec.mtime, h, None))
            except (OSError, PermissionError):
                continue

            if len(new_prefix_entries) >= 1000:
                cache.save_exact_batch(new_prefix_entries)
                new_prefix_entries = []

    if new_prefix_entries:
        cache.save_exact_batch(new_prefix_entries)
        new_prefix_entries = []

    # Filtrar candidatos que compartilham tamanho e cabeçalho
    candidates_by_header = [files for key, files in by_header_hash.items() if len(files) > 1]
    total_deep_check = sum(len(f) for f in candidates_by_header)

    # Estágio 3: Hash completo do arquivo
    by_full_hash = defaultdict(list)
    deep_processed = 0
    new_full_entries: List[Tuple[str, int, float, Optional[str], Optional[str]]] = []

    for file_group in candidates_by_header:
        for rec in file_group:
            if stop_check and stop_check():
                if new_full_entries:
                    cache.save_exact_batch(new_full_entries)
                return []

            deep_processed += 1
            if progress_callback and deep_processed % 50 == 0:
                progress_callback("Computing full hashes...", deep_processed, total_deep_check)

            p_key = str(rec.path)
            cached_data = cached_exact.get(p_key)
            if cached_data and cached_data[1]:
                rec.hash_value = cached_data[1]
                by_full_hash[(rec.size, rec.hash_value)].append(rec)
                continue

            try:
                hasher = xxhash.xxh64()
                with open(rec.path, "rb") as f:
                    while True:
                        buf = f.read(1024 * 1024)  # 1 MB por bloco
                        if not buf:
                            break
                        hasher.update(buf)
                rec.hash_value = hasher.hexdigest()
                by_full_hash[(rec.size, rec.hash_value)].append(rec)
                new_full_entries.append((p_key, rec.size, rec.mtime, None, rec.hash_value))
            except (OSError, PermissionError):
                continue

            if len(new_full_entries) >= 500:
                cache.save_exact_batch(new_full_entries)
                new_full_entries = []

    if new_full_entries:
        cache.save_exact_batch(new_full_entries)
        new_full_entries = []

    # Montar grupos de duplicatas definitivos
    duplicate_groups: List[DuplicateGroup] = []
    group_id = 1

    for (size, h_val), dup_files in by_full_hash.items():
        if len(dup_files) > 1:
            duplicate_groups.append(DuplicateGroup(
                group_id=group_id,
                files=dup_files,
                similarity=1.0,
                kind=ScanMode.EXACT
            ))
            group_id += 1

    return duplicate_groups
