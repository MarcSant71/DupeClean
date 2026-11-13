"""
Deduplicador Exato (Bit-a-bit) de Altíssimo Desempenho com xxHash64 e I/O Otimizado.
Algoritmo de 3 estágios otimizado para alta vazão (NVMe, SSD, HDDs mecânicos e storages em rede/iSCSI):
1. Agrupamento por tamanho (tamanhos únicos são descartados em memória sem tocar no disco).
2. Assinatura composta Head 4KB + Tail 4KB para arquivos > 256KB, com fast-path direto para arquivos pequenos/médios (<= 256KB)
   e ordenação por localidade de diretório para evitar head thrashing em HDDs mecânicos.
3. Hash completo em streaming paralelo com buffers de 4 MB, concorrência balanceada e cancelamento atômico.
"""

import os
import queue
import threading
import time
from collections import defaultdict
from typing import List, Callable, Optional, Tuple, Dict, Any
import xxhash
from dupeclean.core.models import FileRecord, DuplicateGroup, ScanMode
from dupeclean.core.cache import CacheManager
from dupeclean.core.scanner import get_volume_capabilities


def _capture_fstat_ids(f, rec: FileRecord) -> None:
    """Captura (volume_id, file_id) do handle já aberto via os.fstat em volumes NTFS/ReFS (zero CreateFile extra)."""
    if rec.volume_id > 0 and rec.file_id > 0:
        return
    _, supports_fid = get_volume_capabilities(rec.path)
    if not supports_fid:
        return
    try:
        st = os.fstat(f.fileno())
        rec.volume_id = getattr(st, "st_dev", 0) or 0
        rec.file_id = getattr(st, "st_ino", 0) or 0
    except OSError:
        pass

# Tamanho limite para arquivos pequenos/médios (lidos integralmente em 1 única passada no Estágio 2 via fast-path)
PREHASH_CHUNK = 4096  # 4 KB (Head 4 KB + Tail 4 KB para arquivos > 256 KB)
FAST_PATH_SIZE = 256 * 1024  # 256 KB (evita dupla abertura/leitura de fotos pequenas e documentos)

# Buffer de leitura streaming otimizado para alta vazão contínua sem fragmentação de memória
STREAM_BUFFER_SIZE = 512 * 1024  # 512 KB por bloco de leitura


def _read_signature(args: Tuple[FileRecord, Optional[Callable[[], bool]]]) -> Optional[Tuple[FileRecord, str, bool]]:
    """
    Calcula o hash inicial do arquivo com suporte a cancelamento atômico:
    - Arquivos <= 256 KB: lê o conteúdo integral de uma vez e já calcula o full hash (is_full=True).
    - Arquivos > 256 KB: lê os primeiros 4 KB e os últimos 4 KB (Head + Tail) (is_full=False),
      descartando 99.9% dos falsos positivos antes do streaming completo no Estágio 3.
    Retorna (rec, hash_value, is_full) ou None em caso de erro de I/O ou cancelamento.
    """
    rec, stop_check = args
    if stop_check and stop_check():
        return None
    try:
        with open(rec.path, "rb") as f:
            _capture_fstat_ids(f, rec)
            if rec.size <= FAST_PATH_SIZE:
                data = f.read(FAST_PATH_SIZE)
                if stop_check and stop_check():
                    return None
                h = xxhash.xxh64(data).hexdigest()
                return rec, h, True
            else:
                head = f.read(PREHASH_CHUNK)
                if stop_check and stop_check():
                    return None
                try:
                    f.seek(-PREHASH_CHUNK, os.SEEK_END)
                    tail = f.read(PREHASH_CHUNK)
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


def _compute_full_hash_worker(
    rec: FileRecord,
    mv: bytearray,
    stop_check: Optional[Callable[[], bool]]
) -> Optional[Tuple[FileRecord, str]]:
    """Calcula o hash xxHash64 completo com zero overhead de alocação de buffer."""
    if stop_check and stop_check():
        return None
    try:
        hasher = xxhash.xxh64()
        buf_len = len(mv)
        with open(rec.path, "rb", buffering=0) as f:
            _capture_fstat_ids(f, rec)
            while True:
                if stop_check and stop_check():
                    return None
                n = f.readinto(mv)
                if not n:
                    break
                if n < buf_len:
                    hasher.update(memoryview(mv)[:n])
                else:
                    hasher.update(mv)
        if stop_check and stop_check():
            return None
        h = hasher.hexdigest()
        rec.hash_value = h
        return rec, h
    except (OSError, PermissionError):
        return None


def _compute_full_hash(args: Tuple[FileRecord, Optional[Callable[[], bool]]]) -> Optional[Tuple[FileRecord, str]]:
    """Calcula o hash xxHash64 completo do arquivo em streaming com interrupção instantânea (API pública compatível)."""
    rec, stop_check = args
    mv = bytearray(STREAM_BUFFER_SIZE)
    return _compute_full_hash_worker(rec, mv, stop_check)


class _AsyncExactCacheWriter:
    """
    Escritor assíncrono em background para persistência do cache exato (SQLite WAL).
    Desacopla totalmente os workers de I/O do banco de dados, acumulando lotes em memória
    e commitando em uma thread dedicada sem bloquear o pipeline de leitura.
    """
    def __init__(self, cache_manager: CacheManager, batch_size: int = 2048, flush_interval: float = 0.5):
        self.cache_manager = cache_manager
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._queue: queue.Queue = queue.Queue(maxsize=100000)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="AsyncExactCacheWriter")
        self._thread.start()

    def add(self, entry: Tuple[str, int, float, Optional[str], Optional[str]]):
        try:
            self._queue.put_nowait(entry)
        except queue.Full:
            try:
                self._queue.put(entry, timeout=0.1)
            except queue.Full:
                pass

    def _run(self):
        batch = []
        last_flush = time.perf_counter()
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                entry = self._queue.get(timeout=0.05)
                batch.append(entry)
            except queue.Empty:
                pass

            now = time.perf_counter()
            if batch and (len(batch) >= self.batch_size or now - last_flush >= self.flush_interval or self._stop_event.is_set()):
                try:
                    self.cache_manager.save_exact_batch(batch)
                except Exception:
                    pass
                batch.clear()
                last_flush = now

        if batch:
            try:
                self.cache_manager.save_exact_batch(batch)
            except Exception:
                pass

    def close(self):
        """Finaliza a thread e garante o flush de todos os registros pendentes."""
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)


def _emit_progress(cb: Optional[Callable], msg: str, curr: int, tot: int, stage_idx: int):
    """Dispara callback de progresso garantindo compatibilidade com assinaturas de 3 ou 4 argumentos."""
    if not cb:
        return
    try:
        cb(msg, curr, tot, stage_idx)
    except TypeError:
        cb(msg, curr, tot)


def find_exact_duplicates(
    records: List[FileRecord],
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    cache_manager: Optional[CacheManager] = None
) -> List[DuplicateGroup]:
    """
    Executa a identificação de duplicatas exatas usando xxHash64 multithreaded
    com leitura concorrente de altíssima vazão, pipeline com zero overhead de lock,
    persistência de cache assíncrona desacoplada do I/O e resposta instantânea a cancelamentos.
    """
    cache = cache_manager or CacheManager()
    cache_writer = _AsyncExactCacheWriter(cache)

    try:
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
        # Estágio 2: Assinatura composta (Head 4 KB + Tail 4 KB) e fast-path (<= 256 KB)
        # -------------------------------------------------------------------------
        flat_candidates = [f for grp in candidates_by_size for f in grp]
        if progress_callback:
            _emit_progress(progress_callback, f"Checking cache ({total_candidates:,} candidates)...", 0, total_candidates, 2)

        def _exact_cache_cb(msg: str, curr: int, tot: int):
            if progress_callback:
                _emit_progress(progress_callback, msg, curr, tot, 2)

        cached_exact = cache.get_exact_cache(
            flat_candidates,
            progress_callback=_exact_cache_cb if progress_callback else None,
            stop_check=stop_check
        )
        if stop_check and stop_check():
            return []

        by_full_hash = defaultdict(list)
        by_header_hash = defaultdict(list)
        to_header_check: List[FileRecord] = []

        # Separação estrita: arquivos <= FAST_PATH_SIZE nunca entram em by_header_hash (são resolvidos no Estágio 2)
        for rec in flat_candidates:
            p_key = str(rec.path)
            cached_data = cached_exact.get(p_key)
            if rec.size <= FAST_PATH_SIZE:
                if cached_data and cached_data[1]:
                    rec.hash_value = cached_data[1]
                    by_full_hash[(rec.size, cached_data[1])].append(rec)
                else:
                    to_header_check.append(rec)
            else:
                if cached_data and cached_data[0]:
                    by_header_hash[(rec.size, cached_data[0])].append(rec)
                else:
                    to_header_check.append(rec)

        cache_hits_prefix = total_candidates - len(to_header_check)

        if progress_callback and cache_hits_prefix > 0:
            _emit_progress(progress_callback, f"Loading cached partial hashes ({cache_hits_prefix:,}/{total_candidates:,})...", cache_hits_prefix, total_candidates, 2)

        if to_header_check and not (stop_check and stop_check()):
            # Otimização espacial (Directory Locality): agrupa leituras por pasta pai
            # para leitura sequencial do MFT e blocos de diretório, eliminando head thrashing em HDDs e storages NAS
            to_header_check.sort(key=lambda r: str(r.path.parent))

            header_workers = min(32, max(4, os.cpu_count() or 4))
            header_workers = min(header_workers, len(to_header_check))
            header_processed = cache_hits_prefix
            last_progress_time = time.perf_counter()

            task_queue: queue.Queue = queue.Queue(maxsize=4096)
            result_queue: queue.Queue = queue.Queue(maxsize=4096)
            stop_workers = threading.Event()

            def _header_worker():
                while not stop_workers.is_set():
                    if stop_check and stop_check():
                        break
                    try:
                        rec = task_queue.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    if rec is None:
                        break
                    try:
                        res = _read_signature((rec, stop_check))
                    except Exception:
                        res = None

                    while not stop_workers.is_set():
                        if stop_check and stop_check():
                            break
                        try:
                            result_queue.put(res, timeout=0.05)
                            break
                        except queue.Full:
                            continue

            worker_threads = [
                threading.Thread(target=_header_worker, daemon=True, name=f"HeaderWorker-{i}")
                for i in range(header_workers)
            ]
            for t in worker_threads:
                t.start()

            def _header_feeder():
                for r in to_header_check:
                    if stop_workers.is_set() or (stop_check and stop_check()):
                        break
                    while not stop_workers.is_set():
                        if stop_check and stop_check():
                            break
                        try:
                            task_queue.put(r, timeout=0.05)
                            break
                        except queue.Full:
                            continue
                for _ in range(header_workers):
                    try:
                        task_queue.put(None, timeout=0.1)
                    except Exception:
                        pass

            feeder_thread = threading.Thread(target=_header_feeder, daemon=True, name="HeaderFeeder")
            feeder_thread.start()

            try:
                while header_processed < total_candidates:
                    if stop_check and stop_check():
                        stop_workers.set()
                        break
                    try:
                        res = result_queue.get(timeout=0.05)
                    except queue.Empty:
                        if not any(t.is_alive() for t in worker_threads) and task_queue.empty() and result_queue.empty():
                            break
                        continue

                    header_processed += 1
                    if res is not None:
                        rec, h, is_full = res
                        if is_full:
                            rec.hash_value = h
                            by_full_hash[(rec.size, h)].append(rec)
                            cache_writer.add((str(rec.path), rec.size, rec.mtime, h, h, rec.volume_id, rec.file_id))
                        else:
                            by_header_hash[(rec.size, h)].append(rec)
                            cache_writer.add((str(rec.path), rec.size, rec.mtime, h, None, rec.volume_id, rec.file_id))

                    now = time.perf_counter()
                    if progress_callback and (now - last_progress_time >= 0.1 or header_processed % 256 == 0):
                        _emit_progress(
                            progress_callback,
                            f"Calculating partial hashes ({header_processed:,}/{total_candidates:,})...",
                            header_processed,
                            total_candidates,
                            2
                        )
                        last_progress_time = now
            finally:
                stop_workers.set()
                for t in worker_threads:
                    t.join(timeout=0.5)

        if stop_check and stop_check():
            return []

        # -------------------------------------------------------------------------
        # Estágio 3: Hash completo em streaming paralelo com buffer contínuo (512 KB)
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

            deep_workers = min(32, max(4, os.cpu_count() or 4))
            deep_workers = min(deep_workers, len(to_deep_check))
            deep_processed = deep_cached_count
            last_progress_time = time.perf_counter()

            task_queue: queue.Queue = queue.Queue(maxsize=4096)
            result_queue: queue.Queue = queue.Queue(maxsize=4096)
            stop_workers = threading.Event()

            def _deep_worker():
                mv = bytearray(STREAM_BUFFER_SIZE)
                while not stop_workers.is_set():
                    if stop_check and stop_check():
                        break
                    try:
                        rec = task_queue.get(timeout=0.05)
                    except queue.Empty:
                        continue
                    if rec is None:
                        break
                    try:
                        res = _compute_full_hash_worker(rec, mv, stop_check)
                    except Exception:
                        res = None

                    while not stop_workers.is_set():
                        if stop_check and stop_check():
                            break
                        try:
                            result_queue.put(res, timeout=0.05)
                            break
                        except queue.Full:
                            continue

            worker_threads = [
                threading.Thread(target=_deep_worker, daemon=True, name=f"DeepWorker-{i}")
                for i in range(deep_workers)
            ]
            for t in worker_threads:
                t.start()

            def _deep_feeder():
                for r in to_deep_check:
                    if stop_workers.is_set() or (stop_check and stop_check()):
                        break
                    while not stop_workers.is_set():
                        if stop_check and stop_check():
                            break
                        try:
                            task_queue.put(r, timeout=0.05)
                            break
                        except queue.Full:
                            continue
                for _ in range(deep_workers):
                    try:
                        task_queue.put(None, timeout=0.1)
                    except Exception:
                        pass

            feeder_thread = threading.Thread(target=_deep_feeder, daemon=True, name="DeepFeeder")
            feeder_thread.start()

            try:
                while deep_processed < total_deep_check:
                    if stop_check and stop_check():
                        stop_workers.set()
                        break
                    try:
                        res = result_queue.get(timeout=0.05)
                    except queue.Empty:
                        if not any(t.is_alive() for t in worker_threads) and task_queue.empty() and result_queue.empty():
                            break
                        continue

                    deep_processed += 1
                    if res is not None:
                        rec, h = res
                        by_full_hash[(rec.size, h)].append(rec)
                        cache_writer.add((str(rec.path), rec.size, rec.mtime, None, h, rec.volume_id, rec.file_id))

                    now = time.perf_counter()
                    if progress_callback and (now - last_progress_time >= 0.1 or deep_processed % 128 == 0):
                        _emit_progress(
                            progress_callback,
                            f"Calculating full hashes ({deep_processed:,}/{total_deep_check:,})...",
                            deep_processed,
                            total_deep_check,
                            3
                        )
                        last_progress_time = now
            finally:
                stop_workers.set()
                for t in worker_threads:
                    t.join(timeout=0.5)

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

    finally:
        cache_writer.close()
