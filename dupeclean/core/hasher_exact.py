"""
Deduplicador Exato (Bit-a-bit) de Altíssimo Desempenho com xxHash64 e I/O Paralelo.
Algoritmo de 3 estágios otimizado para saturação de link Gigabit (100 MB/s+) e cancelamento instantâneo:
1. Agrupamento por tamanho (tamanhos únicos são descartados em memória sem tocar no disco).
2. Hash paralelo dos primeiros 4 KB com descarte imediato via ThreadPool e stop_check.
3. Hash completo em streaming paralelo com buffers de 4 MB e cancelamento atômico em tempo real.
"""

import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import List, Callable, Optional, Tuple, Dict
import xxhash
from dupeclean.core.models import FileRecord, DuplicateGroup, ScanMode
from dupeclean.core.cache import CacheManager

# Buffer de leitura streaming otimizado para alto throughput em rede (SMB/CIFS/iSCSI)
STREAM_BUFFER_SIZE = 4 * 1024 * 1024  # 4 MB por bloco de leitura


def _read_header_hash(args: Tuple[FileRecord, Optional[Callable[[], bool]]]) -> Optional[Tuple[FileRecord, str]]:
    """Lê os primeiros 4 KB de um arquivo e calcula o hash do cabeçalho com suporte a cancelamento."""
    rec, stop_check = args
    if stop_check and stop_check():
        return None
    try:
        with open(rec.path, "rb") as f:
            chunk = f.read(4096)
            if stop_check and stop_check():
                return None
            h = xxhash.xxh64(chunk).hexdigest()
            return rec, h
    except (OSError, PermissionError):
        return None


def _compute_full_hash(args: Tuple[FileRecord, Optional[Callable[[], bool]]]) -> Optional[Tuple[FileRecord, str]]:
    """Calcula o hash xxHash64 completo do arquivo em streaming com interrupção instantânea."""
    rec, stop_check = args
    if stop_check and stop_check():
        return None
    try:
        hasher = xxhash.xxh64()
        with open(rec.path, "rb", buffering=STREAM_BUFFER_SIZE) as f:
            while True:
                if stop_check and stop_check():
                    return None
                buf = f.read(STREAM_BUFFER_SIZE)
                if not buf:
                    break
                hasher.update(buf)
        if stop_check and stop_check():
            return None
        h = hasher.hexdigest()
        rec.hash_value = h
        return rec, h
    except (OSError, PermissionError):
        return None


def find_exact_duplicates(
    records: List[FileRecord],
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    cache_manager: Optional[CacheManager] = None
) -> List[DuplicateGroup]:
    """
    Executa a identificação de duplicatas exatas usando xxHash64 multithreaded
    com leitura concorrente de alta vazão e resposta instantânea a cancelamentos.
    """
    cache = cache_manager or CacheManager()

    # -------------------------------------------------------------------------
    # Estágio 1: Agrupar por tamanho (descarte instantâneo sem I/O)
    # -------------------------------------------------------------------------
    by_size = defaultdict(list)
    for rec in records:
        by_size[rec.size].append(rec)

    # Filtrar apenas tamanhos que possuem 2 ou mais arquivos
    candidates_by_size = [files for size, files in by_size.items() if len(files) > 1]
    total_candidates = sum(len(f) for f in candidates_by_size)

    if total_candidates == 0 or (stop_check and stop_check()):
        return []

    # -------------------------------------------------------------------------
    # Estágio 2: Hash do cabeçalho (primeiros 4 KB) com ThreadPool concorrente
    # -------------------------------------------------------------------------
    flat_candidates = [f for grp in candidates_by_size for f in grp]
    cached_exact = cache.get_exact_cache(flat_candidates)
    cache_hits_prefix = sum(1 for f in flat_candidates if str(f.path) in cached_exact and cached_exact[str(f.path)][0])

    if progress_callback and cache_hits_prefix > 0:
        progress_callback(f"Loading cached headers ({cache_hits_prefix}/{total_candidates})...", cache_hits_prefix, total_candidates)

    by_header_hash = defaultdict(list)
    to_header_check: List[FileRecord] = []

    # Separa candidatos já presentes no cache dos que requerem leitura em disco
    for rec in flat_candidates:
        p_key = str(rec.path)
        cached_data = cached_exact.get(p_key)
        if cached_data and cached_data[0]:
            by_header_hash[(rec.size, cached_data[0])].append(rec)
        else:
            to_header_check.append(rec)

    if to_header_check and not (stop_check and stop_check()):
        header_workers = min(32, max(4, os.cpu_count() or 4))
        new_prefix_entries: List[Tuple[str, int, float, Optional[str], Optional[str]]] = []
        header_processed = cache_hits_prefix

        # Chunks menores para conferência frequente de stop_check
        header_chunk_size = 256
        executor = ThreadPoolExecutor(max_workers=header_workers)
        try:
            for chunk_start in range(0, len(to_header_check), header_chunk_size):
                if stop_check and stop_check():
                    break

                chunk = to_header_check[chunk_start : chunk_start + header_chunk_size]
                task_args = [(r, stop_check) for r in chunk]
                results = list(executor.map(_read_header_hash, task_args))

                if stop_check and stop_check():
                    break

                for res in results:
                    header_processed += 1
                    if res is not None:
                        rec, h = res
                        by_header_hash[(rec.size, h)].append(rec)
                        new_prefix_entries.append((str(rec.path), rec.size, rec.mtime, h, None))

                if progress_callback:
                    progress_callback(
                        f"Comparing headers ({header_processed}/{total_candidates})...",
                        header_processed,
                        total_candidates
                    )

                if new_prefix_entries:
                    cache.save_exact_batch(new_prefix_entries)
                    new_prefix_entries = []
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if new_prefix_entries:
            cache.save_exact_batch(new_prefix_entries)
            new_prefix_entries = []

    if stop_check and stop_check():
        return []

    # -------------------------------------------------------------------------
    # Estágio 3: Hash completo em streaming paralelo com buffer largo (4 MB)
    # -------------------------------------------------------------------------
    candidates_by_header = [files for key, files in by_header_hash.items() if len(files) > 1]
    total_deep_check = sum(len(f) for f in candidates_by_header)

    if total_deep_check == 0 or (stop_check and stop_check()):
        return []

    flat_deep_candidates = [f for grp in candidates_by_header for f in grp]
    by_full_hash = defaultdict(list)
    to_deep_check: List[FileRecord] = []
    deep_cached_count = 0

    for rec in flat_deep_candidates:
        p_key = str(rec.path)
        cached_data = cached_exact.get(p_key)
        if cached_data and cached_data[1]:
            rec.hash_value = cached_data[1]
            by_full_hash[(rec.size, rec.hash_value)].append(rec)
            deep_cached_count += 1
        else:
            to_deep_check.append(rec)

    if to_deep_check and not (stop_check and stop_check()):
        deep_workers = min(16, max(4, os.cpu_count() or 4))
        new_full_entries: List[Tuple[str, int, float, Optional[str], Optional[str]]] = []
        deep_processed = deep_cached_count

        # Lotes de 64 arquivos para resposta de parada em fração de segundo
        deep_chunk_size = 64
        executor = ThreadPoolExecutor(max_workers=deep_workers)
        try:
            for chunk_start in range(0, len(to_deep_check), deep_chunk_size):
                if stop_check and stop_check():
                    break

                chunk = to_deep_check[chunk_start : chunk_start + deep_chunk_size]
                task_args = [(r, stop_check) for r in chunk]
                results = list(executor.map(_compute_full_hash, task_args))

                if stop_check and stop_check():
                    break

                for res in results:
                    deep_processed += 1
                    if res is not None:
                        rec, h = res
                        by_full_hash[(rec.size, h)].append(rec)
                        new_full_entries.append((str(rec.path), rec.size, rec.mtime, None, h))

                if progress_callback:
                    progress_callback(
                        f"Computing full hashes ({deep_processed}/{total_deep_check})...",
                        deep_processed,
                        total_deep_check
                    )

                if new_full_entries:
                    cache.save_exact_batch(new_full_entries)
                    new_full_entries = []
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if new_full_entries:
            cache.save_exact_batch(new_full_entries)
            new_full_entries = []

    if stop_check and stop_check():
        return []

    # -------------------------------------------------------------------------
    # Montagem dos Grupos de Duplicatas Definitivos
    # -------------------------------------------------------------------------
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
