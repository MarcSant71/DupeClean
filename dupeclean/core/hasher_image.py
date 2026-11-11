"""
Extrator de Assinaturas e Hashes Visuais de Imagens via GPU (RTX 5090 Tensor Cores).
Usa DCT 2D em lote com tensores FP16 para calcular embeddings visuais e pHash em alta velocidade.
"""

from typing import List, Callable, Optional, Tuple
import os
import math
import time
import queue
import threading
from PIL import Image
import torch
import numpy as np

from dupeclean.core.models import FileRecord, DuplicateGroup, ScanMode
from dupeclean.core.cache import CacheManager

# Parâmetros de extração de assinaturas visuais de alta discriminação
IMG_SIZE = 64
LOW_FREQ_SIZE = 16
CHANNELS = 3
ALPHA = 2.0  # Equalização espectral (whitening) para evitar que o fundo estático domine o sujeito
DIM_SIZE = CHANNELS * LOW_FREQ_SIZE * LOW_FREQ_SIZE  # 768 dimensões (1.536 bytes em FP16)


def _load_single_image(args: Tuple[FileRecord, Optional[Callable[[], bool]]]) -> Optional[Tuple[FileRecord, np.ndarray]]:
    """Carrega e redimensiona uma imagem em paralelo liberando o GIL do Python."""
    rec, stop_check = args
    if stop_check and stop_check():
        return None
    try:
        with Image.open(rec.path) as img:
            img.draft("RGB", (IMG_SIZE, IMG_SIZE))
            rgb = img.convert("RGB").resize((IMG_SIZE, IMG_SIZE), Image.Resampling.BILINEAR)
            arr = np.asarray(rgb, dtype=np.uint8)
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
    batch_size: int = 1024,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None,
    cache_manager: Optional[CacheManager] = None
) -> Tuple[List[FileRecord], torch.Tensor]:
    """
    Carrega imagens em paralelo maximizando a eficiência de CPU e throughput de I/O,
    alimenta a RTX 5090 em lotes de alto rendimento e calcula embeddings normalizados.
    Utiliza cache SQLite persistente (WAL) para carregar instantaneamente imagens já analisadas.
    Retorna (registros_validos, tensor_embeddings_gpu).
    """
    device = "cuda"
    dtype = torch.float16
    cache = cache_manager or CacheManager()

    total = len(records)
    dim_size = DIM_SIZE
    if total == 0:
        return [], torch.empty((0, dim_size), device=device, dtype=dtype)

    # 1. Consulta em lote ao cache persistente com telemetria contínua
    cached_map = cache.get_image_cache(records, expected_bytes=dim_size * 2, progress_callback=progress_callback, stop_check=stop_check)
    if stop_check and stop_check():
        return [], torch.empty((0, dim_size), device=device, dtype=dtype)
    cached_count = len(cached_map)

    valid_records: List[FileRecord] = []
    cached_recs: List[FileRecord] = []
    to_process: List[FileRecord] = []

    # Separa imagens já cacheadas das que precisam de processamento
    for rec in records:
        if stop_check and stop_check():
            return [], torch.empty((0, dim_size), device=device, dtype=dtype)
        p_key = str(rec.path)
        if p_key in cached_map:
            cached_recs.append(rec)
        else:
            to_process.append(rec)

    all_embeddings_list: List[torch.Tensor] = []

    if cached_recs:
        valid_records.extend(cached_recs)
        # Prealoca matriz contígua em RAM de uma vez (elimina 140k micro-alocações e fragmentação)
        cached_mat = np.empty((len(cached_recs), dim_size), dtype=np.float16)
        for idx, r in enumerate(cached_recs):
            cached_mat[idx] = np.frombuffer(cached_map[str(r.path)], dtype=np.float16)
        with torch.inference_mode():
            all_embeddings_list.append(torch.from_numpy(cached_mat).to(device=device, dtype=dtype))
        del cached_mat

    if progress_callback and cached_count > 0:
        progress_callback(f"Loading cached images ({cached_count}/{total})...", cached_count, total)

    # 2. Se todas estavam no cache, finaliza imediatamente!
    if not to_process or (stop_check and stop_check()):
        if not all_embeddings_list:
            return [], torch.empty((0, dim_size), device=device, dtype=dtype)
        with torch.inference_mode():
            all_embeddings = torch.cat(all_embeddings_list, dim=0)
        return valid_records, all_embeddings

    # 3. Processamento de Altíssimo Throughput: Streaming Producer-Consumer Concorrente
    # Threads de CPU decodificam continuamente para uma fila delimitada em RAM,
    # enquanto a GPU (Tensor Cores) e o SQLite operam sem tempo ocioso em paralelo.
    dct_mat = _get_dct_matrix(IMG_SIZE, device=device, dtype=dtype)
    dct_mat_t = dct_mat.t()
    u = torch.arange(LOW_FREQ_SIZE, device=device).unsqueeze(1)
    v = torch.arange(LOW_FREQ_SIZE, device=device).unsqueeze(0)
    # Formato (1, 1, LOW_FREQ_SIZE, LOW_FREQ_SIZE) para broadcasting perfeito em tensores RGB [B, 3, H, W]
    spectral_weight = (u + v + 1.0).pow(ALPHA).unsqueeze(0).unsqueeze(0).to(dtype=dtype)

    num_workers = min(32, max(4, os.cpu_count() or 4))
    uncached_total = len(to_process)
    effective_batch = max(64, min(batch_size, 1024))

    img_queue: queue.Queue = queue.Queue(maxsize=2048)
    stop_event = threading.Event()

    def _worker_producer(records_slice: List[FileRecord]):
        for rec in records_slice:
            if stop_event.is_set() or (stop_check and stop_check()):
                break
            try:
                with Image.open(rec.path) as img:
                    img.draft("RGB", (IMG_SIZE, IMG_SIZE))
                    rgb = img.convert("RGB").resize((IMG_SIZE, IMG_SIZE), Image.Resampling.BILINEAR)
                    arr = np.asarray(rgb, dtype=np.uint8)
                    img_queue.put((rec, arr))
            except Exception:
                img_queue.put((rec, None))

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

    # Consumidor: Alimenta a GPU em blocos e agrupa commits no SQLite
    pending_cache_entries: List[Tuple[str, int, float, bytes]] = []
    batch_records: List[FileRecord] = []
    batch_images: List[np.ndarray] = []
    processed_count = 0
    last_progress_time = 0.0

    try:
        with torch.inference_mode():
            while processed_count < uncached_total:
                if stop_check and stop_check():
                    stop_event.set()
                    break

                try:
                    rec, arr = img_queue.get(timeout=0.05)
                    processed_count += 1
                    if arr is not None:
                        batch_records.append(rec)
                        batch_images.append(arr)

                    # Processa lote na GPU (inicia com 64 para resposta instantânea <30ms, depois escala para effective_batch)
                    target_batch = 64 if processed_count < 256 else effective_batch
                    if len(batch_records) >= target_batch or processed_count == uncached_total:
                        if batch_images:
                            stacked = np.stack(batch_images)
                            # Permuta de (B, H, W, C) para (B, C, H, W) preservando os 3 canais de cor (RGB)
                            t_batch = torch.from_numpy(stacked).permute(0, 3, 1, 2).to(device=device, dtype=dtype) / 255.0
                            dct_3d = torch.matmul(dct_mat, torch.matmul(t_batch, dct_mat_t))
                            low_freq = dct_3d[:, :, :LOW_FREQ_SIZE, :LOW_FREQ_SIZE].clone()
                            # Em perceptual hashing (pHash), o coeficiente DC (0,0) representa a luminância média da imagem.
                            # Zeramos o DC em cada canal RGB para que a comparação seja puramente baseada em frequências espaciais AC (texturas, bordas, padrões).
                            low_freq[:, :, 0, 0] = 0.0
                            # Equalização espectral (whitening): contrabalanceia o decaimento 1/f^2 natural das imagens,
                            # impedindo que fundos estáticos (paredes, pisos) dominem a similaridade em detrimento do sujeito
                            weighted = low_freq * spectral_weight
                            flat = weighted.reshape(weighted.shape[0], -1)

                            norms = torch.norm(flat, p=2, dim=1, keepdim=True).clamp(min=1e-6)
                            normalized = flat / norms

                            all_embeddings_list.append(normalized)
                            valid_records.extend(batch_records)

                            norm_cpu_np = normalized.cpu().numpy()
                            for b_idx, b_rec in enumerate(batch_records):
                                pending_cache_entries.append((
                                    str(b_rec.path),
                                    b_rec.size,
                                    b_rec.mtime,
                                    norm_cpu_np[b_idx:b_idx + 1].tobytes()
                                ))

                            # Gravação em lote massivo no SQLite (WAL): reduz transações de I/O em até 8x
                            if len(pending_cache_entries) >= 1024 or processed_count == uncached_total:
                                cache.save_image_batch(pending_cache_entries)
                                pending_cache_entries.clear()

                            batch_records.clear()
                            batch_images.clear()

                    now = time.time()
                    if progress_callback and (now - last_progress_time >= 0.08 or processed_count == uncached_total):
                        last_progress_time = now
                        done_count = cached_count + processed_count
                        progress_callback(
                            f"Processing images ({done_count:,}/{total:,})...",
                            done_count,
                            total
                        )
                except queue.Empty:
                    if all(not t.is_alive() for t in worker_threads) and img_queue.empty():
                        break
    finally:
        stop_event.set()
        if pending_cache_entries and not (stop_check and stop_check()):
            try:
                cache.save_image_batch(pending_cache_entries)
            except Exception:
                pass
        for t in worker_threads:
            t.join(timeout=0.1)

    if not all_embeddings_list:
        return [], torch.empty((0, DIM_SIZE), device=device, dtype=dtype)

    with torch.inference_mode():
        all_embeddings = torch.cat(all_embeddings_list, dim=0)
    return valid_records, all_embeddings
