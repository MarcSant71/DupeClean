"""
Lightweight, PyTorch-free worker module for multiprocessing video decoding.
Keeping this module free of torch/CUDA imports prevents spawned Windows worker
processes from loading gigabytes of CUDA DLLs and stalling for 10+ seconds.
"""

import os
from pathlib import Path
from typing import Optional, Sequence, Tuple

import av
import numpy as np
from PIL import Image

from dupeclean.core.cache import compute_prefix_hash
from dupeclean.core.scanner import get_volume_capabilities

av.logging.set_level(av.logging.PANIC)

# Frame dimensions and frequency count for temporal video signatures
_SAMPLE_DETECT_SIZE = 128
VIDEO_IMG_SIZE = 64
VIDEO_LOW_FREQ_SIZE = 16
VIDEO_CHANNELS = 3
VIDEO_NUM_SAMPLES = 5
VIDEO_DIM_SIZE = (
    VIDEO_LOW_FREQ_SIZE * VIDEO_LOW_FREQ_SIZE * VIDEO_CHANNELS * VIDEO_NUM_SAMPLES
)  # 3840 floats = 7680 bytes (FP16)


def crop_letterbox(frames: Sequence[np.ndarray], threshold: int = 22) -> np.ndarray:
    """
    Detect and crop static black bars (letterbox / pillarbox) across all sampled frames.
    Uses the union (max projection) across frames so dark individual scenes are not over-cropped,
    then normalizes all frames to (VIDEO_IMG_SIZE, VIDEO_IMG_SIZE).
    """
    if not frames:
        return np.empty((0, VIDEO_IMG_SIZE, VIDEO_IMG_SIZE, VIDEO_CHANNELS), dtype=np.uint8)

    stack = np.stack([np.asarray(f, dtype=np.uint8) for f in frames])
    h, w = stack.shape[1], stack.shape[2]
    max_frame = stack.max(axis=0)
    row_means = max_frame.mean(axis=(1, 2))
    col_means = max_frame.mean(axis=(0, 2))

    min_rows = max(16, h // 4)
    min_cols = max(16, w // 4)
    valid_rows = np.where(row_means > threshold)[0]
    valid_cols = np.where(col_means > threshold)[0]

    top, bottom = (int(valid_rows[0]), int(valid_rows[-1]) + 1) if len(valid_rows) >= min_rows else (0, h)
    left, right = (int(valid_cols[0]), int(valid_cols[-1]) + 1) if len(valid_cols) >= min_cols else (0, w)

    has_crop = top > 0 or bottom < h or left > 0 or right < w
    out = []
    for arr in stack:
        region = arr[top:bottom, left:right] if has_crop else arr
        if region.shape[0] != VIDEO_IMG_SIZE or region.shape[1] != VIDEO_IMG_SIZE:
            pil_img = Image.fromarray(region).resize(
                (VIDEO_IMG_SIZE, VIDEO_IMG_SIZE), Image.Resampling.BILINEAR
            )
            out.append(np.asarray(pil_img, dtype=np.uint8))
        else:
            out.append(region)
    return np.array(out, dtype=np.uint8)


def _frame_to_sample_array(frame: av.VideoFrame) -> np.ndarray:
    """Convert a decoded PyAV frame into a compact RGB numpy array for static-bar analysis."""
    img = frame.to_image().convert("RGB").resize(
        (_SAMPLE_DETECT_SIZE, _SAMPLE_DETECT_SIZE), Image.Resampling.BILINEAR
    )
    return np.asarray(img, dtype=np.uint8)


def _decode_frame_near_timestamp(
    container,
    stream,
    target_sec: float,
    tb: float,
    max_decode_frames: int = 600,
) -> Optional[np.ndarray]:
    """
    Seek backward to the nearest keyframe before target_sec and decode forward
    to the frame closest to target_sec (resilient to sparse AVI GOPs and B-frame delay).
    """
    target_pts = max(0, int(target_sec / tb))
    container.seek(target_pts, any_frame=False, backward=True, stream=stream)

    best_frame = None
    best_diff = float("inf")
    for idx_f, f in enumerate(container.decode(stream)):
        if f.pts is not None:
            f_sec = float(f.pts * tb)
            diff = abs(f_sec - target_sec)
            if diff < best_diff:
                best_diff = diff
                best_frame = f
            if f_sec >= target_sec or idx_f >= max_decode_frames:
                break
        else:
            best_frame = f
            if idx_f == 0:
                break

    if best_frame is None:
        return None
    return _frame_to_sample_array(best_frame)


def _worker_decode_path(path_str: str) -> Tuple[str, float, Optional[np.ndarray], int, int, Optional[str]]:
    """
    Process-isolated worker: opens the video, collects NTFS metadata (vol_id, file_id) and prefix_hash,
    extracts 5 clean temporal RGB frames, and returns raw data outside the main process GIL.
    Returns (path_str, -1.0, None, vol_id, file_id, prefix_hash) on failure for tombstone persistence.
    """
    p = Path(path_str)
    vol_id, f_id, p_hash = 0, 0, None
    try:
        st = os.stat(path_str, follow_symlinks=False)
        _, supports_fid = get_volume_capabilities(p)
        vol_id = getattr(st, "st_dev", 0) or 0
        f_id = (getattr(st, "st_ino", 0) or 0) if supports_fid else 0
        if st.st_size > 0:
            p_hash = compute_prefix_hash(p, st.st_size)
    except Exception:
        pass

    is_dav = p.suffix.lower() == ".dav"
    options = {"probesize": "65536", "analyzeduration": "0"} if is_dav else None

    try:
        container = av.open(path_str, options=options)
    except Exception:
        return path_str, -1.0, None, vol_id, f_id, p_hash

    try:
        if not container.streams.video:
            return path_str, -1.0, None, vol_id, f_id, p_hash

        stream = container.streams.video[0]
        dur = float(container.duration) / 1000000.0 if container.duration else None
        if (dur is None or dur <= 0) and stream.duration and stream.time_base:
            dur = float(stream.duration * stream.time_base)
        dur = dur if (dur and dur > 0) else 10.0

        tb = float(stream.time_base) if stream.time_base else (1.0 / 1000000.0)
        start_sec = float(stream.start_time * tb) if stream.start_time is not None else 0.0

        frames = []

        # 1. Strategy A: Timestamp-Accurate Seek + Forward Decode (MP4, MKV, AVI, MOV, WEBM, etc.)
        if not is_dav:
            percentages = (0.10, 0.30, 0.50, 0.70, 0.90)
            try:
                for pct in percentages:
                    target_sec = start_sec + dur * pct
                    arr_f = _decode_frame_near_timestamp(container, stream, target_sec, tb)
                    if arr_f is None and target_sec > start_sec:
                        # Retry slightly earlier if tail seek hit EOF in a truncated index
                        fallback_sec = max(start_sec, target_sec - max(2.0, dur * 0.08))
                        arr_f = _decode_frame_near_timestamp(container, stream, fallback_sec, tb)
                    if arr_f is not None:
                        frames.append(arr_f)
            except Exception:
                frames.clear()

            if not frames:
                try:
                    container.seek(0)
                    for f in container.decode(stream):
                        frames.append(_frame_to_sample_array(f))
                        if len(frames) >= 5:
                            break
                except Exception:
                    pass

        # 2. Strategy B: Temporal Extraction of 5 Distinct Keyframes (for .dav and continuous streams)
        else:
            # Thin packet retention dynamically so multi-GB .dav files never accumulate GBs of packets in RAM
            kf_packets = []
            stride = 1
            seen_kf = 0
            for pkt in container.demux(stream):
                if pkt.is_keyframe and pkt.size > 0:
                    if seen_kf % stride == 0:
                        kf_packets.append(pkt)
                        if len(kf_packets) > 512:
                            kf_packets = kf_packets[::2]
                            stride *= 2
                    seen_kf += 1

            num_kf = len(kf_packets)
            if num_kf > 0:
                step = max(1.0, num_kf / 5.0)
                chosen_indices = [min(int(i * step), num_kf - 1) for i in range(5)]
                for idx in chosen_indices:
                    pkt = kf_packets[idx]
                    decoded_list = pkt.decode()
                    if decoded_list:
                        frames.append(_frame_to_sample_array(decoded_list[0]))

        if not frames:
            return path_str, -1.0, None, vol_id, f_id, p_hash

        while len(frames) < 5:
            frames.append(frames[-1])

        expected_shape = (VIDEO_NUM_SAMPLES, VIDEO_IMG_SIZE, VIDEO_IMG_SIZE, VIDEO_CHANNELS)
        arr = crop_letterbox(frames[:5])  # [5, 64, 64, 3]
        if arr.shape != expected_shape:
            return path_str, -1.0, None, vol_id, f_id, p_hash

        return path_str, dur, arr, vol_id, f_id, p_hash

    except Exception:
        return path_str, -1.0, None, vol_id, f_id, p_hash
    finally:
        try:
            container.close()
        except Exception:
            pass
