"""
Extrator de Assinaturas Visuais Híbridas de Imagens via GPU (RTX Tensor Cores + CPU Fallback).
Combina embeddings semânticos DINOv2-ViT-S/14 (384-D, FlashAttention FP16) com DCT 2D YCbCr
e vetor de cor assinado (384-D) em uma assinatura compacta de 768-D (1.536 bytes em FP16),
detectando duplicatas redimensionadas, recortadas, com marca d'água, rajadas e .heic nativos.
"""

from typing import Dict, List, Callable, Optional, Set, Tuple
import io
import os
import math
import time
import queue
import threading
from PIL import Image, ImageOps
import torch
import numpy as np

from dupeclean.core.models import FileRecord, DuplicateGroup, ScanMode, format_size
from dupeclean.core.conversion_logger import log_conversion_error
from dupeclean.core.hardware import (
    choose_embedding_storage_device,
    get_compute_device,
    get_optimal_gpu_budgets,
)
from dupeclean.core.scanner import get_storage_kind, get_volume_capabilities
from dupeclean.core.cache import (
    AsyncBatchWriter,
    CacheManager,
    _get_drive_volume_id,
    _open_random_binary,
    compute_prefix_hash_from_bytes,
    compute_prefix_hash_from_stream,
)
from dupeclean.core.neural_backbone import (
    NEURAL_INPUT_SIZE,
    compute_hybrid_visual_embeddings,
    get_neural_backbone,
)

_HEIF_REGISTERED = False


def ensure_heif_registered() -> bool:
    """Register pillow-heif opener once so .heic/.heif files decode natively in Pillow."""
    global _HEIF_REGISTERED
    if _HEIF_REGISTERED:
        return True
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        _HEIF_REGISTERED = True
        return True
    except Exception:
        return False


ensure_heif_registered()

# High-discrimination visual signature extraction parameters
IMG_SIZE = 64
LOW_FREQ_SIZE = 16
CHANNELS = 3
ALPHA = 1.2  # Calibrated spectral whitening: attenuates static background without amplifying compression noise
DIM_SIZE = CHANNELS * LOW_FREQ_SIZE * LOW_FREQ_SIZE  # 768 dimensions (1,536 bytes in FP16)
_SINGLE_READ_IMAGE_THRESHOLD = 4 * 1024 * 1024  # 4 MB cap prevents RAM spikes across concurrent workers

_EXIF_TRANSPOSE_MAP = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


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


def _decode_image_from_stream(stream, target_size: int = IMG_SIZE) -> np.ndarray:
    """Decode with draft prescaling, anti-aliased downsampling, and fast post-resize EXIF orientation fix."""
    with Image.open(stream) as img:
        img.draft("RGB", (target_size * 2, target_size * 2))
        orient = 1
        try:
            orient = img.getexif().get(274, 1)
        except Exception:
            pass
        # Because (target_size, target_size) is square, resizing before EXIF transpose operates on tiny buffers
        resampling = Image.Resampling.BILINEAR if target_size > IMG_SIZE else Image.Resampling.HAMMING
        rgb = _to_opaque_rgb(img).resize((target_size, target_size), resampling)
        op = _EXIF_TRANSPOSE_MAP.get(orient)
        if op is not None:
            rgb = rgb.transpose(op)
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


def extract_image_embeddings(
    records: List[FileRecord],
    batch_size: int = 1024,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    cache_manager: Optional[CacheManager] = None
) -> Tuple[List[FileRecord], torch.Tensor]:
    """
    Load images in parallel maximizing CPU efficiency and I/O throughput,
    feed the RTX Tensor Cores (or CPU FP32 fallback) in VRAM-adaptive batches,
    and compute compact 768-D Hybrid Visual Embeddings (DINOv2-ViT-S/14 + YCbCr 2D DCT).
    Uses persistent SQLite cache (WAL) to load already-analyzed images instantaneously.
    """
    device, dtype = get_compute_device()
    cache = cache_manager or CacheManager()

    total = len(records)
    total_bytes = sum(r.size for r in records)
    dim_size = DIM_SIZE
    if total == 0:
        return [], torch.empty((0, dim_size), device=device, dtype=dtype)

    # 1. Zero-I/O Stage-2 L1 lookup + lightweight RAM index preload (L2/L3 execute inline in Stage 3)
    if progress_callback:
        progress_callback(f"Checking cache ({total:,} images, {format_size(total_bytes)})...", 0, total)

    cached_map, l2_index, l3_index, l3_known_sizes, l2_known_volumes = cache.get_image_cache_l1_and_indexes(
        records,
        expected_bytes=dim_size * 2,
        progress_callback=progress_callback,
        stop_check=stop_check,
    )
    if stop_check and stop_check():
        return [], torch.empty((0, dim_size), device=device, dtype=dtype)
    cached_count = len(cached_map)

    valid_records: List[FileRecord] = []
    cached_recs: List[FileRecord] = []
    to_process: List[FileRecord] = []

    # Separate L1-cached images (ignoring b"" tombstones) from those entering Stage 3
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
    store_device = choose_embedding_storage_device(total, dim_size, device, dtype)
    store_dtype = torch.float16 if (store_device == "cpu" and device == "cuda") else dtype

    if cached_recs:
        valid_records.extend(cached_recs)
        cached_mat = np.empty((len(cached_recs), dim_size), dtype=np.float16)
        for idx, r in enumerate(cached_recs):
            cached_mat[idx] = np.frombuffer(cached_map[str(r.path)], dtype=np.float16)
        cached_map.clear()
        with torch.inference_mode():
            all_embeddings_list.append(
                torch.from_numpy(cached_mat).to(
                    device=store_device,
                    dtype=store_dtype,
                    non_blocking=(store_device == "cuda"),
                )
            )
        del cached_mat
    else:
        cached_map.clear()

    cached_bytes = sum(r.size for r in cached_recs)
    if progress_callback and cached_count > 0:
        progress_callback(
            f"Loading cached images of {cached_count:,}/{total:,} files ({format_size(cached_bytes)} / {format_size(total_bytes)})...",
            total,
            total,
        )

    # 2. If all images were already L1-cached, finish immediately
    if not to_process or (stop_check and stop_check()):
        if not all_embeddings_list:
            return [], torch.empty((0, dim_size), device=device, dtype=dtype)
        with torch.inference_mode():
            all_embeddings = torch.cat(all_embeddings_list, dim=0)
            all_embeddings_list.clear()
        return valid_records, all_embeddings

    # 3. Unified Single-Open Streaming Pipeline: inline L2/L3 + CPU decode + RTX GPU inference
    uncached_total = len(to_process)
    if progress_callback:
        progress_callback(
            f"Processing images of {cached_count:,}/{total:,} files ({format_size(cached_bytes)} / {format_size(total_bytes)})...",
            0,
            uncached_total,
        )

    # Pre-resolve per-drive capabilities, storage speed tier, and per-drive I/O concurrency caps once
    _STORAGE_PRIORITY = {"ssd": 0, "unknown": 1, "iscsi": 2, "network": 2, "hdd": 3}
    cpu_cnt = os.cpu_count() or 4
    drive_supports_fid: Dict[str, bool] = {}
    drive_has_l2: Dict[str, bool] = {}
    drive_storage_kind: Dict[str, str] = {}
    drive_semaphores: Dict[str, threading.Semaphore] = {}
    drive_vol_cache: Dict[str, int] = {}
    has_l2_index = bool(l2_index)
    max_img_workers = 4

    for r in to_process:
        drv = os.path.splitdrive(str(r.path))[0].upper()
        if drv in drive_supports_fid:
            continue
        sup_fid = get_volume_capabilities(r.path)[1]
        drive_supports_fid[drv] = sup_fid
        if sup_fid and has_l2_index:
            d_vol = _get_drive_volume_id(r.path, drive_vol_cache)
            drive_has_l2[drv] = (d_vol == 0) or (d_vol in l2_known_volumes)
        else:
            drive_has_l2[drv] = False

        kind = get_storage_kind(r.path)
        drive_storage_kind[drv] = kind
        if kind == "hdd":
            drv_cap = 6
        elif kind in ("iscsi", "network"):
            drv_cap = min(12, max(6, cpu_cnt // 2))
        else:
            drv_cap = min(16, max(4, cpu_cnt))
        drive_semaphores[drv] = threading.Semaphore(drv_cap)
        if drv_cap > max_img_workers:
            max_img_workers = drv_cap

    # Sort fastest drives FIRST (SSD -> iSCSI/network -> HDD) and by directory locality within each drive:
    # SSD drives finish rapidly and warm intra_l3 so duplicate copies on slower iSCSI/HDD drives resolve in 8 KB!
    to_process.sort(
        key=lambda r: (
            _STORAGE_PRIORITY.get(
                drive_storage_kind.get(os.path.splitdrive(str(r.path))[0].upper(), "unknown"),
                1,
            ),
            str(r.path),
        )
    )

    # Identify sizes repeated within this scan; intra_l3_sizes is populated dynamically ONLY after
    # copy #1 actually completes GPU inference, so copy #1 uses full sequential read-ahead (zero O_RANDOM penalty!)
    size_counts: Dict[int, int] = {}
    for r in to_process:
        size_counts[r.size] = size_counts.get(r.size, 0) + 1
    dup_sizes: Set[int] = {sz for sz, cnt in size_counts.items() if cnt >= 2}
    intra_l3: Dict[Tuple[int, str], bytes] = {}
    intra_l3_sizes: Set[int] = set()
    intra_l3_lock = threading.Lock()

    backbone_bundle = get_neural_backbone(device=device, dtype=dtype)
    use_neural = backbone_bundle is not None
    decode_size = NEURAL_INPUT_SIZE if use_neural else IMG_SIZE

    dct_mat = _get_dct_matrix(IMG_SIZE, device=device, dtype=torch.float32)
    dct_mat_t = dct_mat.t()
    u16 = torch.arange(LOW_FREQ_SIZE, device=device, dtype=torch.float32).unsqueeze(1)
    v16 = torch.arange(LOW_FREQ_SIZE, device=device, dtype=torch.float32).unsqueeze(0)
    sw16 = (u16 + v16 + 1.0).pow(ALPHA)
    sw16[0, 0] = 0.0
    u8 = torch.arange(8, device=device, dtype=torch.float32).unsqueeze(1)
    v8 = torch.arange(8, device=device, dtype=torch.float32).unsqueeze(0)
    sw8 = (u8 + v8 + 1.0).pow(ALPHA)
    sw8[0, 0] = 0.0
    # Fallback pure-DCT spectral weight shape (1, 1, 16, 16)
    spectral_weight_rgb = (u16 + v16 + 1.0).pow(ALPHA).unsqueeze(0).unsqueeze(0)

    # Pool scales to the fastest drive present while per-drive semaphores enforce exact HDD(6)/iSCSI(12)/SSD(16) caps
    num_workers = min(max_img_workers, max(4, cpu_cnt), uncached_total)

    if use_neural:
        budgets = get_optimal_gpu_budgets()
        neural_cap = int(budgets.get("neural_batch_size", 64 if device == "cuda" else 32))
        effective_batch = max(16, min(batch_size, neural_cap))
        queue_cap = 256  # Caps decoded 224x224 RGB queue RAM to ~38 MB
    else:
        effective_batch = max(64, min(batch_size, 1024))
        queue_cap = 512

    img_queue: queue.Queue = queue.Queue(maxsize=queue_cap)
    stop_event = threading.Event()
    cache_writer = cache.create_batch_writer(
        "images",
        batch_size=1024,
        max_queue=50000,
        name="AsyncImageCacheWriter",
    )

    # Dynamic work-stealing batch cursor preserves directory locality while eliminating tail stragglers
    next_idx = 0
    idx_lock = threading.Lock()
    work_batch = 16

    def _worker_producer():
        nonlocal next_idx
        while not stop_event.is_set():
            if stop_check and stop_check():
                break
            with idx_lock:
                if next_idx >= uncached_total:
                    return
                start_i = next_idx
                end_i = min(uncached_total, start_i + work_batch)
                next_idx = end_i

            for rec in to_process[start_i:end_i]:
                if stop_event.is_set() or (stop_check and stop_check()):
                    return
                opened_ok = False
                drv = os.path.splitdrive(str(rec.path))[0].upper()
                supports_fid = drive_supports_fid.get(drv, False)
                has_l2 = drive_has_l2.get(drv, False)
                # Only probe 8 KB L3 when an embedding of this size ALREADY exists in SQLite or intra_l3
                check_l3 = (rec.size in l3_known_sizes) or (rec.size in intra_l3_sizes)
                drv_sem = drive_semaphores.get(drv)
                # Tuple format: (rec, decoded_arr, is_tombstone, inline_emb_blob, stale_src_path)
                try:
                    if drv_sem is not None:
                        drv_sem.acquire()
                    try:
                        opener = _open_random_binary(rec.path, rec.size) if check_l3 else open(rec.path, "rb")
                        with opener as f:
                            opened_ok = True
                            if supports_fid and (rec.volume_id == 0 or rec.file_id == 0):
                                st = os.fstat(f.fileno())
                                rec.volume_id = getattr(st, "st_dev", 0) or 0
                                rec.file_id = getattr(st, "st_ino", 0) or 0

                            # Inline Tier 2 (L2): 0 bytes read from image body on moved/renamed files
                            if has_l2 and rec.volume_id > 0 and rec.file_id > 0:
                                l2_hit = l2_index.get((rec.volume_id, rec.file_id))
                                if (
                                    l2_hit is not None
                                    and rec.size == l2_hit[1]
                                    and abs(rec.mtime - l2_hit[2]) < 0.001
                                ):
                                    c_path, _, _, emb_blob, c_prefix = l2_hit
                                    if c_prefix:
                                        rec.prefix_hash = c_prefix
                                    item = (rec, None, False, emb_blob, c_path)
                                    while not stop_event.is_set():
                                        if stop_check and stop_check():
                                            return
                                        try:
                                            img_queue.put(item, timeout=0.05)
                                            break
                                        except queue.Full:
                                            continue
                                    continue

                            # Inline Tier 3 (L3): read at most 8 KB (Head 4 KB + Tail 4 KB) before full decode
                            if check_l3:
                                if rec.prefix_hash is None:
                                    rec.prefix_hash = compute_prefix_hash_from_stream(f, rec.size)
                                if rec.prefix_hash:
                                    l3_hit = l3_index.get((rec.size, rec.prefix_hash))
                                    if l3_hit is not None:
                                        c_path, emb_blob = l3_hit
                                        item = (rec, None, False, emb_blob, c_path)
                                        while not stop_event.is_set():
                                            if stop_check and stop_check():
                                                return
                                            try:
                                                img_queue.put(item, timeout=0.05)
                                                break
                                            except queue.Full:
                                                continue
                                        continue
                                    if rec.size in dup_sizes:
                                        with intra_l3_lock:
                                            intra_emb = intra_l3.get((rec.size, rec.prefix_hash))
                                        if intra_emb is not None:
                                            item = (rec, None, False, intra_emb, None)
                                            while not stop_event.is_set():
                                                if stop_check and stop_check():
                                                    return
                                                try:
                                                    img_queue.put(item, timeout=0.05)
                                                    break
                                                except queue.Full:
                                                    continue
                                            continue
                                f.seek(0)

                            # Full decode on the SAME already-open handle (single bulk read avoids 4 KB loop)
                            if rec.size <= _SINGLE_READ_IMAGE_THRESHOLD:
                                raw_bytes = f.read(max(rec.size + 1, 8192))
                                if len(raw_bytes) > rec.size:
                                    raw_bytes += f.read()
                                if rec.prefix_hash is None:
                                    rec.prefix_hash = compute_prefix_hash_from_bytes(raw_bytes)
                                arr = _decode_image_from_stream(io.BytesIO(raw_bytes), target_size=decode_size)
                            else:
                                if rec.prefix_hash is None:
                                    rec.prefix_hash = compute_prefix_hash_from_stream(f, rec.size)
                                    f.seek(0)
                                arr = _decode_image_from_stream(f, target_size=decode_size)
                    finally:
                        if drv_sem is not None:
                            drv_sem.release()
                    item = (rec, arr, False, None, None)
                except Exception as exc:
                    # Log real failure reason to logs/conversion.log without ever interrupting the batch
                    log_conversion_error(
                        "IMAGES",
                        "DECODE" if opened_ok else "READ",
                        rec.path,
                        f"{type(exc).__name__}: {exc}",
                    )
                    # Only write a permanent tombstone if the file opened and content decoding failed
                    item = (rec, None, opened_ok, None, None)

                while not stop_event.is_set():
                    if stop_check and stop_check():
                        return
                    try:
                        img_queue.put(item, timeout=0.05)
                        break
                    except queue.Full:
                        continue

    worker_threads: List[threading.Thread] = []
    for _ in range(num_workers):
        t = threading.Thread(target=_worker_producer, daemon=True)
        t.start()
        worker_threads.append(t)

    # Consumer: Feeds the GPU in contiguous blocks, handles inline L2/L3 hits, persists via AsyncCacheWriter
    batch_records: List[FileRecord] = []
    batch_images: List[np.ndarray] = []
    inline_hit_recs: List[FileRecord] = []
    inline_hit_blobs: List[bytes] = []
    inline_upsert_entries: List[Tuple] = []
    tombstone_entries: List[Tuple] = []
    stale_src_paths: Set[str] = set()
    active_paths: Set[str] = {str(r.path) for r in cached_recs}
    processed_count = 0
    processed_bytes = cached_bytes
    last_progress_time = 0.0

    def _flush_inline_hits() -> None:
        if not inline_hit_recs:
            return
        mat = np.empty((len(inline_hit_recs), dim_size), dtype=np.float16)
        for h_idx, blob in enumerate(inline_hit_blobs):
            mat[h_idx] = np.frombuffer(blob, dtype=np.float16)
        all_embeddings_list.append(
            torch.from_numpy(mat).to(
                device=store_device,
                dtype=store_dtype,
                non_blocking=(store_device == "cuda"),
            )
        )
        valid_records.extend(inline_hit_recs)
        inline_hit_recs.clear()
        inline_hit_blobs.clear()

    try:
        with torch.inference_mode():
            while processed_count < uncached_total:
                if stop_check and stop_check():
                    stop_event.set()
                    break

                try:
                    rec, arr, is_tombstone, inline_blob, stale_src = img_queue.get(timeout=0.05)
                    processed_count += 1
                    processed_bytes += rec.size
                    p_str = str(rec.path)
                    active_paths.add(p_str)

                    if inline_blob is not None:
                        if len(inline_blob) > 0:
                            inline_hit_recs.append(rec)
                            inline_hit_blobs.append(inline_blob)
                            if len(inline_hit_recs) >= 4096:
                                _flush_inline_hits()
                        inline_upsert_entries.append(
                            (p_str, rec.size, rec.mtime, inline_blob, rec.volume_id, rec.file_id, rec.prefix_hash)
                        )
                        if stale_src and stale_src != p_str:
                            stale_src_paths.add(stale_src)
                        if len(inline_upsert_entries) >= 1024:
                            cache_writer.add_batch(inline_upsert_entries)
                            inline_upsert_entries.clear()
                    elif arr is not None:
                        batch_records.append(rec)
                        batch_images.append(arr)
                    elif is_tombstone:
                        tombstone_entries.append((p_str, rec.size, rec.mtime, b"", rec.volume_id, rec.file_id, rec.prefix_hash))
                        if rec.prefix_hash and rec.size in dup_sizes:
                            with intra_l3_lock:
                                intra_l3.setdefault((rec.size, rec.prefix_hash), b"")
                                intra_l3_sizes.add(rec.size)

                    # Start with smaller initial batch for instant UI response, then scale to effective_batch
                    initial_target = min(32 if use_neural else 64, effective_batch)
                    target_batch = initial_target if processed_count < 128 else effective_batch
                    if batch_records and (len(batch_records) >= target_batch or processed_count == uncached_total):
                        stacked = np.stack(batch_images)
                        t_cpu = torch.from_numpy(stacked)
                        if device == "cuda":
                            t_dev = t_cpu.pin_memory().to(device=device, non_blocking=True)
                        else:
                            t_dev = t_cpu
                        t_batch = t_dev.permute(0, 3, 1, 2).to(dtype=torch.float32).mul_(1.0 / 255.0)

                        if use_neural and backbone_bundle is not None:
                            normalized = compute_hybrid_visual_embeddings(
                                t_rgb_224=t_batch,
                                dct64=dct_mat,
                                dct64_t=dct_mat_t,
                                sw16=sw16,
                                sw8=sw8,
                                backbone_bundle=backbone_bundle,
                                out_dtype=dtype,
                            )
                        else:
                            dct_3d = torch.matmul(dct_mat, torch.matmul(t_batch, dct_mat_t))
                            low_freq = dct_3d[:, :, :LOW_FREQ_SIZE, :LOW_FREQ_SIZE].clone()
                            low_freq[:, :, 0, 0] = 0.0
                            weighted = low_freq * spectral_weight_rgb
                            flat = weighted.reshape(weighted.shape[0], -1)
                            norms = torch.norm(flat, p=2, dim=1, keepdim=True).clamp(min=1e-6)
                            normalized = (flat / norms).to(dtype=dtype)

                        all_embeddings_list.append(
                            normalized if store_device == device else normalized.to(device=store_device, dtype=store_dtype)
                        )
                        valid_records.extend(batch_records)

                        norm_cpu_np = normalized.to(dtype=torch.float16).cpu().numpy()
                        new_entries = []
                        intra_updates = []
                        for b_idx, b_rec in enumerate(batch_records):
                            emb_bytes = norm_cpu_np[b_idx:b_idx + 1].tobytes()
                            new_entries.append(
                                (str(b_rec.path), b_rec.size, b_rec.mtime, emb_bytes, b_rec.volume_id, b_rec.file_id, b_rec.prefix_hash)
                            )
                            if b_rec.prefix_hash and b_rec.size in dup_sizes:
                                intra_updates.append(((b_rec.size, b_rec.prefix_hash), emb_bytes))
                        if intra_updates:
                            with intra_l3_lock:
                                for k_intra, v_intra in intra_updates:
                                    intra_l3.setdefault(k_intra, v_intra)
                                    intra_l3_sizes.add(k_intra[0])
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
                            f"Processing images of {done_count:,}/{total:,} files ({format_size(processed_bytes)} / {format_size(total_bytes)})...",
                            processed_count,
                            uncached_total,
                        )
                except queue.Empty:
                    if all(not t.is_alive() for t in worker_threads) and img_queue.empty():
                        break

            # Flush any remaining buffered inline hits
            _flush_inline_hits()
    finally:
        if inline_upsert_entries:
            cache_writer.add_batch(inline_upsert_entries)
            inline_upsert_entries.clear()
        if tombstone_entries:
            cache_writer.add_batch(tombstone_entries)
            tombstone_entries.clear()
        stop_event.set()
        cache_writer.close()
        for t in worker_threads:
            t.join(timeout=0.5)

    if stale_src_paths and not (stop_check and stop_check()):
        try:
            cache.cleanup_stale_paths("cache_images", stale_src_paths, active_paths)
        except Exception:
            pass

    if not all_embeddings_list:
        return [], torch.empty((0, DIM_SIZE), device=device, dtype=dtype)

    with torch.inference_mode():
        all_embeddings = torch.cat(all_embeddings_list, dim=0)
        all_embeddings_list.clear()
    return valid_records, all_embeddings
