"""
Extrator de Assinaturas de Vídeo de Altíssima Performance com Multiprocessamento Paralelo e GPU Tensor Cores.
Elimina completamente a contenção do GIL do Python via ProcessPoolExecutor isolado por processo,
alimenta em fluxo contínuo a RTX 5090 e processa tensores DCT 2D diretamente em VRAM (FP16).
"""

import sys
import os
import time
import queue
import threading
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import List, Optional, Tuple, Callable, Dict, Any
import numpy as np
import torch
from PIL import Image

import av
av.logging.set_level(av.logging.PANIC)

from dupeclean.core.models import FileRecord
from dupeclean.core.hasher_image import _get_dct_matrix
from dupeclean.core.cache import CacheManager, compute_prefix_hash

# Dimensão dos quadros e frequência para assinatura temporal de vídeos
VIDEO_IMG_SIZE = 64
VIDEO_LOW_FREQ_SIZE = 16
VIDEO_CHANNELS = 3
VIDEO_NUM_SAMPLES = 5
VIDEO_DIM_SIZE = VIDEO_LOW_FREQ_SIZE * VIDEO_LOW_FREQ_SIZE * VIDEO_CHANNELS * VIDEO_NUM_SAMPLES  # 3840 floats = 7680 bytes (FP16)


def get_video_duration(path: Path) -> Optional[float]:
    """Obtém duração do vídeo nativamente em memória via PyAV com I/O reduzido."""
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


def crop_letterbox(frames: np.ndarray, threshold: int = 25) -> np.ndarray:
    """
    Detecta e recorta barras pretas (letterbox / pillarbox) antes do hashing.
    Evita que o formato de tela gere harmônicos espúrios na DCT 2D.
    """
    cropped_list = []
    for f in frames:
        row_means = f.mean(axis=1)
        col_means = f.mean(axis=0)
        valid_rows = np.where(row_means > threshold)[0]
        valid_cols = np.where(col_means > threshold)[0]
        if len(valid_rows) >= 16 and len(valid_cols) >= 16 and (len(valid_rows) < VIDEO_IMG_SIZE or len(valid_cols) < VIDEO_IMG_SIZE):
            top, bottom = valid_rows[0], valid_rows[-1] + 1
            left, right = valid_cols[0], valid_cols[-1] + 1
            cropped = f[top:bottom, left:right]
            pil_img = Image.fromarray(cropped).resize((VIDEO_IMG_SIZE, VIDEO_IMG_SIZE), Image.Resampling.BILINEAR)
            cropped_list.append(np.asarray(pil_img, dtype=np.uint8))
        else:
            cropped_list.append(f)
    return np.array(cropped_list)


def extract_video_frames_and_duration(
    path: Path,
    num_samples: int = 5
) -> Optional[Tuple[float, np.ndarray]]:
    """Função compatível de extração direta por caminho."""
    res = _worker_decode_path(str(path))
    if res is None:
        return None
    _, dur, arr = res
    if arr is None or dur == -1.0:
        return None
    return dur, arr


def _worker_decode_path(path_str: str) -> Tuple[str, float, Optional[np.ndarray]]:
    """
    Worker isolado por processo: abre o vídeo, extrai 5 quadros temporais RGB limpos e retorna dados brutos.
    Opera fora do GIL do processo principal, eliminando travamentos de thread no Windows.
    Retorna (path_str, -1.0, None) em caso de falha/arquivo corrompido para persistência de tombstone.
    """
    p = Path(path_str)
    is_dav = p.suffix.lower() == ".dav"
    options = {"probesize": "65536", "analyzeduration": "0"} if is_dav else None

    try:
        container = av.open(path_str, options=options)
    except Exception:
        return path_str, -1.0, None

    try:
        if not container.streams.video:
            container.close()
            return path_str, -1.0, None

        stream = container.streams.video[0]
        dur = float(container.duration) / 1000000.0 if container.duration else None
        if dur is None and stream.duration and stream.time_base:
            dur = float(stream.duration * stream.time_base)
        dur = dur or 10.0

        frames = []

        # 1. Estratégia A: Fast Keyframe Seek (para formatos indexados: MP4, MKV, AVI, MOV, WEBM, etc.)
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

        # 2. Estratégia B: Extração Temporal de 5 Keyframes Distintos (para .dav e fluxos contínuos)
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
            return path_str, -1.0, None

        while len(frames) < 5:
            frames.append(frames[-1])

        expected_shape = (VIDEO_NUM_SAMPLES, VIDEO_IMG_SIZE, VIDEO_IMG_SIZE, VIDEO_CHANNELS)
        arr = np.array(frames[:5], dtype=np.uint8)  # [5, 64, 64, 3]
        if arr.shape != expected_shape:
            return path_str, -1.0, None

        return path_str, dur, arr

    except Exception:
        try:
            container.close()
        except Exception:
            pass
        return path_str, -1.0, None


class _AsyncVideoCacheWriter:
    """
    Escritor assíncrono em background para persistência do cache de vídeos (SQLite WAL).
    Desacopla totalmente a decodificação de vídeo e a GPU do banco de dados,
    acumulando lotes e tombstones em memória e commitando em segundo plano.
    """
    def __init__(self, cache_manager: CacheManager, batch_size: int = 512, flush_interval: float = 0.5):
        self.cache_manager = cache_manager
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._queue: queue.Queue = queue.Queue(maxsize=50000)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="AsyncVideoCacheWriter")
        self._thread.start()

    def add(self, entry: Tuple[str, int, float, float, bytes]):
        try:
            self._queue.put_nowait(entry)
        except queue.Full:
            try:
                self._queue.put(entry, timeout=0.1)
            except queue.Full:
                pass

    def add_batch(self, entries: List[Tuple[str, int, float, float, bytes]]):
        for e in entries:
            self.add(e)

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
                    self.cache_manager.save_video_batch(batch)
                except Exception:
                    pass
                batch.clear()
                last_flush = now

        if batch:
            try:
                self.cache_manager.save_video_batch(batch)
            except Exception:
                pass

    def close(self):
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)


def extract_video_embeddings(
    records: List[FileRecord],
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    cache_manager: Optional[CacheManager] = None
) -> Tuple[List[FileRecord], torch.Tensor]:
    """
    Processa vídeos em altíssima velocidade utilizando multiprocessamento paralelo (ProcessPoolExecutor)
    e consolidação em lote na RTX 5090 (PyTorch CUDA FP16).
    Garante isolamento de memória por arquivo (zero vazamento de frames) e persistência WAL no SQLite.
    """
    device = "cuda"
    dtype = torch.float16
    cache = cache_manager or CacheManager()

    total = len(records)
    dim_size = VIDEO_DIM_SIZE
    if total == 0:
        return [], torch.empty((0, dim_size), device=device, dtype=dtype)

    # 1. Consulta em lote ao cache persistente (valida 7.680 bytes estruturais)
    cached_map = cache.get_video_cache(records, expected_bytes=dim_size * 2, stop_check=stop_check)
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
                # Arquivo corrompido/ilegível já marcado como tombstone: descarta sem reprocessar
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

    if progress_callback and cached_count > 0:
        progress_callback(f"Loading cached videos ({cached_count}/{total})...", cached_count, total)

    # 2. Se todos os vídeos já estavam em cache, retorna de imediato
    if not to_process or (stop_check and stop_check()):
        if not embeddings_list:
            return [], torch.empty((0, dim_size), device=device, dtype=dtype)
        with torch.inference_mode():
            all_embeddings = torch.cat(embeddings_list, dim=0)
        return valid_records, all_embeddings

    # 3. Preparação das transformadas DCT na GPU com Spectral Whitening
    to_process.sort(key=lambda r: str(r.path.parent))

    dct_mat = _get_dct_matrix(VIDEO_IMG_SIZE, device=device, dtype=dtype)
    dct_mat_t = dct_mat.t()

    u = torch.arange(VIDEO_LOW_FREQ_SIZE, device=device, dtype=dtype).unsqueeze(1)
    v = torch.arange(VIDEO_LOW_FREQ_SIZE, device=device, dtype=dtype).unsqueeze(0)
    spectral_weight = (u + v + 1.0).pow(2.0)
    spectral_weight[0, 0] = 0.0  # Zero DC

    uncached_total = len(to_process)
    cache_writer = _AsyncVideoCacheWriter(cache)

    # Mapeamento rápido de caminhos para objetos FileRecord
    path_to_rec = {str(r.path): r for r in to_process}
    path_list = list(path_to_rec.keys())

    # Número ideal de processos trabalhadores no Core i9 (16 workers saturam I/O e eliminam o GIL)
    num_workers = min(16, max(4, (os.cpu_count() or 4) // 2))

    processed_count = 0
    last_progress_time = 0.0
    batch_records: List[FileRecord] = []
    batch_frames: List[np.ndarray] = []
    batch_durations: List[float] = []

    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        try:
            with torch.inference_mode():
                # Processamento em lote com chunksize=16 para máxima taxa de transferência inter-processos
                for res in executor.map(_worker_decode_path, path_list, chunksize=16):
                    if stop_check and stop_check():
                        break

                    processed_count += 1
                    current_total_done = cached_count + processed_count

                    now = time.time()
                    if progress_callback and (now - last_progress_time >= 0.08 or processed_count == uncached_total):
                        last_progress_time = now
                        progress_callback(f"Processing videos ({current_total_done:,}/{total:,})...", current_total_done, total)

                    if res is not None:
                        p_str, duration, frames_arr = res
                        rec = path_to_rec.get(p_str)
                        if rec is not None:
                            if rec.prefix_hash is None:
                                rec.prefix_hash = compute_prefix_hash(rec.path, rec.size)
                            expected_shape = (VIDEO_NUM_SAMPLES, VIDEO_IMG_SIZE, VIDEO_IMG_SIZE, VIDEO_CHANNELS)
                            if frames_arr is None or duration == -1.0 or getattr(frames_arr, "shape", None) != expected_shape:
                                # Grava tombstone no SQLite para nunca mais tentar reprocessar este arquivo corrompido
                                cache_writer.add((
                                    str(rec.path),
                                    rec.size,
                                    rec.mtime,
                                    -1.0,
                                    b"",
                                    rec.volume_id,
                                    rec.file_id,
                                    rec.prefix_hash
                                ))
                            else:
                                rec.duration = duration
                                batch_records.append(rec)
                                batch_frames.append(frames_arr)
                                batch_durations.append(duration)

                    # Processa em lotes matriciais na RTX 5090 (128 itens por lote)
                    target_batch = 128
                    if len(batch_records) >= target_batch or processed_count == uncached_total:
                        if batch_frames:
                            stacked = np.stack(batch_frames)  # [B, 5, 64, 64, 3]
                            t_batch = torch.from_numpy(stacked).permute(0, 1, 4, 2, 3).to(device=device, dtype=dtype) / 255.0  # [B, 5, 3, 64, 64]
                            B = t_batch.shape[0]
                            t_flat = t_batch.reshape(B * VIDEO_NUM_SAMPLES * VIDEO_CHANNELS, VIDEO_IMG_SIZE, VIDEO_IMG_SIZE)
                            dct_batch = torch.matmul(dct_mat, torch.matmul(t_flat, dct_mat_t))
                            low_freq = dct_batch[:, :VIDEO_LOW_FREQ_SIZE, :VIDEO_LOW_FREQ_SIZE].clone()
                            low_freq = low_freq * spectral_weight.unsqueeze(0)

                            flat = low_freq.reshape(B, -1)  # [B, 3840]
                            norms = torch.norm(flat, p=2, dim=1, keepdim=True).clamp(min=1e-6)
                            normalized = flat / norms  # [B, 3840]

                            embeddings_list.append(normalized)
                            valid_records.extend(batch_records)

                            norm_cpu_np = normalized.cpu().numpy()
                            for b_i, b_rec in enumerate(batch_records):
                                b_dur = batch_durations[b_i]
                                cache_writer.add((
                                    str(b_rec.path),
                                    b_rec.size,
                                    b_rec.mtime,
                                    b_dur,
                                    norm_cpu_np[b_i:b_i + 1].tobytes(),
                                    b_rec.volume_id,
                                    b_rec.file_id,
                                    b_rec.prefix_hash
                                ))

                            batch_records.clear()
                            batch_frames.clear()
                            batch_durations.clear()

        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            cache_writer.close()

    if not embeddings_list:
        return [], torch.empty((0, dim_size), device=device, dtype=dtype)

    with torch.inference_mode():
        all_embeddings = torch.cat(embeddings_list, dim=0)
    return valid_records, all_embeddings
