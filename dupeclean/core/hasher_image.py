"""
Extrator de Assinaturas e Hashes Visuais de Imagens via GPU (RTX 5090 Tensor Cores).
Usa DCT 2D em lote com tensores FP16 para calcular embeddings visuais e pHash em alta velocidade.
"""

from typing import List, Callable, Optional, Tuple
import io
import os
import math
import time
import queue
import threading
from PIL import Image, ImageOps
import torch
import numpy as np

from dupeclean.core.models import FileRecord, DuplicateGroup, ScanMode
from dupeclean.core.hardware import get_compute_device
from dupeclean.core.scanner import get_volume_capabilities
from dupeclean.core.cache import (
    CacheManager,
    compute_prefix_hash_from_bytes,
    compute_prefix_hash_from_stream,
)

# High-discrimination visual signature extraction parameters
IMG_SIZE = 64
LOW_FREQ_SIZE = 16
CHANNELS = 3
ALPHA = 1.2  # Calibrated spectral whitening: attenuates static background without amplifying compression noise
DIM_SIZE = CHANNELS * LOW_FREQ_SIZE * LOW_FREQ_SIZE  # 768 dimensions (1,536 bytes in FP16)
_SINGLE_READ_IMAGE_THRESHOLD = 16 * 1024 * 1024  # 16 MB


def _to_opaque_rgb(img: Image.Image) -> Image.Image:
    """Converte qualquer modo (alpha, paleta transparente ou 16-bit) para RGB em fundo neutro."""
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (128, 128, 128))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    if img.mode.startswith("I;16"):
        arr16 = np.asarray(img, dtype=np.uint16)
        return Image.fromarray((arr16 >> 8).astype(np.uint8), mode="L").convert("RGB")
    return img if img.mode == "RGB" else img.convert("RGB")


def _decode_image_from_stream(stream) -> np.ndarray:
    """Decode with EXIF orientation fix, alpha compositing, and anti-aliased downsampling."""
    with Image.open(stream) as img:
        img.draft("RGB", (IMG_SIZE * 2, IMG_SIZE * 2))
        try:
            ImageOps.exif_transpose(img, in_place=True)
        except Exception:
            pass
        rgb = _to_opaque_rgb(img).resize((IMG_SIZE, IMG_SIZE), Image.Resampling.HAMMING)
        return np.asarray(rgb, dtype=np.uint8)


def _get_dct_matrix(n: int, device: str = "cuda", dtype=torch.float16) -> torch.Tensor:
    """Generate orthogonal DCT-II transform matrix."""
    matrix = np.zeros((n, n), dtype=np.float32)
    s = math.sqrt(2.0 / n)
    s0 = math.sqrt(1.0 / n)
    for i in range(n):
        for j in range(n):
            scale = s0 if i == 0 else s
            matrix[i, j] = scale * math.cos((math.pi * i * (2 * j + 1)) / (2.0 * n))
    return torch.tensor(matrix, device=device, dtype=dtype)


class _AsyncImageCacheWriter:
    """
    Asynchronous background writer for image cache persistence (SQLite WAL).
    Completely decouples the GPU and decoding workers from the database,
    accumulating batches in memory and committing on a dedicated thread without stalling Tensor Cores.
    """
    def __init__(self, cache_manager: CacheManager, batch_size: int = 1024, flush_interval: float = 0.5):
        self.cache_manager = cache_manager
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._queue: queue.Queue = queue.Queue(maxsize=50000)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="AsyncImageCacheWriter")
        self._thread.start()

    def add_batch(self, entries: List[Tuple[str, int, float, bytes]]):
        for e in entries:
            try:
                self._queue.put_nowait(e)
            except queue.Full:
                try:
                    self._queue.put(e, timeout=0.1)
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
                    self.cache_manager.save_image_batch(batch)
                except Exception:
                    pass
                batch.clear()
                last_flush = now

        if batch:
            try:
                self.cache_manager.save_image_batch(batch)
            except Exception:
                pass

    def close(self):
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)


def extract_image_embeddings(
    records: List[FileRecord],
    batch_size: int = 1024,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    cache_manager: Optional[CacheManager] = None
) -> Tuple[List[FileRecord], torch.Tensor]:
    """
    Load images in parallel maximizing CPU efficiency and I/O throughput,
    alimenta a RTX 5090 em lotes de alto rendimento e calcula embeddings normalizados.
    Uses persistent SQLite cache (WAL) to load already-analyzed images instantaneously.
    Retorna (registros_validos, tensor_embeddings).
    """
    device, dtype = get_compute_device()
    cache = cache_manager or CacheManager()

    total = len(records)
    dim_size = DIM_SIZE
    if total == 0:
        return [], torch.empty((0, dim_size), device=device, dtype=dtype)

    # 1. Batch lookup in persistent cache with continuous telemetry
    def _image_cache_cb(msg: str, curr: int, tot: int):
        if progress_callback:
            progress_callback(msg, curr, tot * 2)

    if progress_callback:
        progress_callback(f"Checking cache ({total:,} images)...", 0, total * 2)

    cached_map = cache.get_image_cache(
        records,
        expected_bytes=dim_size * 2,
        progress_callback=_image_cache_cb if progress_callback else None,
        stop_check=stop_check,
    )
    if stop_check and stop_check():
        return [], torch.empty((0, dim_size), device=device, dtype=dtype)
    cached_count = len(cached_map)

    valid_records: List[FileRecord] = []
    cached_recs: List[FileRecord] = []
    to_process: List[FileRecord] = []

    # Separate already-cached images (ignoring b"" tombstones) from those needing processing
    for rec in records:
        if stop_check and stop_check():
            return [], torch.empty((0, dim_size), device=device, dtype=dtype)
        p_key = str(rec.path)
        if p_key in cached_map:
            if len(cached_map[p_key]) > 0:
                cached_recs.append(rec)
        else:
            to_process.append(rec)

    all_embeddings_list: List[torch.Tensor] = []

    if cached_recs:
        valid_records.extend(cached_recs)
        # Pre-allocate contiguous matrix in RAM once (eliminates 140k micro-allocations and fragmentation)
        cached_mat = np.empty((len(cached_recs), dim_size), dtype=np.float16)
        for idx, r in enumerate(cached_recs):
            cached_mat[idx] = np.frombuffer(cached_map[str(r.path)], dtype=np.float16)
        with torch.inference_mode():
            all_embeddings_list.append(torch.from_numpy(cached_mat).to(device=device, dtype=dtype))
        del cached_mat

    stage_total = total + len(to_process)
    if progress_callback and cached_count > 0:
        progress_callback(
            f"Loading cached images ({cached_count:,}/{total:,})...",
            total,
            stage_total if to_process else total,
        )

    # 2. Se todas estavam no cache, finaliza imediatamente!
    if not to_process or (stop_check and stop_check()):
        if not all_embeddings_list:
            return [], torch.empty((0, dim_size), device=device, dtype=dtype)
        with torch.inference_mode():
            all_embeddings = torch.cat(all_embeddings_list, dim=0)
            all_embeddings_list.clear()
        return valid_records, all_embeddings

    # 3. Ultra-High-Throughput Processing: Concurrent Producer-Consumer Streaming
    # Threads de CPU decodificam continuamente para uma fila delimitada em RAM,
    # enquanto a GPU (Tensor Cores) e o SQLite operam sem tempo ocioso em paralelo.
    to_process.sort(key=lambda r: str(r.path.parent))

    dct_mat = _get_dct_matrix(IMG_SIZE, device=device, dtype=torch.float32)
    dct_mat_t = dct_mat.t()
    u = torch.arange(LOW_FREQ_SIZE, device=device, dtype=torch.float32).unsqueeze(1)
    v = torch.arange(LOW_FREQ_SIZE, device=device, dtype=torch.float32).unsqueeze(0)
    # Formato (1, 1, LOW_FREQ_SIZE, LOW_FREQ_SIZE) para broadcasting perfeito em tensores RGB [B, 3, H, W]
    spectral_weight = (u + v + 1.0).pow(ALPHA).unsqueeze(0).unsqueeze(0)

    num_workers = min(32, max(4, os.cpu_count() or 4))
    uncached_total = len(to_process)
    effective_batch = max(64, min(batch_size, 1024))

    img_queue: queue.Queue = queue.Queue(maxsize=2048)
    stop_event = threading.Event()
    cache_writer = _AsyncImageCacheWriter(cache)

    def _worker_producer(records_slice: List[FileRecord]):
        for rec in records_slice:
            if stop_event.is_set() or (stop_check and stop_check()):
                break
            opened_ok = False
            try:
                supports_fid = get_volume_capabilities(rec.path)[1]
                with open(rec.path, "rb") as f:
                    opened_ok = True
                    if supports_fid and (rec.volume_id == 0 or rec.file_id == 0):
                        st = os.fstat(f.fileno())
                        rec.volume_id = getattr(st, "st_dev", 0) or 0
                        rec.file_id = getattr(st, "st_ino", 0) or 0

                    if rec.size <= _SINGLE_READ_IMAGE_THRESHOLD:
                        raw_bytes = f.read()
                        if rec.prefix_hash is None:
                            rec.prefix_hash = compute_prefix_hash_from_bytes(raw_bytes)
                        arr = _decode_image_from_stream(io.BytesIO(raw_bytes))
                    else:
                        if rec.prefix_hash is None:
                            rec.prefix_hash = compute_prefix_hash_from_stream(f, rec.size)
                            f.seek(0)
                        arr = _decode_image_from_stream(f)
                item = (rec, arr, False)
            except Exception:
                # Only write a permanent tombstone if the file opened and content decoding failed
                item = (rec, None, opened_ok)

            while not stop_event.is_set():
                if stop_check and stop_check():
                    break
                try:
                    img_queue.put(item, timeout=0.05)
                    break
                except queue.Full:
                    continue

    # Distribui os arquivos uniformemente entre as threads produtoras
    slice_size = (uncached_total + num_workers - 1) // num_workers
    worker_threads: List[threading.Thread] = []
    for w_idx in range(num_workers):
        start_idx = w_idx * slice_size
        end_idx = min(start_idx + slice_size, uncached_total)
        chunk = to_process[start_idx:end_idx]
        if chunk:
            t = threading.Thread(target=_worker_producer, args=(chunk,), daemon=True)
            t.start()
            worker_threads.append(t)

    # Consumer: Feeds the GPU in contiguous blocks and delegates persistence to AsyncCacheWriter
    batch_records: List[FileRecord] = []
    batch_images: List[np.ndarray] = []
    tombstone_entries: List[Tuple] = []
    processed_count = 0
    last_progress_time = 0.0

    try:
        with torch.inference_mode():
            while processed_count < uncached_total:
                if stop_check and stop_check():
                    stop_event.set()
                    break

                try:
                    rec, arr, is_tombstone = img_queue.get(timeout=0.05)
                    processed_count += 1
                    if arr is not None:
                        batch_records.append(rec)
                        batch_images.append(arr)
                    elif is_tombstone:
                        tombstone_entries.append((str(rec.path), rec.size, rec.mtime, b"", rec.volume_id, rec.file_id, rec.prefix_hash))

                    # Process batch on GPU (starts at 64 for instant <30ms response, then scales to effective_batch)
                    target_batch = 64 if processed_count < 256 else effective_batch
                    if len(batch_records) >= target_batch or processed_count == uncached_total:
                        if batch_images:
                            stacked = np.stack(batch_images)
                            # Compute DCT, whitening, and L2 norm in FP32 for maximum precision before quantizing to FP16
                            t_batch = torch.from_numpy(stacked).permute(0, 3, 1, 2).to(device=device, dtype=torch.float32) / 255.0
                            dct_3d = torch.matmul(dct_mat, torch.matmul(t_batch, dct_mat_t))
                            low_freq = dct_3d[:, :, :LOW_FREQ_SIZE, :LOW_FREQ_SIZE].clone()
                            # Zero out the DC (0,0) component in each RGB channel to compare only AC spatial frequencies
                            low_freq[:, :, 0, 0] = 0.0
                            # Spectral equalization (whitening): prevents static backgrounds from dominating the subject
                            weighted = low_freq * spectral_weight
                            flat = weighted.reshape(weighted.shape[0], -1)

                            norms = torch.norm(flat, p=2, dim=1, keepdim=True).clamp(min=1e-6)
                            normalized = (flat / norms).to(dtype=dtype)

                            all_embeddings_list.append(normalized)
                            valid_records.extend(batch_records)

                            norm_cpu_np = normalized.to(dtype=torch.float16).cpu().numpy()
                            new_entries = [
                                (str(b_rec.path), b_rec.size, b_rec.mtime, norm_cpu_np[b_idx:b_idx + 1].tobytes(), b_rec.volume_id, b_rec.file_id, b_rec.prefix_hash)
                                for b_idx, b_rec in enumerate(batch_records)
                            ]
                            cache_writer.add_batch(new_entries)

                            batch_records.clear()
                            batch_images.clear()

                    if tombstone_entries and (len(tombstone_entries) >= 512 or processed_count == uncached_total):
                        cache_writer.add_batch(tombstone_entries)
                        tombstone_entries.clear()

                    now = time.time()
                    if progress_callback and (now - last_progress_time >= 0.08 or processed_count == uncached_total):
                        last_progress_time = now
                        done_count = cached_count + processed_count
                        progress_callback(
                            f"Processing images ({done_count:,}/{total:,})...",
                            total + processed_count,
                            stage_total
                        )
                except queue.Empty:
                    if all(not t.is_alive() for t in worker_threads) and img_queue.empty():
                        break
    finally:
        if tombstone_entries:
            cache_writer.add_batch(tombstone_entries)
            tombstone_entries.clear()
        stop_event.set()
        cache_writer.close()
        for t in worker_threads:
            t.join(timeout=0.5)

    if not all_embeddings_list:
        return [], torch.empty((0, DIM_SIZE), device=device, dtype=dtype)

    with torch.inference_mode():
        all_embeddings = torch.cat(all_embeddings_list, dim=0)
        all_embeddings_list.clear()
    return valid_records, all_embeddings
