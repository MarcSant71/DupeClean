"""
Lightweight, PyTorch-free worker module for multiprocessing video decoding.
Keeping this module free of torch/CUDA imports prevents spawned Windows worker
processes from loading gigabytes of CUDA DLLs and stalling for 10+ seconds.
"""

import os
from pathlib import Path
from typing import Optional, Tuple

import av
import numpy as np
from PIL import Image

from dupeclean.core.cache import compute_prefix_hash
from dupeclean.core.scanner import get_volume_capabilities

av.logging.set_level(av.logging.PANIC)

# Frame dimensions and frequency count for temporal video signatures
VIDEO_IMG_SIZE = 64
VIDEO_LOW_FREQ_SIZE = 16
VIDEO_CHANNELS = 3
VIDEO_NUM_SAMPLES = 5
VIDEO_DIM_SIZE = (
    VIDEO_LOW_FREQ_SIZE * VIDEO_LOW_FREQ_SIZE * VIDEO_CHANNELS * VIDEO_NUM_SAMPLES
)  # 3840 floats = 7680 bytes (FP16)


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
            container.close()
            return path_str, -1.0, None, vol_id, f_id, p_hash

        stream = container.streams.video[0]
        dur = float(container.duration) / 1000000.0 if container.duration else None
        if dur is None and stream.duration and stream.time_base:
            dur = float(stream.duration * stream.time_base)
        dur = dur or 10.0

        frames = []

        # 1. Strategy A: Fast Keyframe Seek (for indexed formats: MP4, MKV, AVI, MOV, WEBM, etc.)
        if not is_dav:
            percentages = [0.10, 0.30, 0.50, 0.70, 0.90]
            try:
                for pct in percentages:
                    target_sec = dur * pct
                    target_pts = int(target_sec / stream.time_base) if stream.time_base else int(target_sec * 1000000)
                    container.seek(target_pts, any_frame=False, backward=True, stream=stream)
                    for pkt in container.demux():
                        if pkt.stream == stream and pkt.is_keyframe and pkt.size > 0:
                            decoded_list = pkt.decode()
                            if decoded_list:
                                f = decoded_list[0]
                                img = f.to_image().convert("RGB").resize((VIDEO_IMG_SIZE, VIDEO_IMG_SIZE), Image.Resampling.BILINEAR)
                                frames.append(np.asarray(img, dtype=np.uint8))
                                break
            except Exception:
                frames.clear()

            if len(frames) < 5:
                try:
                    container.seek(0)
                    for f in container.decode(video=0):
                        img = f.to_image().convert("RGB").resize((VIDEO_IMG_SIZE, VIDEO_IMG_SIZE), Image.Resampling.BILINEAR)
                        frames.append(np.asarray(img, dtype=np.uint8))
                        if len(frames) >= 5:
                            break
                except Exception:
                    pass

        # 2. Strategy B: Temporal Extraction of 5 Distinct Keyframes (for .dav and continuous streams)
        else:
            kf_packets = []
            for pkt in container.demux():
                if pkt.stream == stream and pkt.is_keyframe and pkt.size > 0:
                    kf_packets.append(pkt)

            num_kf = len(kf_packets)
            if num_kf > 0:
                step = max(1.0, num_kf / 5.0)
                chosen_indices = [min(int(i * step), num_kf - 1) for i in range(5)]
                for idx in chosen_indices:
                    pkt = kf_packets[idx]
                    decoded_list = pkt.decode()
                    if decoded_list:
                        f = decoded_list[0]
                        img = f.to_image().convert("RGB").resize((VIDEO_IMG_SIZE, VIDEO_IMG_SIZE), Image.Resampling.BILINEAR)
                        frames.append(np.asarray(img, dtype=np.uint8))

        container.close()

        if not frames:
            return path_str, -1.0, None, vol_id, f_id, p_hash

        while len(frames) < 5:
            frames.append(frames[-1])

        expected_shape = (VIDEO_NUM_SAMPLES, VIDEO_IMG_SIZE, VIDEO_IMG_SIZE, VIDEO_CHANNELS)
        arr = np.array(frames[:5], dtype=np.uint8)  # [5, 64, 64, 3]
        if arr.shape != expected_shape:
            return path_str, -1.0, None, vol_id, f_id, p_hash

        return path_str, dur, arr, vol_id, f_id, p_hash

    except Exception:
        try:
            container.close()
        except Exception:
            pass
        return path_str, -1.0, None, vol_id, f_id, p_hash
