"""
Extrator de Assinaturas de Vídeo com Decodificação por Hardware (NVDEC / FFmpeg HWAccel).
Acelera a extração de múltiplos frames na RTX 5090 e gera assinaturas temporais em tensores GPU.
"""

import shutil
import subprocess
import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple, Callable
import numpy as np
import torch
from PIL import Image

from dupeclean.core.models import FileRecord
from dupeclean.core.hasher_image import _get_dct_matrix, IMG_SIZE, LOW_FREQ_SIZE
from dupeclean.core.cache import CacheManager

# Flag para ocultar qualquer janela de console/DOS no Windows
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def find_ffmpeg_binaries() -> Tuple[Optional[str], Optional[str]]:
    """
    Localiza os executáveis do ffmpeg e ffprobe.
    Prioriza a pasta 'bin/' embutida no próprio projeto para portabilidade total.
    """
    # 1. Prioridade: bin/ embutido no DupeClean
    project_bin = Path(__file__).resolve().parent.parent.parent / "bin"
    ffmpeg_local = project_bin / "ffmpeg.exe"
    ffprobe_local = project_bin / "ffprobe.exe"
    if ffmpeg_local.exists() and ffprobe_local.exists():
        return str(ffmpeg_local), str(ffprobe_local)

    # 2. Fallback: PATH global do sistema
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    return ffmpeg, ffprobe


def get_video_duration(path: Path, ffprobe_path: str) -> Optional[float]:
    """Obtém a duração total do vídeo em segundos via ffprobe."""
    try:
        cmd = [
            ffprobe_path,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path)
        ]
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            creationflags=CREATE_NO_WINDOW
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            duration_str = data.get("format", {}).get("duration")
            if duration_str:
                return float(duration_str)
    except Exception:
        pass
    return None


def extract_video_frames(
    path: Path,
    duration: float,
    ffmpeg_path: str,
    num_samples: int = 5
) -> Optional[np.ndarray]:
    """
    Extrai `num_samples` frames distribuídos no tempo usando decodificação acelerada por hardware.
    Retorna array numpy com shape [num_samples, 64, 64].
    """
    if duration <= 1.0:
        sample_times = [duration * 0.5]
    else:
        # Pontos de amostragem percentuais (15%, 35%, 50%, 65%, 85%)
        percentages = [0.15, 0.35, 0.50, 0.65, 0.85][:num_samples]
        sample_times = [duration * p for p in percentages]

    frames = []

    for t in sample_times:
        try:
            # -hwaccel cuda ativa decodificação direta via NVDEC na RTX 5090 (device 0)
            cmd = [
                ffmpeg_path,
                "-hwaccel", "cuda",
                "-hwaccel_device", "0",
                "-ss", f"{t:.2f}",
                "-i", str(path),
                "-vframes", "1",
                "-s", f"{IMG_SIZE}x{IMG_SIZE}",
                "-f", "rawvideo",
                "-pix_fmt", "gray",
                "-v", "quiet",
                "pipe:1"
            ]
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=4,
                creationflags=CREATE_NO_WINDOW
            )
            if res.returncode == 0 and len(res.stdout) >= IMG_SIZE * IMG_SIZE:
                frame_bytes = res.stdout[:IMG_SIZE * IMG_SIZE]
                frame_arr = np.frombuffer(frame_bytes, dtype=np.uint8).reshape((IMG_SIZE, IMG_SIZE))
                frames.append(frame_arr.astype(np.float32) / 255.0)
            else:
                # Tenta fallback sem hwaccel caso o codec exótico não seja acelerável por NVDEC
                cmd_cpu = [
                    ffmpeg_path,
                    "-ss", f"{t:.2f}",
                    "-i", str(path),
                    "-vframes", "1",
                    "-s", f"{IMG_SIZE}x{IMG_SIZE}",
                    "-f", "rawvideo",
                    "-pix_fmt", "gray",
                    "-v", "quiet",
                    "pipe:1"
                ]
                res_cpu = subprocess.run(
                    cmd_cpu,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=4,
                    creationflags=CREATE_NO_WINDOW
                )
                if res_cpu.returncode == 0 and len(res_cpu.stdout) >= IMG_SIZE * IMG_SIZE:
                    frame_bytes = res_cpu.stdout[:IMG_SIZE * IMG_SIZE]
                    frame_arr = np.frombuffer(frame_bytes, dtype=np.uint8).reshape((IMG_SIZE, IMG_SIZE))
                    frames.append(frame_arr.astype(np.float32) / 255.0)
        except Exception:
            continue

    if not frames:
        return None

    # Preenche se faltou algum frame
    while len(frames) < num_samples:
        frames.append(frames[-1])

    return np.array(frames[:num_samples])


def extract_video_embeddings(
    records: List[FileRecord],
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    cache_manager: Optional[CacheManager] = None
) -> Tuple[List[FileRecord], torch.Tensor]:
    """
    Processa arquivos de vídeo, extrai múltiplos frames via NVDEC
    e consolida assinaturas compostas em tensores na GPU.
    Utiliza cache SQLite persistente com commits periódicos para retenção total de progresso.
    """
    device = "cuda"
    dtype = torch.float16
    cache = cache_manager or CacheManager()

    total = len(records)
    if total == 0:
        return [], torch.empty((0, LOW_FREQ_SIZE * LOW_FREQ_SIZE * 5), device=device, dtype=dtype)

    # 1. Consulta em lote ao cache persistente
    cached_map = cache.get_video_cache(records)
    cached_count = len(cached_map)

    valid_records: List[FileRecord] = []
    embeddings_list: List[torch.Tensor] = []

    to_process: List[FileRecord] = []
    for rec in records:
        p_key = str(rec.path)
        if p_key in cached_map:
            valid_records.append(rec)
            dur, emb_blob = cached_map[p_key]
            # Reconstrói array a partir do BLOB binário FP16 [320 floats]
            arr = np.frombuffer(emb_blob, dtype=np.float16).reshape(1, LOW_FREQ_SIZE * LOW_FREQ_SIZE * 5)
            embeddings_list.append(torch.tensor(arr, device=device, dtype=dtype))
        else:
            to_process.append(rec)

    if progress_callback and cached_count > 0:
        progress_callback(f"Loading cached videos ({cached_count}/{total})...", cached_count, total)

    # 2. Se todos os vídeos já estavam em cache, retorna de imediato
    if not to_process or (stop_check and stop_check()):
        if not embeddings_list:
            return [], torch.empty((0, LOW_FREQ_SIZE * LOW_FREQ_SIZE * 5), device=device, dtype=dtype)
        return valid_records, torch.cat(embeddings_list, dim=0)

    # 3. Localiza binários e prepara transformadas para os vídeos restantes
    ffmpeg_path, ffprobe_path = find_ffmpeg_binaries()
    if not ffmpeg_path or not ffprobe_path:
        if embeddings_list:
            return valid_records, torch.cat(embeddings_list, dim=0)
        return [], torch.empty((0, LOW_FREQ_SIZE * LOW_FREQ_SIZE * 5), device=device, dtype=dtype)

    dct_mat = _get_dct_matrix(IMG_SIZE, device=device, dtype=dtype)
    dct_mat_t = dct_mat.t()

    new_cache_entries: List[Tuple[str, int, float, float, bytes]] = []
    uncached_total = len(to_process)

    for idx, rec in enumerate(to_process):
        if stop_check and stop_check():
            if new_cache_entries:
                cache.save_video_batch(new_cache_entries)
            break

        current_total_done = cached_count + idx + 1
        if progress_callback:
            progress_callback(f"Processing videos ({current_total_done}/{total})...", current_total_done, total)

        duration = get_video_duration(rec.path, ffprobe_path)
        if not duration or duration <= 0:
            continue

        frames_arr = extract_video_frames(rec.path, duration, ffmpeg_path, num_samples=5)
        if frames_arr is None:
            continue

        # frames_arr: [5, 64, 64]
        t_frames = torch.tensor(frames_arr, device=device, dtype=dtype)
        dct_2d = torch.matmul(dct_mat, torch.matmul(t_frames, dct_mat_t))
        low_freq = dct_2d[:, :LOW_FREQ_SIZE, :LOW_FREQ_SIZE].clone()
        low_freq[:, 0, 0] = low_freq[:, 0, 0] * 0.15

        # Concatena os 5 frames em uma assinatura contínua [5 * 64] = 320 dimensões
        flat = low_freq.reshape(-1)
        norm = torch.norm(flat, p=2).clamp(min=1e-6)
        normalized = (flat / norm).unsqueeze(0)  # [1, 320]

        embeddings_list.append(normalized)
        valid_records.append(rec)

        # Prepara entrada para persistência no SQLite
        emb_bytes = normalized.cpu().numpy().tobytes()
        new_cache_entries.append((str(rec.path), rec.size, rec.mtime, duration, emb_bytes))

        # Salva periodicamente a cada 25 vídeos para retenção em cancelamento
        if len(new_cache_entries) >= 25:
            cache.save_video_batch(new_cache_entries)
            new_cache_entries = []

    if new_cache_entries:
        cache.save_video_batch(new_cache_entries)

    if not embeddings_list:
        return [], torch.empty((0, LOW_FREQ_SIZE * LOW_FREQ_SIZE * 5), device=device, dtype=dtype)

    all_embeddings = torch.cat(embeddings_list, dim=0)
    return valid_records, all_embeddings
