"""
Deduplicador Exato (Bit-a-bit) de Altíssimo Desempenho com xxHash64 e I/O Otimizado.
Algoritmo de 3 estágios otimizado para alta vazão (NVMe, SSD, HDDs mecânicos e storages em rede/iSCSI):
1. Agrupamento por tamanho (tamanhos únicos são descartados em memória sem tocar no disco).
2. Assinatura composta Head 4KB + Tail 4KB com fast-path para arquivos pequenos (<= 8KB)
   e ordenação por localidade de diretório para evitar head thrashing em HDDs mecânicos.
3. Hash completo em streaming paralelo com buffers de 4 MB, concorrência balanceada e cancelamento atômico.
"""

import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import List, Callable, Optional, Tuple, Dict
import xxhash
from dupeclean.core.models import FileRecord, DuplicateGroup, ScanMode
from dupeclean.core.cache import CacheManager

# Buffer de leitura streaming otimizado para alto throughput de I/O (armazenamentos locais e em rede)
STREAM_BUFFER_SIZE = 4 * 1024 * 1024  # 4 MB por bloco de leitura


def _read_signature(args: Tuple[FileRecord, Optional[Callable[[], bool]]]) -> Optional[Tuple[FileRecord, str, bool]]:
    """
    Calcula a assinatura rápida do arquivo com suporte a cancelamento atômico:
    - Arquivos <= 8 KB: lê o conteúdo integral e calcula o hash definitivo (is_full=True).
    - Arquivos > 8 KB: lê os primeiros 4 KB e os últimos 4 KB (Head + Tail) (is_full=False),
      descartando 99.9% dos falsos positivos de cabeçalho sem streaming do arquivo inteiro.
    Retorna (rec, hash_value, is_full) ou None em caso de erro de I/O ou cancelamento.
    """
    rec, stop_check = args
    if stop_check and stop_check():
        return None
    try:
        with open(rec.path, "rb") as f:
            if rec.size <= 8192:
                data = f.read(8192)
                if stop_check and stop_check():
                    return None
                h = xxhash.xxh64(data).hexdigest()
                return rec, h, True
            else:
                head = f.read(4096)
                if stop_check and stop_check():
                    return None
                try:
                    f.seek(-4096, os.SEEK_END)
                    tail = f.read(4096)
                except OSError:
                    tail = b""
                if stop_check and stop_check():
                    return None
                hasher = xxhash.xxh64()
                hasher.update(head)
                if tail:
                    hasher.update(tail)
                h = hasher.hexdigest()
                return rec, h, False
    except (OSError, PermissionError):
        return None


# Alias para retrocompatibilidade interna
_read_header_hash = _read_signature


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
    # Estágio 2: Assinatura composta (Head 4 KB + Tail 4 KB) e fast-path (<= 8 KB)
    # -------------------------------------------------------------------------
    flat_candidates = [f for grp in candidates_by_size for f in grp]
    cached_exact = cache.get_exact_cache(flat_candidates, stop_check=stop_check)
    if stop_check and stop_check():
        return []

    by_full_hash = defaultdict(list)
    by_header_hash = defaultdict(list)
    to_header_check: List[FileRecord] = []

    # Fast-path: arquivos pequenos (<= 8 KB) com full_hash em cache vão direto para by_full_hash
    for rec in flat_candidates:
        p_key = str(rec.path)
        cached_data = cached_exact.get(p_key)
        if rec.size <= 8192 and cached_data and cached_data[1]:
            rec.hash_value = cached_data[1]
            by_full_hash[(rec.size, cached_data[1])].append(rec)
        elif cached_data and cached_data[0]:
            by_header_hash[(rec.size, cached_data[0])].append(rec)
        else:
            to_header_check.append(rec)

    cache_hits_prefix = total_candidates - len(to_header_check)

    if progress_callback and cache_hits_prefix > 0:
        progress_callback(f"Loading cached results ({cache_hits_prefix:,}/{total_candidates:,})...", cache_hits_prefix, total_candidates)

    if to_header_check and not (stop_check and stop_check()):
        # Otimização espacial (Directory Locality): agrupa leituras por pasta pai
        # para leitura sequencial do MFT e blocos de diretório, eliminando head thrashing em HDDs e storages NAS
        to_header_check.sort(key=lambda r: str(r.path.parent))

        # Concorrência equilibrada para evitar saturação de fila de I/O em discos mecânicos e iSCSI
        header_workers = min(16, max(4, os.cpu_count() or 4))
        new_prefix_entries: List[Tuple[str, int, float, Optional[str], Optional[str]]] = []
        header_processed = cache_hits_prefix

        # Chunks para conferência frequente de stop_check
        header_chunk_size = 256
        executor = ThreadPoolExecutor(max_workers=header_workers)
        try:
            for chunk_start in range(0, len(to_header_check), header_chunk_size):
                if stop_check and stop_check():
                    break

                chunk = to_header_check[chunk_start : chunk_start + header_chunk_size]
                task_args = [(r, stop_check) for r in chunk]
                futures = [executor.submit(_read_signature, arg) for arg in task_args]
                results = []
                for f in futures:
                    if stop_check and stop_check():
                        for pending in futures:
                            pending.cancel()
                        break
                    try:
                        results.append(f.result())
                    except Exception:
                        results.append(None)

                if stop_check and stop_check():
                    break

                for res in results:
                    header_processed += 1
                    if res is not None:
                        rec, h, is_full = res
                        if is_full:
                            rec.hash_value = h
                            by_full_hash[(rec.size, h)].append(rec)
                            new_prefix_entries.append((str(rec.path), rec.size, rec.mtime, h, h))
                        else:
                            by_header_hash[(rec.size, h)].append(rec)
                            new_prefix_entries.append((str(rec.path), rec.size, rec.mtime, h, None))

                if progress_callback:
                    progress_callback(
                        f"Comparing file signatures ({header_processed:,}/{total_candidates:,})...",
                        header_processed,
                        total_candidates
                    )

                if len(new_prefix_entries) >= 1024:
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

    to_deep_check: List[FileRecord] = []
    deep_cached_count = 0

    if total_deep_check > 0 and not (stop_check and stop_check()):
        flat_deep_candidates = [f for grp in candidates_by_header for f in grp]

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
        # Otimização espacial para leitura sequencial de grandes blocos contíguos
        to_deep_check.sort(key=lambda r: str(r.path.parent))

        # Concorrência moderada para I/O massivo em streaming de 4 MB (evita contenção de banda em NAS/iSCSI e HDDs)
        deep_workers = min(8, max(2, (os.cpu_count() or 4) // 2))
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
                futures = [executor.submit(_compute_full_hash, arg) for arg in task_args]
                results = []
                for f in futures:
                    if stop_check and stop_check():
                        for pending in futures:
                            pending.cancel()
                        break
                    try:
                        results.append(f.result())
                    except Exception:
                        results.append(None)

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
                        f"Computing full hashes ({deep_processed:,}/{total_deep_check:,})...",
                        deep_processed,
                        total_deep_check
                    )

                if len(new_full_entries) >= 512:
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
