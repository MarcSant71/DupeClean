"""
Ultra-High-Performance Video Signature Extractor with Parallel Multiprocessing and GPU Tensor Cores.
Completely eliminates Python GIL contention via an isolated ProcessPoolExecutor,
continuously feeds the GPU, and processes 2D DCT tensors directly in VRAM (FP16).
"""

import os
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import List, Optional, Tuple, Callable, Dict, Any
import numpy as np
import torch
from PIL import Image

import av
av.logging.set_level(av.logging.PANIC)

from dupeclean.core.models import FileRecord, format_size
from dupeclean.core.hardware import get_compute_device
from dupeclean.core.hasher_image import _get_dct_matrix
from dupeclean.core.cache import AsyncBatchWriter, CacheManager, compute_prefix_hash
from dupeclean.core.scanner import get_volume_capabilities
from dupeclean.core._video_worker import (
    VIDEO_CHANNELS,
    VIDEO_DIM_SIZE,
    VIDEO_IMG_SIZE,
    VIDEO_LOW_FREQ_SIZE,
    VIDEO_NUM_SAMPLES,
    _worker_decode_path,
    crop_letterbox,
)

# Calibrated spectral whitening for video re-encodes (balances structural detail vs MPEG-4/H.264 noise)
VIDEO_ALPHA = 1.0


def get_video_duration(path: Path) -> Optional[float]:
    """Get video duration natively in memory via PyAV with minimal I/O."""
    is_dav = path.suffix.lower() == ".dav"
    options = {"probesize": "65536", "analyzeduration": "0"} if is_dav else None
    try:
        container = av.open(str(path), options=options)
        dur = float(container.duration) / 1000000.0 if container.duration else None
        if dur is None and container.streams.video:
            s = container.streams.video[0]
            if s.duration and s.time_base:
                dur = float(s.duration * s.time_base)
        container.close()
        return dur
    except Exception:
        return None


def extract_video_frames_and_duration(
    path: Path,
    num_samples: int = 5
) -> Optional[Tuple[float, np.ndarray]]:
    """Compatible direct path-based extraction function."""
    res = _worker_decode_path(str(path))
    if res is None:
        return None
    _, dur, arr, _, _, _ = res
    if arr is None or dur == -1.0:
        return None
    return dur, arr


def _terminate_pool_processes(executor: Optional[ProcessPoolExecutor]) -> None:
    """Terminate active child processes on cancellation/broken pool so Stop never hangs."""
    if executor is None:
        return
    try:
        for proc in list(getattr(executor, "_processes", {}).values()):
            try:
                if proc.is_alive():
                    proc.terminate()
            except Exception:
                pass
    except Exception:
        pass


def extract_video_embeddings(
    records: List[FileRecord],
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    cache_manager: Optional[CacheManager] = None
) -> Tuple[List[FileRecord], torch.Tensor]:
    """
    Process videos at ultra-high speed using parallel multiprocessing (ProcessPoolExecutor)
    and batch consolidation (CUDA FP16 on RTX GPUs or CPU FP32 as universal fallback).
    Ensures per-file memory isolation (zero frame leaks) and SQLite WAL persistence.
    """
    device, dtype = get_compute_device()
    cache = cache_manager or CacheManager()

    total = len(records)
    total_bytes = sum(r.size for r in records)
    dim_size = VIDEO_DIM_SIZE
    if total == 0:
        return [], torch.empty((0, dim_size), device=device, dtype=dtype)

    # 1. Consulta em lote ao cache persistente (valida 7.680 bytes estruturais)
    def _video_cache_cb(msg: str, curr: int, tot: int):
        if progress_callback:
            progress_callback(msg, curr, tot * 2)

    if progress_callback:
        progress_callback(f"Checking video cache ({total:,} videos, {format_size(total_bytes)})...", 0, total * 2)

    cached_map = cache.get_video_cache(
        records,
        expected_bytes=dim_size * 2,
        progress_callback=_video_cache_cb if progress_callback else None,
        stop_check=stop_check,
    )
    if stop_check and stop_check():
        return [], torch.empty((0, dim_size), device=device, dtype=dtype)
    cached_count = len(cached_map)

    valid_records: List[FileRecord] = []
    cached_recs: List[FileRecord] = []
    to_process: List[FileRecord] = []

    for rec in records:
        if stop_check and stop_check():
            return [], torch.empty((0, dim_size), device=device, dtype=dtype)
        p_key = str(rec.path)
        if p_key in cached_map:
            dur, emb_blob = cached_map[p_key]
            if dur == -1.0 or len(emb_blob) == 0:
                # Corrupted/unreadable file already marked as tombstone: skip without reprocessing
                continue
            rec.duration = dur
            cached_recs.append(rec)
        else:
            to_process.append(rec)

    embeddings_list: List[torch.Tensor] = []

    if cached_recs:
        valid_records.extend(cached_recs)
        cached_mat = np.empty((len(cached_recs), dim_size), dtype=np.float16)
        for idx, r in enumerate(cached_recs):
            _, emb_blob = cached_map[str(r.path)]
            cached_mat[idx] = np.frombuffer(emb_blob, dtype=np.float16)
        with torch.inference_mode():
            embeddings_list.append(torch.from_numpy(cached_mat).to(device=device, dtype=dtype))
        del cached_mat

    cached_bytes = sum(r.size for r in cached_recs)
    stage_total = total + len(to_process)
    if progress_callback and cached_count > 0:
        progress_callback(
            f"Loading cached videos of {cached_count:,}/{total:,} files ({format_size(cached_bytes)} / {format_size(total_bytes)})...",
            total,
            stage_total if to_process else total,
        )

    # 2. If all videos were already cached, return immediately
    if not to_process or (stop_check and stop_check()):
        if not embeddings_list:
            return [], torch.empty((0, dim_size), device=device, dtype=dtype)
        with torch.inference_mode():
            all_embeddings = torch.cat(embeddings_list, dim=0)
        return valid_records, all_embeddings

    # 3. Prepare DCT transforms on GPU with Spectral Whitening
    to_process.sort(key=lambda r: str(r.path.parent))

    dct_mat = _get_dct_matrix(VIDEO_IMG_SIZE, device=device, dtype=dtype)
    dct_mat_t = dct_mat.t()

    u = torch.arange(VIDEO_LOW_FREQ_SIZE, device=device, dtype=dtype).unsqueeze(1)
    v = torch.arange(VIDEO_LOW_FREQ_SIZE, device=device, dtype=dtype).unsqueeze(0)
    spectral_weight = (u + v + 1.0).pow(VIDEO_ALPHA)
    spectral_weight[0, 0] = 0.0  # Zero DC

    uncached_total = len(to_process)
    cache_writer = AsyncBatchWriter(
        cache.save_video_batch,
        batch_size=512,
        max_queue=50000,
        name="AsyncVideoCacheWriter",
    )

    # Fast mapping from paths to FileRecord objects
    path_to_rec = {str(r.path): r for r in to_process}
    path_list = list(path_to_rec.keys())

    # Optimal worker process count (saturates I/O without overspawning on small batches)
    num_workers = min(16, max(4, (os.cpu_count() or 4) // 2), uncached_total)
    window_size = 512

    processed_count = 0
    processed_bytes = cached_bytes
    last_progress_time = 0.0
    batch_records: List[FileRecord] = []
    batch_frames: List[np.ndarray] = []
    batch_durations: List[float] = []

    if progress_callback:
        progress_callback(
            f"Processing videos of {cached_count:,}/{total:,} files ({format_size(cached_bytes)} / {format_size(total_bytes)})...",
            total,
            stage_total,
        )

    def _flush_gpu_batch() -> None:
        if not batch_frames:
            return
        stacked = np.stack(batch_frames)  # [B, 5, 64, 64, 3]
        t_batch = torch.from_numpy(stacked).permute(0, 1, 4, 2, 3).to(device=device, dtype=dtype) / 255.0
        b_size = t_batch.shape[0]
        t_flat = t_batch.reshape(b_size * VIDEO_NUM_SAMPLES * VIDEO_CHANNELS, VIDEO_IMG_SIZE, VIDEO_IMG_SIZE)
        dct_batch = torch.matmul(dct_mat, torch.matmul(t_flat, dct_mat_t))
        low_freq = dct_batch[:, :VIDEO_LOW_FREQ_SIZE, :VIDEO_LOW_FREQ_SIZE].clone()
        low_freq = low_freq * spectral_weight.unsqueeze(0)

        flat = low_freq.reshape(b_size, -1)  # [B, 3840]
        norms = torch.norm(flat, p=2, dim=1, keepdim=True).clamp(min=1e-6)
        normalized = flat / norms  # [B, 3840]

        embeddings_list.append(normalized)
        valid_records.extend(batch_records)

        norm_cpu_np = normalized.to(dtype=torch.float16).cpu().numpy()
        for b_i, b_rec in enumerate(batch_records):
            cache_writer.add((
                str(b_rec.path),
                b_rec.size,
                b_rec.mtime,
                batch_durations[b_i],
                norm_cpu_np[b_i:b_i + 1].tobytes(),
                b_rec.volume_id,
                b_rec.file_id,
                b_rec.prefix_hash,
            ))

        batch_records.clear()
        batch_frames.clear()
        batch_durations.clear()

    def _handle_decoded_result(res) -> None:
        nonlocal processed_count, processed_bytes, last_progress_time
        processed_count += 1
        current_total_done = cached_count + processed_count

        if res is not None:
            p_str, duration, frames_arr, vol_id, f_id, p_hash = res
            rec = path_to_rec.get(p_str)
            if rec is not None:
                processed_bytes += rec.size
                if vol_id and rec.volume_id == 0:
                    rec.volume_id = vol_id
                if f_id and rec.file_id == 0:
                    rec.file_id = f_id
                if p_hash and rec.prefix_hash is None:
                    rec.prefix_hash = p_hash
                expected_shape = (VIDEO_NUM_SAMPLES, VIDEO_IMG_SIZE, VIDEO_IMG_SIZE, VIDEO_CHANNELS)
                if frames_arr is None or duration == -1.0 or getattr(frames_arr, "shape", None) != expected_shape:
                    # Persist tombstone in SQLite so corrupted files are never reprocessed
                    cache_writer.add((
                        str(rec.path),
                        rec.size,
                        rec.mtime,
                        -1.0,
                        b"",
                        rec.volume_id,
                        rec.file_id,
                        rec.prefix_hash,
                    ))
                else:
                    rec.duration = duration
                    batch_records.append(rec)
                    batch_frames.append(frames_arr)
                    batch_durations.append(duration)

        now = time.time()
        if progress_callback and (now - last_progress_time >= 0.08 or processed_count == uncached_total):
            last_progress_time = now
            progress_callback(
                f"Processing videos of {current_total_done:,}/{total:,} files ({format_size(processed_bytes)} / {format_size(total_bytes)})...",
                total + processed_count,
                stage_total,
            )

        if len(batch_records) >= 128 or processed_count == uncached_total:
            _flush_gpu_batch()

    executor: Optional[ProcessPoolExecutor] = None
    cancelled = False
    try:
        with torch.inference_mode():
            if uncached_total <= 4:
                for p_str in path_list:
                    if stop_check and stop_check():
                        cancelled = True
                        break
                    _handle_decoded_result(_worker_decode_path(p_str))
            else:
                executor = ProcessPoolExecutor(max_workers=num_workers)
                # Bounded submission windows prevent queuing 100k+ futures at once and speed up Stop
                for win_start in range(0, uncached_total, window_size):
                    if stop_check and stop_check():
                        cancelled = True
                        break
                    win_paths = path_list[win_start:win_start + window_size]
                    chunksize = max(1, min(4, len(win_paths) // (num_workers * 2) or 1))
                    try:
                        for res in executor.map(_worker_decode_path, win_paths, chunksize=chunksize):
                            if stop_check and stop_check():
                                cancelled = True
                                break
                            _handle_decoded_result(res)
                    except BrokenProcessPool:
                        # Isolate worker crash cleanly and recreate pool for subsequent windows
                        _terminate_pool_processes(executor)
                        executor.shutdown(wait=False, cancel_futures=True)
                        executor = ProcessPoolExecutor(max_workers=num_workers)
                        processed_count += max(0, (win_start + len(win_paths)) - processed_count)
                        _flush_gpu_batch()
                        continue
                    if cancelled:
                        break
            if not cancelled:
                _flush_gpu_batch()

    finally:
        if executor is not None:
            if cancelled:
                _terminate_pool_processes(executor)
                executor.shutdown(wait=False, cancel_futures=True)
            else:
                executor.shutdown(wait=True, cancel_futures=True)
        cache_writer.close()

    if not embeddings_list:
        return [], torch.empty((0, dim_size), device=device, dtype=dtype)

    with torch.inference_mode():
        all_embeddings = torch.cat(embeddings_list, dim=0)
    return valid_records, all_embeddings
