"""
High-Performance Video Signature Extractor with Embedded FFmpeg (PyAV) + RTX DINOv2-ViT-S/14 + YCbCr DCT.
- Decodes 5 temporal keyframes per video inside crash-isolated PyAV (FFmpeg 8) worker processes
  with RAM-adaptive pool recycling, surgical 1-by-1 poison-file quarantine, and conversion.log reporting.
- Computes Hybrid DINOv2-ViT-S/14 + YCbCr 2D DCT temporal embeddings (5 x 768 = 3,840-D = 7,680 bytes FP16)
  on NVIDIA RTX Tensor Cores (30/40/50 Series), with seamless CPU FP32 fallback on non-GPU systems.
"""

import os
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Callable
import numpy as np
import torch

import av
av.logging.set_level(av.logging.PANIC)

from dupeclean.core.models import FileRecord, format_size
from dupeclean.core.conversion_logger import log_conversion_error
from dupeclean.core.hardware import (
    choose_embedding_storage_device,
    get_compute_device,
    get_optimal_gpu_budgets,
)
from dupeclean.core.hasher_image import _get_dct_matrix
from dupeclean.core.cache import (
    CacheManager,
    _get_drive_volume_id,
    _open_random_binary,
    _select_cache_io_workers,
    compute_prefix_hash_from_stream,
)
from dupeclean.core.scanner import get_storage_kind, get_volume_capabilities
from dupeclean.core.neural_backbone import (
    compute_hybrid_visual_embeddings,
    get_neural_backbone,
)
from dupeclean.core._video_worker import (
    _AVAILABLE_CUVID_DECODERS,
    VIDEO_CHANNELS,
    VIDEO_DIM_SIZE,
    VIDEO_IMG_SIZE,
    VIDEO_LOW_FREQ_SIZE,
    VIDEO_NEURAL_SIZE,
    VIDEO_NUM_SAMPLES,
    _worker_decode_path,
    _worker_decode_path_cuvid_dct,
    _worker_decode_path_cuvid_neural,
    _worker_decode_path_neural,
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
    _, dur, arr, *_ = res
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
    Process videos using crash-isolated embedded FFmpeg (PyAV) decoding + Hybrid
    DINOv2-ViT-S/14 + YCbCr 2D DCT temporal embeddings (3,840-D = 7,680 bytes FP16)
    on RTX Tensor Cores (with universal CPU FP32 fallback) and SQLite WAL persistence.
    """
    device, dtype = get_compute_device()
    cache = cache_manager or CacheManager()

    total = len(records)
    total_bytes = sum(r.size for r in records)
    dim_size = VIDEO_DIM_SIZE
    if total == 0:
        return [], torch.empty((0, dim_size), device=device, dtype=dtype)

    # 1. Zero-I/O Stage-2 L1 lookup + lightweight RAM index preload (L2/L3 execute inline in Stage 3)
    if progress_callback:
        progress_callback(f"Checking video cache ({total:,} videos, {format_size(total_bytes)})...", 0, total)

    cached_map, l2_index, l3_index, l3_known_sizes, l2_known_volumes = cache.get_video_cache_l1_and_indexes(
        records,
        expected_bytes=dim_size * 2,
        progress_callback=progress_callback,
        stop_check=stop_check,
    )
    if stop_check and stop_check():
        return [], torch.empty((0, dim_size), device=device, dtype=dtype)
    cached_count = len(cached_map)

    store_device = choose_embedding_storage_device(total, dim_size, device, dtype)
    store_dtype = torch.float16 if (store_device == "cpu" and device == "cuda") else dtype

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
        cached_map.clear()
        with torch.inference_mode():
            embeddings_list.append(
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
            f"Loading cached videos of {cached_count:,}/{total:,} files ({format_size(cached_bytes)} / {format_size(total_bytes)})...",
            total,
            total,
        )

    # 2. If all videos were already L1-cached, return immediately
    if not to_process or (stop_check and stop_check()):
        if not embeddings_list:
            return [], torch.empty((0, dim_size), device=device, dtype=dtype)
        with torch.inference_mode():
            all_embeddings = torch.cat(embeddings_list, dim=0)
        return valid_records, all_embeddings

    # 3. Unified Stage 3: sort fastest drives FIRST (SSD -> iSCSI/network -> HDD) + directory locality
    _STORAGE_PRIORITY = {"ssd": 0, "unknown": 1, "iscsi": 2, "network": 2, "hdd": 3}
    _vid_drive_prio: Dict[str, int] = {}
    for r in to_process:
        drv = os.path.splitdrive(str(r.path))[0].upper()
        if drv not in _vid_drive_prio:
            _vid_drive_prio[drv] = _STORAGE_PRIORITY.get(get_storage_kind(r.path), 1)
    to_process.sort(
        key=lambda r: (
            _vid_drive_prio.get(os.path.splitdrive(str(r.path))[0].upper(), 1),
            str(r.path),
        )
    )
    uncached_total = len(to_process)

    if progress_callback:
        progress_callback(
            f"Processing videos of {cached_count:,}/{total:,} files ({format_size(cached_bytes)} / {format_size(total_bytes)})...",
            0,
            uncached_total,
        )

    cache_writer = cache.create_batch_writer(
        "videos",
        batch_size=512,
        max_queue=50000,
        name="AsyncVideoCacheWriter",
    )

    processed_count = 0
    processed_bytes = cached_bytes
    last_progress_time = 0.0
    stale_src_paths: Set[str] = set()
    active_paths: Set[str] = {str(r.path) for r in cached_recs}

    def _emit_video_progress(force: bool = False) -> None:
        nonlocal last_progress_time
        now = time.time()
        if progress_callback and (force or now - last_progress_time >= 0.08 or processed_count == uncached_total):
            last_progress_time = now
            current_total_done = cached_count + processed_count
            progress_callback(
                f"Processing videos of {current_total_done:,}/{total:,} files ({format_size(processed_bytes)} / {format_size(total_bytes)})...",
                processed_count,
                uncached_total,
            )

    # 3A. Fast inline L2 (fstat) + L3 (8 KB) + intra-scan clone grouping for candidate videos only
    size_counts: Dict[int, int] = {}
    for r in to_process:
        size_counts[r.size] = size_counts.get(r.size, 0) + 1
    dup_sizes: Set[int] = {sz for sz, cnt in size_counts.items() if cnt >= 2}
    l3_check_sizes: Set[int] = l3_known_sizes | dup_sizes

    drive_supports_fid: Dict[str, bool] = {}
    drive_has_l2: Dict[str, bool] = {}
    drive_vol_cache: Dict[str, int] = {}
    has_l2_index = bool(l2_index)
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

    def _inline_probe_video(rec: FileRecord) -> Tuple[FileRecord, Optional[Tuple[float, bytes, Optional[str]]]]:
        """Single-open inline L2 (0 bytes) + L3 (<=8 KB) probe for video cache/clone resolution."""
        drv = os.path.splitdrive(str(rec.path))[0].upper()
        supports_fid = drive_supports_fid.get(drv, False)
        has_l2 = drive_has_l2.get(drv, False)
        check_l3 = rec.size in l3_check_sizes
        if not has_l2 and not check_l3:
            return rec, None
        try:
            with _open_random_binary(rec.path, rec.size) as f:
                if supports_fid and (rec.volume_id == 0 or rec.file_id == 0):
                    st = os.fstat(f.fileno())
                    rec.volume_id = getattr(st, "st_dev", 0) or 0
                    rec.file_id = getattr(st, "st_ino", 0) or 0

                # Inline L2 hit (0 bytes read from video body)
                if has_l2 and rec.volume_id > 0 and rec.file_id > 0:
                    l2_hit = l2_index.get((rec.volume_id, rec.file_id))
                    if (
                        l2_hit is not None
                        and rec.size == l2_hit[1]
                        and abs(rec.mtime - l2_hit[2]) < 0.001
                    ):
                        c_path, _, _, dur, emb_blob, c_prefix = l2_hit
                        if c_prefix:
                            rec.prefix_hash = c_prefix
                        return rec, (dur, emb_blob, c_path)

                # Inline L3 probe (<= 8 KB Head + Tail)
                if check_l3:
                    if rec.prefix_hash is None:
                        rec.prefix_hash = compute_prefix_hash_from_stream(f, rec.size)
                    if rec.prefix_hash:
                        candidates_row = l3_index.get((rec.size, rec.prefix_hash))
                        if candidates_row:
                            is_dav = rec.ext == ".dav" or str(rec.path).lower().endswith(".dav")
                            for r_path, r_mtime, dur, emb_blob in candidates_row:
                                if is_dav and abs(rec.mtime - r_mtime) > 2.0:
                                    continue
                                return rec, (dur, emb_blob, r_path)
        except (OSError, PermissionError):
            pass
        return rec, None

    probe_candidates = [
        r for r in to_process
        if drive_has_l2.get(os.path.splitdrive(str(r.path))[0].upper(), False) or r.size in l3_check_sizes
    ]
    direct_decode_recs = [
        r for r in to_process
        if not (drive_has_l2.get(os.path.splitdrive(str(r.path))[0].upper(), False) or r.size in l3_check_sizes)
    ]

    inline_hit_recs: List[FileRecord] = []
    inline_hit_blobs: List[bytes] = []
    inline_upserts: List[Tuple] = []
    decode_recs: List[FileRecord] = list(direct_decode_recs)
    intra_clones: Dict[Tuple[int, str], List[FileRecord]] = defaultdict(list)
    seen_intra_reps: Set[Tuple[int, str]] = set()

    if probe_candidates and not (stop_check and stop_check()):
        probe_workers = _select_cache_io_workers(probe_candidates)

        def _consume_probe(rec: FileRecord, hit: Optional[Tuple[float, bytes, Optional[str]]]) -> None:
            nonlocal processed_count, processed_bytes
            p_str = str(rec.path)
            active_paths.add(p_str)
            if hit is not None:
                dur, emb_blob, src_path = hit
                processed_count += 1
                processed_bytes += rec.size
                if dur != -1.0 and len(emb_blob) > 0:
                    rec.duration = dur
                    inline_hit_recs.append(rec)
                    inline_hit_blobs.append(emb_blob)
                inline_upserts.append(
                    (p_str, rec.size, rec.mtime, dur, emb_blob, rec.volume_id, rec.file_id, rec.prefix_hash)
                )
                if src_path and src_path != p_str:
                    stale_src_paths.add(src_path)
                if len(inline_upserts) >= 512:
                    cache_writer.add_batch(inline_upserts)
                    inline_upserts.clear()
                _emit_video_progress()
            else:
                # Intra-scan identical copy collapse (skip .dav due to pre-allocated empty headers)
                is_dav = rec.ext == ".dav" or p_str.lower().endswith(".dav")
                if not is_dav and rec.size in dup_sizes and rec.prefix_hash:
                    key = (rec.size, rec.prefix_hash)
                    if key in seen_intra_reps:
                        intra_clones[key].append(rec)
                        return
                    seen_intra_reps.add(key)
                decode_recs.append(rec)

        if len(probe_candidates) <= 16:
            for r in probe_candidates:
                if stop_check and stop_check():
                    break
                _consume_probe(*_inline_probe_video(r))
        else:
            with ThreadPoolExecutor(max_workers=probe_workers, thread_name_prefix="VideoProbe") as p_pool:
                for rec_out, hit_out in p_pool.map(_inline_probe_video, probe_candidates):
                    if stop_check and stop_check():
                        break
                    _consume_probe(rec_out, hit_out)

    if inline_upserts:
        cache_writer.add_batch(inline_upserts)
        inline_upserts.clear()

    if inline_hit_recs:
        with torch.inference_mode():
            hit_mat = np.empty((len(inline_hit_recs), dim_size), dtype=np.float16)
            for h_i, blob in enumerate(inline_hit_blobs):
                hit_mat[h_i] = np.frombuffer(blob, dtype=np.float16)
            embeddings_list.append(
                torch.from_numpy(hit_mat).to(
                    device=store_device,
                    dtype=store_dtype,
                    non_blocking=(store_device == "cuda"),
                )
            )
            valid_records.extend(inline_hit_recs)
        inline_hit_recs.clear()
        inline_hit_blobs.clear()

    # If every video resolved via inline L2/L3 cache hits, finish immediately without loading backbone/pools!
    if not decode_recs or (stop_check and stop_check()):
        cache_writer.close()
        if stale_src_paths and not (stop_check and stop_check()):
            try:
                cache.cleanup_stale_paths("cache_videos", stale_src_paths, active_paths)
            except Exception:
                pass
        _emit_video_progress(force=True)
        if not embeddings_list:
            return [], torch.empty((0, dim_size), device=device, dtype=dtype)
        with torch.inference_mode():
            all_embeddings = torch.cat(embeddings_list, dim=0)
        return valid_records, all_embeddings

    # 3B. Prepare Hybrid DINOv2 + YCbCr DCT transforms (with pure-DCT fallback if weights absent)
    backbone_bundle = get_neural_backbone(device=device, dtype=dtype)
    use_neural = backbone_bundle is not None
    frame_size = VIDEO_NEURAL_SIZE if use_neural else VIDEO_IMG_SIZE

    dct_mat = _get_dct_matrix(VIDEO_IMG_SIZE, device=device, dtype=torch.float32)
    dct_mat_t = dct_mat.t()

    u16 = torch.arange(VIDEO_LOW_FREQ_SIZE, device=device, dtype=torch.float32).unsqueeze(1)
    v16 = torch.arange(VIDEO_LOW_FREQ_SIZE, device=device, dtype=torch.float32).unsqueeze(0)
    sw16 = (u16 + v16 + 1.0).pow(VIDEO_ALPHA)
    sw16[0, 0] = 0.0  # Zero DC
    u8 = torch.arange(8, device=device, dtype=torch.float32).unsqueeze(1)
    v8 = torch.arange(8, device=device, dtype=torch.float32).unsqueeze(0)
    sw8 = (u8 + v8 + 1.0).pow(VIDEO_ALPHA)
    sw8[0, 0] = 0.0

    budgets = get_optimal_gpu_budgets()
    neural_frame_cap = int(budgets.get("neural_batch_size", 64 if device == "cuda" else 32))
    # Cap video batch size so B * 5 frames fits comfortably in VRAM even on 8 GB RTX GPUs
    video_batch_limit = max(4, neural_frame_cap // VIDEO_NUM_SAMPLES) if use_neural else 64

    path_to_rec = {str(r.path): r for r in decode_recs}

    batch_cpu_records: List[FileRecord] = []
    batch_cpu_frames: List[np.ndarray] = []
    batch_cpu_durations: List[float] = []

    def _compute_and_store_batch(
        t_b_5_3_hw: torch.Tensor,
        recs_batch: List[FileRecord],
        durs_batch: List[float],
    ) -> None:
        """Compute Hybrid 3,840-D temporal video embeddings (5 x 768-D), quantize to FP16, fan out clones, and persist."""
        nonlocal processed_count, processed_bytes
        b_size = t_b_5_3_hw.shape[0]
        if use_neural and backbone_bundle is not None:
            flat_frames = t_b_5_3_hw.reshape(b_size * VIDEO_NUM_SAMPLES, VIDEO_CHANNELS, frame_size, frame_size)
            frame_embs = compute_hybrid_visual_embeddings(
                t_rgb_224=flat_frames,
                dct64=dct_mat,
                dct64_t=dct_mat_t,
                sw16=sw16,
                sw8=sw8,
                backbone_bundle=backbone_bundle,
                out_dtype=torch.float32,
            )  # [B * 5, 768] FP32
            flat = frame_embs.reshape(b_size, VIDEO_DIM_SIZE)
            norms = torch.norm(flat, p=2, dim=1, keepdim=True).clamp(min=1e-6)
            normalized = (flat / norms).to(dtype=dtype)
        else:
            t_flat = t_b_5_3_hw.reshape(
                b_size * VIDEO_NUM_SAMPLES * VIDEO_CHANNELS, VIDEO_IMG_SIZE, VIDEO_IMG_SIZE
            )
            dct_batch = torch.matmul(dct_mat, torch.matmul(t_flat, dct_mat_t))
            low_freq = dct_batch[:, :VIDEO_LOW_FREQ_SIZE, :VIDEO_LOW_FREQ_SIZE] * sw16.unsqueeze(0)
            flat = low_freq.reshape(b_size, -1)  # [B, 3840] FP32
            norms = torch.norm(flat, p=2, dim=1, keepdim=True).clamp(min=1e-6)
            normalized = (flat / norms).to(dtype=dtype)

        norm_stored = normalized if store_device == device else normalized.to(device=store_device, dtype=store_dtype)
        embeddings_list.append(norm_stored)
        valid_records.extend(recs_batch)

        norm_cpu_np = normalized.to(dtype=torch.float16).cpu().numpy()
        entries = []
        clone_recs_batch: List[FileRecord] = []
        clone_rows_idx: List[int] = []

        for b_i, b_rec in enumerate(recs_batch):
            emb_bytes = norm_cpu_np[b_i:b_i + 1].tobytes()
            dur_val = durs_batch[b_i]
            entries.append((
                str(b_rec.path),
                b_rec.size,
                b_rec.mtime,
                dur_val,
                emb_bytes,
                b_rec.volume_id,
                b_rec.file_id,
                b_rec.prefix_hash,
            ))
            # Fan out exact embedding to intra-scan identical video copies (0 extra NVDEC/PyAV decodes!)
            if b_rec.prefix_hash:
                clones = intra_clones.pop((b_rec.size, b_rec.prefix_hash), None)
                if clones:
                    for cl in clones:
                        cl.duration = dur_val
                        clone_recs_batch.append(cl)
                        clone_rows_idx.append(b_i)
                        processed_count += 1
                        processed_bytes += cl.size
                        entries.append((
                            str(cl.path),
                            cl.size,
                            cl.mtime,
                            dur_val,
                            emb_bytes,
                            cl.volume_id,
                            cl.file_id,
                            cl.prefix_hash,
                        ))

        if clone_recs_batch:
            idx_t = torch.tensor(clone_rows_idx, dtype=torch.int64, device=norm_stored.device)
            embeddings_list.append(norm_stored[idx_t])
            valid_records.extend(clone_recs_batch)

        cache_writer.add_batch(entries)

    def _flush_cpu_frame_batch() -> None:
        if not batch_cpu_frames:
            return
        stacked = np.stack(batch_cpu_frames)  # [B, 5, H, W, 3] uint8
        t_cpu = torch.from_numpy(stacked)
        t_dev = t_cpu.pin_memory().to(device=device, non_blocking=True) if device == "cuda" else t_cpu
        t_batch = t_dev.permute(0, 1, 4, 2, 3).to(dtype=torch.float32).mul_(1.0 / 255.0)
        _compute_and_store_batch(t_batch, batch_cpu_records, batch_cpu_durations)
        batch_cpu_records.clear()
        batch_cpu_frames.clear()
        batch_cpu_durations.clear()

    expected_cpu_shape = (VIDEO_NUM_SAMPLES, frame_size, frame_size, VIDEO_CHANNELS)

    def _diagnose_failed_video(p_str: str) -> str:
        """Pure-Python OS check (never invokes native decoders in main process) for conversion.log."""
        try:
            st = os.stat(p_str)
            if st.st_size == 0:
                return "Empty 0-byte video file (tombstoned)"
            return "Corrupt container, unsupported codec, or no decodable video stream (tombstoned)"
        except OSError as exc:
            return f"{type(exc).__name__}: {exc}"

    def _handle_cpu_decoded_result(res, extra_log_note: Optional[str] = None) -> None:
        nonlocal processed_count, processed_bytes
        processed_count += 1

        if res is not None:
            if len(res) >= 7:
                p_str, duration, frames_arr, vol_id, f_id, p_hash, fallback_note = res[:7]
            else:
                p_str, duration, frames_arr, vol_id, f_id, p_hash = res
                fallback_note = None
            rec = path_to_rec.get(p_str)
            if rec is not None:
                processed_bytes += rec.size
                if vol_id and rec.volume_id == 0:
                    rec.volume_id = vol_id
                if f_id and rec.file_id == 0:
                    rec.file_id = f_id
                if p_hash and rec.prefix_hash is None:
                    rec.prefix_hash = p_hash
                if frames_arr is None or duration == -1.0 or getattr(frames_arr, "shape", None) != expected_cpu_shape:
                    tomb_entries = [(
                        str(rec.path),
                        rec.size,
                        rec.mtime,
                        -1.0,
                        b"",
                        rec.volume_id,
                        rec.file_id,
                        rec.prefix_hash,
                    )]
                    clones_count = 0
                    if rec.prefix_hash:
                        clones = intra_clones.pop((rec.size, rec.prefix_hash), None)
                        if clones:
                            clones_count = len(clones)
                            for cl in clones:
                                processed_count += 1
                                processed_bytes += cl.size
                                tomb_entries.append((
                                    str(cl.path),
                                    cl.size,
                                    cl.mtime,
                                    -1.0,
                                    b"",
                                    cl.volume_id,
                                    cl.file_id,
                                    cl.prefix_hash,
                                ))
                    cache_writer.add_batch(tomb_entries)
                    reason = _diagnose_failed_video(str(rec.path))
                    if clones_count:
                        reason = f"{reason} (+{clones_count} identical clone(s) skipped)"
                    log_conversion_error("VIDEOS", "DECODE", rec.path, reason)
                else:
                    note = fallback_note or extra_log_note
                    if note:
                        log_conversion_error("VIDEOS", "CUVID_FALLBACK", rec.path, note)
                    rec.duration = duration
                    batch_cpu_records.append(rec)
                    batch_cpu_frames.append(frames_arr)
                    batch_cpu_durations.append(duration)

        _emit_video_progress()
        if len(batch_cpu_records) >= video_batch_limit or processed_count >= uncached_total:
            _flush_cpu_frame_batch()

    # 4. Crash-isolated embedded FFmpeg 8 (CUVID on RTX + multi-thread PyAV CPU fallback) inside ProcessPoolExecutor
    cpu_paths: List[str] = [str(r.path) for r in decode_recs]

    def _tombstone_poison_path(p_str: str, reason: str = "Native decoder crash in isolated PyAV worker (tombstoned)") -> None:
        """Persist permanent tombstone for a verified crash-inducing poison video and log to conversion.log."""
        nonlocal processed_count, processed_bytes
        rec = path_to_rec.get(p_str)
        processed_count += 1
        if rec is None:
            log_conversion_error("VIDEOS", "PYAV_CRASH", p_str, reason)
            return
        processed_bytes += rec.size
        tomb_entries = [(
            str(rec.path),
            rec.size,
            rec.mtime,
            -1.0,
            b"",
            rec.volume_id,
            rec.file_id,
            rec.prefix_hash,
        )]
        clones_count = 0
        if rec.prefix_hash:
            clones = intra_clones.pop((rec.size, rec.prefix_hash), None)
            if clones:
                clones_count = len(clones)
                for cl in clones:
                    processed_count += 1
                    processed_bytes += cl.size
                    tomb_entries.append((
                        str(cl.path),
                        cl.size,
                        cl.mtime,
                        -1.0,
                        b"",
                        cl.volume_id,
                        cl.file_id,
                        cl.prefix_hash,
                    ))
        cache_writer.add_batch(tomb_entries)
        full_reason = f"{reason} (+{clones_count} identical clone(s) skipped)" if clones_count else reason
        log_conversion_error("VIDEOS", "PYAV_CRASH", rec.path, full_reason)
        _emit_video_progress(force=True)

    cuvid_budget = int(budgets.get("cuvid_workers", 0))
    use_cuvid_pool = (device == "cuda") and (cuvid_budget > 0) and bool(_AVAILABLE_CUVID_DECODERS)

    # Always keep a pure-CPU worker for 1-by-1 crash quarantine rescue so CUVID crashes still get decoded via CPU!
    cpu_worker_fn = _worker_decode_path_neural if use_neural else _worker_decode_path
    primary_worker_fn = (
        (_worker_decode_path_cuvid_neural if use_neural else _worker_decode_path_cuvid_dct)
        if use_cuvid_pool
        else cpu_worker_fn
    )
    _native_workers = (
        _worker_decode_path,
        _worker_decode_path_neural,
        _worker_decode_path_cuvid_neural,
        _worker_decode_path_cuvid_dct,
    )

    def _build_worker_arg(p_str: str, fn) -> object:
        if fn not in _native_workers:
            return p_str
        rec = path_to_rec.get(p_str)
        if rec is None:
            return p_str
        drv = os.path.splitdrive(p_str)[0].upper()
        need_meta = (rec.prefix_hash is None) or (
            drive_supports_fid.get(drv, False) and (rec.volume_id == 0 or rec.file_id == 0)
        )
        return p_str if need_meta else (p_str, False)

    executor: Optional[ProcessPoolExecutor] = None
    cancelled = False

    try:
        with torch.inference_mode():
            # 100% Crash-isolated FFmpeg 8 pipeline with VRAM-adaptive CUVID, RAM recycling, and CPU quarantine rescue
            if cpu_paths and not cancelled and not (stop_check and stop_check()):
                cpu_total = len(cpu_paths)
                cpu_cnt = os.cpu_count() or 4
                if use_cuvid_pool:
                    num_workers = min(cuvid_budget, max(1, min(cpu_total, max(4, cpu_cnt))))
                else:
                    num_workers = min(12, max(1, min(cpu_total, max(2, cpu_cnt // 2))))
                max_in_flight = num_workers * 3
                idx_cursor = 0
                quarantine_paths: List[str] = []
                cuvid_rescued_paths: Set[str] = set()
                pyav_recycle_per_worker = int(budgets.get("pyav_recycle_per_worker", 200))
                cpu_recycle_batch = (
                    max(num_workers * pyav_recycle_per_worker, num_workers)
                    if pyav_recycle_per_worker > 0
                    else 0
                )
                cpu_submitted_in_cycle = 0

                executor = ProcessPoolExecutor(max_workers=num_workers)
                in_flight = {}

                def _refill() -> bool:
                    nonlocal idx_cursor, cpu_submitted_in_cycle
                    while idx_cursor < cpu_total and len(in_flight) < max_in_flight:
                        if cpu_recycle_batch > 0 and cpu_submitted_in_cycle >= cpu_recycle_batch:
                            break
                        p_next = cpu_paths[idx_cursor]
                        try:
                            fut_c = executor.submit(primary_worker_fn, _build_worker_arg(p_next, primary_worker_fn))
                        except BrokenProcessPool:
                            return False
                        idx_cursor += 1
                        cpu_submitted_in_cycle += 1
                        in_flight[fut_c] = p_next
                    return True

                if not _refill():
                    quarantine_paths.extend(cpu_paths[idx_cursor:])
                    if use_cuvid_pool:
                        cuvid_rescued_paths.update(cpu_paths[idx_cursor:])
                    idx_cursor = cpu_total

                while in_flight:
                    if stop_check and stop_check():
                        cancelled = True
                        break
                    done_futs, _ = wait(in_flight.keys(), timeout=0.05, return_when=FIRST_COMPLETED)
                    pool_broken = False
                    crashed_batch: List[str] = []
                    for fut in done_futs:
                        p_src = in_flight.pop(fut)
                        try:
                            res = fut.result()
                        except BrokenProcessPool:
                            pool_broken = True
                            crashed_batch.append(p_src)
                            break
                        except Exception as exc:
                            if use_cuvid_pool:
                                quarantine_paths.append(p_src)
                                cuvid_rescued_paths.add(p_src)
                            else:
                                _tombstone_poison_path(p_src, f"Worker exception ({type(exc).__name__}: {exc})")
                            continue
                        _handle_cpu_decoded_result(res)

                    if not pool_broken and not (stop_check and stop_check()):
                        if not _refill():
                            pool_broken = True

                    if pool_broken:
                        crashed_batch.extend(in_flight.values())
                        in_flight.clear()
                        _terminate_pool_processes(executor)
                        executor.shutdown(wait=False, cancel_futures=True)
                        executor = None
                        _flush_cpu_frame_batch()
                        if not use_cuvid_pool and len(crashed_batch) == 1:
                            _tombstone_poison_path(crashed_batch[0])
                        elif crashed_batch:
                            # Rescue via isolated 1-by-1 pure-CPU worker so CUVID crashes still match in Stage 4!
                            quarantine_paths.extend(crashed_batch)
                            if use_cuvid_pool:
                                cuvid_rescued_paths.update(crashed_batch)
                        if idx_cursor < cpu_total and not (stop_check and stop_check()):
                            executor = ProcessPoolExecutor(max_workers=num_workers)
                            cpu_submitted_in_cycle = 0
                            if not _refill():
                                quarantine_paths.extend(cpu_paths[idx_cursor:])
                                if use_cuvid_pool:
                                    cuvid_rescued_paths.update(cpu_paths[idx_cursor:])
                                idx_cursor = cpu_total
                                break
                        else:
                            break
                    elif not in_flight and idx_cursor < cpu_total and not (stop_check and stop_check()):
                        # Proactive RAM-aware worker pool recycle to prevent C-heap / FFmpeg arena fragmentation
                        executor.shutdown(wait=True, cancel_futures=True)
                        executor = None
                        _flush_cpu_frame_batch()
                        executor = ProcessPoolExecutor(max_workers=num_workers)
                        cpu_submitted_in_cycle = 0
                        if not _refill():
                            quarantine_paths.extend(cpu_paths[idx_cursor:])
                            if use_cuvid_pool:
                                cuvid_rescued_paths.update(cpu_paths[idx_cursor:])
                            idx_cursor = cpu_total
                            break

                if executor is not None:
                    if cancelled:
                        _terminate_pool_processes(executor)
                        executor.shutdown(wait=False, cancel_futures=True)
                    else:
                        executor.shutdown(wait=True, cancel_futures=True)
                    executor = None

                # Drain quarantined files 1-by-1 in an isolated pure-CPU single-worker process
                if quarantine_paths and not cancelled and not (stop_check and stop_check()):
                    q_exec: Optional[ProcessPoolExecutor] = ProcessPoolExecutor(max_workers=1)
                    try:
                        for q_path in quarantine_paths:
                            if stop_check and stop_check():
                                cancelled = True
                                break
                            if q_exec is None:
                                q_exec = ProcessPoolExecutor(max_workers=1)
                            try:
                                fut_q = q_exec.submit(cpu_worker_fn, _build_worker_arg(q_path, cpu_worker_fn))
                                res_q = fut_q.result()
                                rescue_note = (
                                    "CUVID worker crashed natively; rescued via isolated CPU"
                                    if q_path in cuvid_rescued_paths
                                    else None
                                )
                                _handle_cpu_decoded_result(res_q, extra_log_note=rescue_note)
                            except BrokenProcessPool:
                                _terminate_pool_processes(q_exec)
                                q_exec.shutdown(wait=False, cancel_futures=True)
                                q_exec = None
                                _tombstone_poison_path(q_path)
                            except Exception as exc:
                                _tombstone_poison_path(q_path, f"Isolated PyAV worker exception ({type(exc).__name__}: {exc})")
                    finally:
                        if q_exec is not None:
                            if cancelled:
                                _terminate_pool_processes(q_exec)
                                q_exec.shutdown(wait=False, cancel_futures=True)
                            else:
                                q_exec.shutdown(wait=True, cancel_futures=True)

            if not cancelled:
                _flush_cpu_frame_batch()
                _emit_video_progress(force=True)

    finally:
        if executor is not None:
            if cancelled:
                _terminate_pool_processes(executor)
                executor.shutdown(wait=False, cancel_futures=True)
            else:
                executor.shutdown(wait=True, cancel_futures=True)
        cache_writer.close()

    if stale_src_paths and not cancelled and not (stop_check and stop_check()):
        try:
            cache.cleanup_stale_paths("cache_videos", stale_src_paths, active_paths)
        except Exception:
            pass

    if not embeddings_list:
        return [], torch.empty((0, dim_size), device=device, dtype=dtype)

    with torch.inference_mode():
        all_embeddings = torch.cat(embeddings_list, dim=0)
    return valid_records, all_embeddings
