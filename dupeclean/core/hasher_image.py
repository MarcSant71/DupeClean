"""
Extrator de Assinaturas e Hashes Visuais de Imagens via GPU (RTX 5090 Tensor Cores).
Usa DCT 2D em lote com tensores FP16 para calcular embeddings visuais e pHash em alta velocidade.
"""

from typing import List, Callable, Optional, Tuple
import os
import math
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
import torch
import numpy as np

from dupeclean.core.models import FileRecord, DuplicateGroup, ScanMode
from dupeclean.core.cache import CacheManager

# Dimensão da imagem para o cálculo do hash perceptual (64x64)
IMG_SIZE = 64
LOW_FREQ_SIZE = 8  # 8x8 coeficientes de baixa frequência (64 valores)


def _load_single_image(args: Tuple[FileRecord, Optional[Callable[[], bool]]]) -> Optional[Tuple[FileRecord, np.ndarray]]:
    """Carrega e redimensiona uma imagem em paralelo liberando o GIL do Python."""
    rec, stop_check = args
    if stop_check and stop_check():
        return None
    try:
        with Image.open(rec.path) as img:
            gray = img.convert("L").resize((IMG_SIZE, IMG_SIZE), Image.Resampling.BILINEAR)
            arr = np.asarray(gray, dtype=np.float32) / 255.0
            return rec, arr
    except Exception:
        return None


def _get_dct_matrix(n: int, device: str = "cuda", dtype=torch.float16) -> torch.Tensor:
    """Gera matriz ortogonal de transformação DCT-II."""
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
    batch_size: int = 512,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    cache_manager: Optional[CacheManager] = None
) -> Tuple[List[FileRecord], torch.Tensor]:
    """
    Carrega imagens em paralelo saturando todos os núcleos de CPU e I/O de rede,
    alimenta a RTX 5090 em lotes de alto rendimento e calcula embeddings normalizados.
    Utiliza cache SQLite persistente (WAL) para carregar instantaneamente imagens já analisadas.
    Retorna (registros_validos, tensor_embeddings_gpu).
    """
    device = "cuda"
    dtype = torch.float16
    cache = cache_manager or CacheManager()

    total = len(records)
    if total == 0:
        return [], torch.empty((0, LOW_FREQ_SIZE * LOW_FREQ_SIZE), device=device, dtype=dtype)

    # 1. Consulta em lote ao cache persistente
    cached_map = cache.get_image_cache(records)
    cached_count = len(cached_map)

    valid_records: List[FileRecord] = []
    all_embeddings_list: List[torch.Tensor] = []

    # Separa imagens já cacheadas das que precisam de processamento
    to_process: List[FileRecord] = []
    for rec in records:
        p_key = str(rec.path)
        if p_key in cached_map:
            valid_records.append(rec)
            # Reconstrói array a partir do BLOB binário FP16 [64 floats]
            arr = np.frombuffer(cached_map[p_key], dtype=np.float16).reshape(1, LOW_FREQ_SIZE * LOW_FREQ_SIZE)
            all_embeddings_list.append(torch.tensor(arr, device=device, dtype=dtype))
        else:
            to_process.append(rec)

    if progress_callback and cached_count > 0:
        progress_callback(f"Loading cached photos ({cached_count}/{total})...", cached_count, total)

    # 2. Se todas estavam no cache, finaliza imediatamente!
    if not to_process or (stop_check and stop_check()):
        if not all_embeddings_list:
            return [], torch.empty((0, LOW_FREQ_SIZE * LOW_FREQ_SIZE), device=device, dtype=dtype)
        return valid_records, torch.cat(all_embeddings_list, dim=0)

    # 3. Processamento de Alta Performance: I/O multithreaded + Tensor Cores na RTX 5090
    dct_mat = _get_dct_matrix(IMG_SIZE, device=device, dtype=dtype)
    dct_mat_t = dct_mat.t()

    num_workers = min(32, max(4, os.cpu_count() or 4))
    uncached_total = len(to_process)

    executor = ThreadPoolExecutor(max_workers=num_workers)
    try:
        for chunk_start in range(0, uncached_total, batch_size):
            if stop_check and stop_check():
                break

            chunk_records = to_process[chunk_start : chunk_start + batch_size]
            task_args = [(rec, stop_check) for rec in chunk_records]

            # Decodifica e redimensiona em paralelo usando todos os núcleos da CPU
            loaded_items = list(executor.map(_load_single_image, task_args))

            if stop_check and stop_check():
                break

            batch_images = []
            batch_records = []
            for item in loaded_items:
                if item is not None:
                    rec, arr = item
                    batch_images.append(arr)
                    batch_records.append(rec)

            if batch_images:
                # Envia o lote para a VRAM da RTX 5090 e calcula DCT 2D via Tensor Cores
                t_batch = torch.tensor(np.array(batch_images), device=device, dtype=dtype)
                dct_2d = torch.matmul(dct_mat, torch.matmul(t_batch, dct_mat_t))
                low_freq = dct_2d[:, :LOW_FREQ_SIZE, :LOW_FREQ_SIZE].clone()
                low_freq[:, 0, 0] = low_freq[:, 0, 0] * 0.15
                flat = low_freq.reshape(low_freq.shape[0], -1)

                norms = torch.norm(flat, p=2, dim=1, keepdim=True).clamp(min=1e-6)
                normalized = flat / norms

                all_embeddings_list.append(normalized)
                valid_records.extend(batch_records)

                # Persiste imediatamente o lote no cache SQLite (WAL)
                norm_cpu_np = normalized.cpu().numpy()
                new_cache_entries = [
                    (str(b_rec.path), b_rec.size, b_rec.mtime, norm_cpu_np[b_idx:b_idx + 1].tobytes())
                    for b_idx, b_rec in enumerate(batch_records)
                ]
                cache.save_image_batch(new_cache_entries)

            done_count = cached_count + chunk_start + len(chunk_records)
            if progress_callback:
                progress_callback(
                    f"Processing photos ({min(done_count, total)}/{total})...",
                    min(done_count, total),
                    total
                )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if not all_embeddings_list:
        return [], torch.empty((0, LOW_FREQ_SIZE * LOW_FREQ_SIZE), device=device, dtype=dtype)

    all_embeddings = torch.cat(all_embeddings_list, dim=0)
    return valid_records, all_embeddings
