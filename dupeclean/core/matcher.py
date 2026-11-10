"""
Agrupador e Comparador Matricial por Similaridade na GPU.
Executa multiplicação de tensores na VRAM e agrupa duplicatas por componentes conexos (Union-Find).
Aplica rigorosamente as diretrizes da Pasta de Referência (Ref).
"""

from pathlib import Path
from typing import List, Dict, Set, Optional, Callable, Tuple
import time
import torch
import numpy as np

from dupeclean.core.models import FileRecord, DuplicateGroup, ScanMode


def _fast_gpu_greedy_clique(cand_sims: torch.Tensor, thresh: float) -> torch.Tensor:
    """
    Calcula na GPU um clique maximal garantido onde TODOS os pares selecionados possuem
    similaridade mútua >= thresh (elimina rigorosamente falsos positivos e encadeamento indireto).
    """
    K = cand_sims.shape[0]
    if K <= 1:
        return torch.ones(K, dtype=torch.bool, device=cand_sims.device)

    adj = cand_sims >= thresh
    in_clique = torch.zeros(K, dtype=torch.bool, device=cand_sims.device)
    in_clique[0] = True
    pool = adj[0].clone()
    pool[0] = False

    while pool.any():
        pool_indices = torch.nonzero(pool).squeeze(1)
        if pool_indices.numel() == 1:
            next_node = pool_indices[0]
        else:
            sub_adj = adj[pool_indices][:, pool_indices]
            best_local_idx = torch.argmax(sub_adj.sum(dim=1))
            next_node = pool_indices[best_local_idx]

        in_clique[next_node] = True
        pool = pool & adj[next_node]
        pool[in_clique] = False

    return in_clique


def cluster_by_similarity(
    records: List[FileRecord],
    embeddings: torch.Tensor,
    similarity_threshold: float,
    mode: ScanMode,
    stop_check: Optional[Callable[[], bool]] = None,
    progress_callback: Optional[Callable[[str, int, int], None]] = None
) -> List[DuplicateGroup]:
    """
    Calcula similaridade por cosseno em blocos GEMM na GPU e agrupa duplicatas via
    algoritmo de Exemplar/Star Clustering de altíssimo rendimento diretamente na VRAM.
    Elimina qualquer gargalo de CPU em coleções massivas, garantindo telemetria contínua,
    alta ocupação de GPU (RTX Tensor Cores) e tolerância temporal em vídeos (<= 8%).
    """
    n = len(records)
    if n <= 1 or embeddings.shape[0] != n:
        return []
    if stop_check and stop_check():
        return []

    device = embeddings.device

    with torch.inference_mode():
        # 1. Vetor de durações para filtro GPU em vídeos
        durations = None
        if mode == ScanMode.VIDEOS:
            dur_vals = [getattr(r, "duration", None) or 0.0 for r in records]
            durations = torch.tensor(dur_vals, device=device, dtype=torch.float32)

        # 2. Análise de conectividade em lote (GEMM) na GPU
        # Dimensiona o chunk baseado no hardware para proteger placas de 8 GB e escalar até 32 GB
        from dupeclean.core.hardware import get_optimal_gpu_budgets
        budgets = get_optimal_gpu_budgets()
        target_chunk_bytes = budgets.get("target_chunk_bytes", 256 * 1024 * 1024)
        chunk_size = max(128, min(4096, int(target_chunk_bytes / (n * 2))))
        emb_t = embeddings.t()

        degrees = torch.zeros(n, dtype=torch.int32, device=device)
        unassigned = torch.ones(n, dtype=torch.bool, device=device)

        last_progress_time = 0.0

        for i in range(0, n, chunk_size):
            if stop_check and stop_check():
                return []
            end_i = min(i + chunk_size, n)
            chunk = embeddings[i:end_i]
            sim_block = torch.matmul(chunk, emb_t)
            mask = sim_block >= similarity_threshold

            if durations is not None:
                dur_row = durations[i:end_i].unsqueeze(1)
                dur_col = durations.unsqueeze(0)
                has_dur = (dur_row > 0) & (dur_col > 0)
                dur_max = torch.maximum(dur_row, dur_col)
                dur_diff = torch.abs(dur_row - dur_col) / torch.clamp(dur_max, min=1e-5)
                dur_valid = (~has_dur) | (dur_diff <= 0.08)
                mask = mask & dur_valid

            degrees[i:end_i] = mask.sum(dim=1).to(torch.int32) - 1

            now = time.time()
            if progress_callback and (now - last_progress_time >= 0.08 or end_i == n):
                last_progress_time = now
                progress_callback(f"Analyzing similarities ({end_i:,}/{n:,})...", end_i, n)

        # Arquivos com grau 0 não possuem nenhum par similar
        unassigned[degrees <= 0] = False
        candidates = torch.nonzero(unassigned).squeeze(1)
        total_candidates = candidates.numel()

        if total_candidates == 0:
            return []

        # 3. Ordenação inteligente de líderes:
        # Prioriza arquivos de Referência (is_reference=True) e maior densidade/grau de conectividade
        is_ref_arr = np.array([1 if getattr(r, "is_reference", False) else 0 for r in records], dtype=np.int32)
        is_ref_tensor = torch.from_numpy(is_ref_arr).to(device)

        cand_degrees = degrees[candidates]
        cand_refs = is_ref_tensor[candidates]
        composite_keys = (cand_refs.to(torch.int64) * 1_000_000_000) + cand_degrees.to(torch.int64)
        sorted_order = candidates[torch.argsort(composite_keys, descending=True)]

        # 4. Formação de grupos por Cohesive Clique Clustering acelerado na GPU
        duplicate_groups: List[DuplicateGroup] = []
        group_id = 1
        processed_candidates = 0

        for idx in sorted_order:
            if stop_check and stop_check():
                return []

            idx_val = idx.item()
            if not unassigned[idx_val]:
                continue

            sim_vec = torch.mv(embeddings, embeddings[idx_val])
            match_mask = unassigned & (sim_vec >= similarity_threshold)

            if durations is not None:
                lead_dur = durations[idx_val]
                if lead_dur > 0:
                    has_dur = (lead_dur > 0) & (durations > 0)
                    dur_max = torch.maximum(lead_dur, durations)
                    dur_diff = torch.abs(lead_dur - durations) / torch.clamp(dur_max, min=1e-5)
                    dur_valid = (~has_dur) | (dur_diff <= 0.08)
                    match_mask = match_mask & dur_valid

            cand_indices = torch.nonzero(match_mask).squeeze(1)
            k_members = cand_indices.numel()

            if k_members <= 1:
                unassigned[idx_val] = False
                processed_candidates += 1
                continue

            cand_embs = embeddings[cand_indices]
            cand_sims = torch.matmul(cand_embs, cand_embs.t())

            # 1. Verifica se todos os candidatos já formam um clique perfeito
            if (cand_sims.min(dim=1).values >= similarity_threshold).all():
                clique_indices = cand_indices
                k_clique = k_members
                avg_sim = (cand_sims.sum() - k_clique).item() / (k_clique * (k_clique - 1)) if k_clique > 1 else 1.0
            else:
                # 2. Extrai na GPU o clique maximal contendo o líder
                clique_mask = _fast_gpu_greedy_clique(cand_sims, similarity_threshold)
                clique_indices = cand_indices[clique_mask]
                k_clique = clique_indices.numel()
                if k_clique > 1:
                    sub_sims = cand_sims[clique_mask][:, clique_mask]
                    avg_sim = (sub_sims.sum() - k_clique).item() / (k_clique * (k_clique - 1))
                else:
                    clique_indices = torch.empty(0, dtype=torch.int64, device=device)
                    avg_sim = 1.0

            if clique_indices.numel() > 1:
                members_list = clique_indices.cpu().tolist()
                # Garante que o líder seja o primeiro elemento da lista
                if idx_val in members_list:
                    members_list.remove(idx_val)
                    members_list.insert(0, idx_val)

                group_files = [records[m] for m in members_list]

                duplicate_groups.append(DuplicateGroup(
                    group_id=group_id,
                    files=group_files,
                    similarity=round(float(avg_sim), 4),
                    kind=mode
                ))
                group_id += 1

                unassigned[clique_indices] = False
                processed_candidates += clique_indices.numel()
            else:
                unassigned[idx_val] = False
                processed_candidates += 1

            now = time.time()
            if progress_callback and (now - last_progress_time >= 0.08 or processed_candidates >= total_candidates):
                last_progress_time = now
                item_label = "photos" if mode == ScanMode.IMAGES else ("videos" if mode == ScanMode.VIDEOS else "files")
                progress_callback(
                    f"Grouping similar {item_label} ({min(processed_candidates, total_candidates):,}/{total_candidates:,})...",
                    min(processed_candidates, total_candidates),
                    total_candidates
                )

        return duplicate_groups


def apply_reference_rules(group: DuplicateGroup) -> Set[Path]:
    """
    Retorna o conjunto de arquivos que PODEM ser selecionados para exclusão,
    GARANTINDO que nenhum arquivo de pasta de Referência seja jamais marcado.
    
    Regra:
    - Se existirem arquivos de Referência no grupo:
      -> NENHUM arquivo de referência é selecionado.
      -> Todos os arquivos normais (não-referência) são marcados para exclusão.
    - Se NÃO existirem arquivos de Referência:
      -> Preserva o mais novo (ou maior) e marca o restante.
    """
    ref_files = [f for f in group.files if f.is_reference]
    non_ref_files = [f for f in group.files if not f.is_reference]

    selected_for_removal: Set[Path] = set()

    if ref_files:
        # Há pelo menos um arquivo de referência protegido!
        # Todos os não-referência podem ser limpos
        for f in non_ref_files:
            selected_for_removal.add(f.path)
    else:
        # Nenhum é referência: preserva o primeiro/mais novo
        sorted_files = sorted(group.files, key=lambda x: (x.size, x.mtime), reverse=True)
        # O primeiro é preservado, os outros são marcados
        for f in sorted_files[1:]:
            selected_for_removal.add(f.path)

    return selected_for_removal
