"""
Lightweight, PyTorch-free worker module for multiprocessing/threaded video decoding.
Keeping this module free of torch/CUDA imports prevents spawned Windows worker
processes from loading gigabytes of CUDA DLLs and stalling for 10+ seconds.
"""

import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple, Union

import av
import numpy as np
from PIL import Image

from dupeclean.core.cache import _open_random_binary, compute_prefix_hash_from_stream
from dupeclean.core.scanner import get_volume_capabilities

av.logging.set_level(av.logging.PANIC)

# Map heavy modern video codecs to embedded FFmpeg 8 NVIDIA CUVID hardware decoders.
# Lightweight legacy codecs (mpeg4/XviD AVI, mpeg1/2, mjpeg) decode faster on multi-thread CPU
# without paying per-file CUDA init overhead or AVI packed-B-frame timestamp quirks.
_CUVID_CODEC_MAP: Dict[str, str] = {
    "h264": "h264_cuvid",
    "hevc": "hevc_cuvid",
    "av1": "av1_cuvid",
    "vp9": "vp9_cuvid",
    "vp8": "vp8_cuvid",
    "vc1": "vc1_cuvid",
}
_AVAILABLE_CUVID_DECODERS = {
    name for name in _CUVID_CODEC_MAP.values() if name in av.codecs_available
}

# Frame dimensions and frequency count for temporal video signatures
_SAMPLE_DETECT_SIZE = 224
VIDEO_IMG_SIZE = 64
VIDEO_NEURAL_SIZE = 224
VIDEO_LOW_FREQ_SIZE = 16
VIDEO_CHANNELS = 3
VIDEO_NUM_SAMPLES = 5
VIDEO_DIM_SIZE = (
    VIDEO_LOW_FREQ_SIZE * VIDEO_LOW_FREQ_SIZE * VIDEO_CHANNELS * VIDEO_NUM_SAMPLES
)  # 3840 floats = 7680 bytes (FP16)


def crop_letterbox(
    frames: Sequence[np.ndarray],
    threshold: int = 22,
    output_size: int = VIDEO_IMG_SIZE,
) -> np.ndarray:
    """
    Detect and crop static black bars (letterbox / pillarbox) across all sampled frames.
    Uses the union (max projection) across frames so dark individual scenes are not over-cropped,
    then normalizes all frames to (output_size, output_size).
    """
    if not frames:
        return np.empty((0, output_size, output_size, VIDEO_CHANNELS), dtype=np.uint8)

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
        if region.shape[0] != output_size or region.shape[1] != output_size:
            pil_img = Image.fromarray(region).resize(
                (output_size, output_size), Image.Resampling.BILINEAR
            )
            out.append(np.asarray(pil_img, dtype=np.uint8))
        else:
            out.append(region)
    return np.array(out, dtype=np.uint8)


def _frame_to_sample_array(frame: av.VideoFrame) -> np.ndarray:
    """
    Convert a decoded PyAV frame directly into a compact 128x128 RGB numpy array
    using FFmpeg libswscale SIMD in a single pass (avoids materializing 24 MB 4K RGB PIL buffers).
    """
    try:
        return frame.to_ndarray(
            format="rgb24",
            width=_SAMPLE_DETECT_SIZE,
            height=_SAMPLE_DETECT_SIZE,
        )
    except Exception:
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
    do_seek: bool = True,
) -> Tuple[Optional[np.ndarray], float]:
    """
    Seek backward to the nearest keyframe before target_sec (or continue forward if already close)
    and decode forward to the frame closest to target_sec (resilient to sparse AVI GOPs and B-frame delay).
    Returns (sample_array_or_None, last_decoded_sec).
    """
    if do_seek:
        target_pts = max(0, int(target_sec / tb))
        container.seek(target_pts, any_frame=False, backward=True, stream=stream)

    best_frame = None
    best_diff = float("inf")
    last_sec = -1e9
    for idx_f, f in enumerate(container.decode(stream)):
        if f.pts is not None:
            f_sec = float(f.pts * tb)
            last_sec = f_sec
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
        return None, last_sec
    return _frame_to_sample_array(best_frame), last_sec


def _decode_frame_near_timestamp_cuvid(
    container,
    stream,
    ctx,
    target_sec: float,
    tb: float,
    max_decode_frames: int = 600,
    do_seek: bool = True,
) -> Tuple[Optional[np.ndarray], float]:
    """
    Seek backward to the nearest keyframe before target_sec and decode forward using FFmpeg 8
    embedded NVIDIA CUVID with silicon 224x224 downscale (eliminating 40x PCIe transfer overhead).
    """
    if do_seek:
        target_pts = max(0, int(target_sec / tb))
        container.seek(target_pts, any_frame=False, backward=True, stream=stream)
        ctx.flush_buffers()

    best_frame = None
    best_diff = float("inf")
    last_sec = -1e9
    seek_f = 0
    done = False

    for pkt in container.demux(stream):
        if pkt.dts is None:
            continue
        pkt_ts = pkt.pts if pkt.pts is not None else pkt.dts
        for f in ctx.decode(pkt):
            seek_f += 1
            raw_ts = f.pts if f.pts is not None else (f.dts if f.dts is not None else pkt_ts)
            if raw_ts is not None:
                f_sec = float(raw_ts * tb)
                last_sec = f_sec
                diff = abs(f_sec - target_sec)
                if diff < best_diff:
                    best_diff = diff
                    best_frame = f
                if f_sec >= target_sec or seek_f >= max_decode_frames:
                    done = True
                    break
            else:
                best_frame = f
                done = True
                break
        if done:
            break

    # Flush delayed B-frames at EOF if no frame reached target_sec yet
    if best_frame is None:
        for f in ctx.decode(None):
            raw_ts = f.pts if f.pts is not None else f.dts
            if raw_ts is not None:
                f_sec = float(raw_ts * tb)
                last_sec = f_sec
                diff = abs(f_sec - target_sec)
                if diff < best_diff:
                    best_diff = diff
                    best_frame = f
            else:
                best_frame = f
                break

    if best_frame is None:
        return None, last_sec
    return _frame_to_sample_array(best_frame), last_sec


def _try_cuvid_extract_frames(
    container,
    stream,
    start_sec: float,
    dur: float,
    tb: float,
) -> Tuple[List[np.ndarray], Optional[str]]:
    """
    Attempt hardware-accelerated decoding via embedded FFmpeg 8 *_cuvid with native 224x224 resize.
    Returns (frames_list, fallback_reason_or_None). Never raises; caller falls back cleanly to CPU.
    """
    codec_name = getattr(stream.codec_context, "name", "") or ""
    dec_name = _CUVID_CODEC_MAP.get(codec_name)
    if not dec_name or dec_name not in _AVAILABLE_CUVID_DECODERS:
        return [], None  # Expected non-CUVID codec (e.g. wmv/prores/theora) -> silent CPU decode

    ctx = None
    try:
        ctx = av.Codec(dec_name, "r").create()
        if stream.codec_context.extradata:
            ctx.extradata = stream.codec_context.extradata
        ctx.options = {"resize": f"{_SAMPLE_DETECT_SIZE}x{_SAMPLE_DETECT_SIZE}"}
        ctx.open()

        frames: List[np.ndarray] = []
        if dur < 1.0:
            container.seek(0)
            ctx.flush_buffers()
            for pkt in container.demux(stream):
                if pkt.dts is None:
                    continue
                for f in ctx.decode(pkt):
                    frames.append(_frame_to_sample_array(f))
                    if len(frames) >= 5:
                        break
                if len(frames) >= 5:
                    break
            if not frames:
                for f in ctx.decode(None):
                    frames.append(_frame_to_sample_array(f))
                    if len(frames) >= 5:
                        break
        else:
            percentages = (0.10, 0.30, 0.50, 0.70, 0.90)
            last_decoded_sec = -1e9
            for pct in percentages:
                target_sec = start_sec + dur * pct
                do_seek = not (0.0 <= (target_sec - last_decoded_sec) <= 2.5)
                arr_f, last_sec = _decode_frame_near_timestamp_cuvid(
                    container, stream, ctx, target_sec, tb, do_seek=do_seek
                )
                if last_sec > -1e8:
                    last_decoded_sec = last_sec
                if arr_f is None and target_sec > start_sec:
                    fallback_sec = max(start_sec, target_sec - max(2.0, dur * 0.08))
                    arr_f, last_sec = _decode_frame_near_timestamp_cuvid(
                        container, stream, ctx, fallback_sec, tb, do_seek=True
                    )
                    if last_sec > -1e8:
                        last_decoded_sec = last_sec
                if arr_f is not None:
                    frames.append(arr_f)

        if frames:
            return frames, None
        return [], f"CUVID ({dec_name}) yielded 0 frames; decoded via CPU"
    except Exception as exc:
        return [], f"CUVID ({dec_name}) failed ({type(exc).__name__}: {exc}); decoded via CPU"
    finally:
        del ctx


def _collect_video_metadata(path_str: str) -> Tuple[int, int, Optional[str]]:
    """Collect (volume_id, file_id, prefix_hash) in a SINGLE file open (zero double iSCSI roundtrip)."""
    p = Path(path_str)
    vol_id, f_id, p_hash = 0, 0, None
    try:
        _, supports_fid = get_volume_capabilities(p)
        with _open_random_binary(p, 0) as f:
            st = os.fstat(f.fileno())
            vol_id = getattr(st, "st_dev", 0) or 0
            f_id = (getattr(st, "st_ino", 0) or 0) if supports_fid else 0
            if st.st_size > 0:
                p_hash = compute_prefix_hash_from_stream(f, st.st_size)
    except Exception:
        pass
    return vol_id, f_id, p_hash


def _worker_decode_path(
    task: Union[str, Tuple[str, bool]],
    output_size: int = VIDEO_IMG_SIZE,
    use_cuvid: bool = False,
    cpu_threads: int = 2,
) -> Tuple[str, float, Optional[np.ndarray], int, int, Optional[str], Optional[str]]:
    """
    Process-isolated worker: collects metadata (when not already probed), extracts 5 clean temporal
    RGB frames via embedded FFmpeg 8 *_cuvid (when enabled on RTX GPUs) with automatic multi-thread
    PyAV CPU fallback, and returns raw data outside the main process GIL.
    Returns (path_str, dur, arr_or_None, vol_id, file_id, prefix_hash, fallback_note_or_None).
    """
    if isinstance(task, tuple):
        path_str, need_meta = task[0], bool(task[1])
    else:
        path_str, need_meta = task, True

    p = Path(path_str)
    vol_id, f_id, p_hash = _collect_video_metadata(path_str) if need_meta else (0, 0, None)

    is_dav = p.suffix.lower() == ".dav"
    options = {"probesize": "65536", "analyzeduration": "0"} if is_dav else None

    try:
        container = av.open(path_str, options=options)
    except Exception:
        return path_str, -1.0, None, vol_id, f_id, p_hash, None

    fallback_note: Optional[str] = None
    try:
        if not container.streams.video:
            return path_str, -1.0, None, vol_id, f_id, p_hash, None

        stream = container.streams.video[0]
        dur = float(container.duration) / 1000000.0 if container.duration else None
        if (dur is None or dur <= 0) and stream.duration and stream.time_base:
            dur = float(stream.duration * stream.time_base)
        dur = dur if (dur and dur > 0) else 10.0

        tb = float(stream.time_base) if stream.time_base else (1.0 / 1000000.0)
        start_sec = float(stream.start_time * tb) if stream.start_time is not None else 0.0

        frames: List[np.ndarray] = []

        # 1. Strategy A: Timestamp-Accurate Seek + Forward Decode (MP4, MKV, AVI, MOV, WEBM, etc.)
        if not is_dav:
            # 1A. Fast hardware path on RTX GPUs: FFmpeg 8 *_cuvid + silicon 224x224 downscale
            if use_cuvid:
                frames, fallback_note = _try_cuvid_extract_frames(container, stream, start_sec, dur, tb)
                if not frames:
                    # Reset container cleanly before falling back to multi-thread CPU decode
                    try:
                        container.seek(0)
                    except Exception:
                        try:
                            container.close()
                        except Exception:
                            pass
                        container = av.open(path_str, options=options)
                        stream = container.streams.video[0]

            # 1B. Multi-thread PyAV CPU decode (universal CPU path & seamless CUVID fallback)
            if not frames:
                if cpu_threads > 1:
                    try:
                        stream.thread_type = "AUTO"
                        stream.codec_context.thread_count = cpu_threads
                    except Exception:
                        pass

                if dur < 1.0:
                    try:
                        container.seek(0)
                        for f in container.decode(stream):
                            frames.append(_frame_to_sample_array(f))
                            if len(frames) >= 5:
                                break
                    except Exception:
                        frames.clear()
                else:
                    percentages = (0.10, 0.30, 0.50, 0.70, 0.90)
                    last_decoded_sec = -1e9
                    try:
                        for pct in percentages:
                            target_sec = start_sec + dur * pct
                            # Continue forward without backward re-seek if target is right ahead in the same GOP
                            do_seek = not (0.0 <= (target_sec - last_decoded_sec) <= 2.5)
                            arr_f, last_sec = _decode_frame_near_timestamp(
                                container, stream, target_sec, tb, do_seek=do_seek
                            )
                            if last_sec > -1e8:
                                last_decoded_sec = last_sec
                            if arr_f is None and target_sec > start_sec:
                                # Retry slightly earlier if tail seek hit EOF in a truncated index
                                fallback_sec = max(start_sec, target_sec - max(2.0, dur * 0.08))
                                arr_f, last_sec = _decode_frame_near_timestamp(
                                    container, stream, fallback_sec, tb, do_seek=True
                                )
                                if last_sec > -1e8:
                                    last_decoded_sec = last_sec
                            if arr_f is not None:
                                frames.append(arr_f)
                    except Exception:
                        frames.clear()

                if not frames:
                    try:
                        stream.codec_context.skip_frame = "DEFAULT"
                    except Exception:
                        pass
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
            return path_str, -1.0, None, vol_id, f_id, p_hash, None

        while len(frames) < 5:
            frames.append(frames[-1])

        expected_shape = (VIDEO_NUM_SAMPLES, output_size, output_size, VIDEO_CHANNELS)
        arr = crop_letterbox(frames[:5], output_size=output_size)  # [5, out, out, 3]
        if arr.shape != expected_shape:
            return path_str, -1.0, None, vol_id, f_id, p_hash, None

        return path_str, dur, arr, vol_id, f_id, p_hash, fallback_note

    except Exception:
        return path_str, -1.0, None, vol_id, f_id, p_hash, None
    finally:
        try:
            container.close()
        except Exception:
            pass


def _worker_decode_path_neural(
    task: Union[str, Tuple[str, bool]],
) -> Tuple[str, float, Optional[np.ndarray], int, int, Optional[str], Optional[str]]:
    """Picklable ProcessPoolExecutor entry point: multi-thread PyAV CPU (224x224 RGB for Hybrid DINOv2+DCT)."""
    return _worker_decode_path(task, output_size=VIDEO_NEURAL_SIZE, use_cuvid=False, cpu_threads=2)


def _worker_decode_path_cuvid_neural(
    task: Union[str, Tuple[str, bool]],
) -> Tuple[str, float, Optional[np.ndarray], int, int, Optional[str], Optional[str]]:
    """Picklable ProcessPoolExecutor entry point: FFmpeg 8 *_cuvid + 224x224 silicon resize with CPU fallback."""
    return _worker_decode_path(task, output_size=VIDEO_NEURAL_SIZE, use_cuvid=True, cpu_threads=2)


def _worker_decode_path_cuvid_dct(
    task: Union[str, Tuple[str, bool]],
) -> Tuple[str, float, Optional[np.ndarray], int, int, Optional[str], Optional[str]]:
    """Picklable ProcessPoolExecutor entry point: FFmpeg 8 *_cuvid + CPU fallback (64x64 pure-DCT mode)."""
    return _worker_decode_path(task, output_size=VIDEO_IMG_SIZE, use_cuvid=True, cpu_threads=2)
