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


_BMM_CLIQUE_MAX_K = 96


def _resolve_clique_from_precomputed(
    sub_adj: np.ndarray,
    sub_sims: np.ndarray,
    cands: List[int],
    idx_val: int,
    keep_pos: List[int],
) -> Tuple[List[int], float]:
    """Resolve Complete Linkage em matriz de adjacência/similaridade já computada na GPU sem roundtrips CUDA."""
    k_members = len(cands)
    lp = cands.index(idx_val) if idx_val in cands else -1
    if lp > 0:
        cands[0], cands[lp] = cands[lp], cands[0]
        keep_pos[0], keep_pos[lp] = keep_pos[lp], keep_pos[0]

    kp = np.asarray(keep_pos, dtype=np.intp)
    local_adj = sub_adj[kp[:, None], kp]
    local_sims = sub_sims[kp[:, None], kp]

    if local_adj.all():
        diag_sum = float(np.trace(local_sims, dtype=np.float32))
        total_sum = float(local_sims.sum(dtype=np.float32))
        avg_sim = (total_sum - diag_sum) / (k_members * (k_members - 1)) if k_members > 1 else 1.0
        return cands, min(1.0, max(0.0, round(avg_sim, 4)))

    in_clique = np.zeros(k_members, dtype=bool)
    in_clique[0] = True
    pool = local_adj[0].copy()
    pool[0] = False

    while pool.any():
        pool_idx = np.flatnonzero(pool)
        if pool_idx.size == 1:
            next_node = int(pool_idx[0])
        else:
            deg = local_adj[pool_idx[:, None], pool_idx].sum(axis=1)
            next_node = int(pool_idx[int(np.argmax(deg))])
        in_clique[next_node] = True
        pool &= local_adj[next_node]
        pool[in_clique] = False

    chosen = np.flatnonzero(in_clique)
    k_clique = chosen.size
    if k_clique <= 1:
        return [], 1.0

    clique_sims = local_sims[chosen[:, None], chosen]
    diag_sum = float(np.trace(clique_sims, dtype=np.float32))
    total_sum = float(clique_sims.sum(dtype=np.float32))
    avg_sim = (total_sum - diag_sum) / (k_clique * (k_clique - 1))
    return [cands[int(i)] for i in chosen], min(1.0, max(0.0, round(avg_sim, 4)))


def _precompute_batch_bmm_matrices(
    cands_by_row: List[List[int]],
    embeddings: torch.Tensor,
    similarity_threshold: float,
) -> Tuple[Dict[int, np.ndarray], Dict[int, np.ndarray]]:
    """Pré-calcula em lote via Tensor Core BMM todas as matrizes KxK (3 <= K <= 96) do batch."""
    buckets: Tuple[List[int], List[int]] = ([], [])
    for b, cl in enumerate(cands_by_row):
        lc = len(cl)
        if 3 <= lc <= 24:
            buckets[0].append(b)
        elif 24 < lc <= _BMM_CLIQUE_MAX_K:
            buckets[1].append(b)

    pre_adj: Dict[int, np.ndarray] = {}
    pre_sims: Dict[int, np.ndarray] = {}
    device = embeddings.device

    for b_list in buckets:
        if not b_list:
            continue
        # Sub-chunks limitam pico de memória em placas de 8 GB mesmo se K_max = 96
        for start in range(0, len(b_list), 256):
            chunk_b = b_list[start:start + 256]
            k_max = max(len(cands_by_row[b]) for b in chunk_b)
            pad_np = np.zeros((len(chunk_b), k_max), dtype=np.int64)
            for slot, b in enumerate(chunk_b):
                cl = cands_by_row[b]
                pad_np[slot, :len(cl)] = cl

            pad_t = torch.from_numpy(pad_np).to(device)
            pad_embs = embeddings[pad_t]
            sims_t = torch.bmm(pad_embs, pad_embs.transpose(1, 2))
            adj_np = (sims_t >= similarity_threshold).cpu().numpy()
            sims_np = sims_t.cpu().numpy()

            for slot, b in enumerate(chunk_b):
                ln = len(cands_by_row[b])
                pre_adj[b] = adj_np[slot, :ln, :ln]
                pre_sims[b] = sims_np[slot, :ln, :ln]

    return pre_adj, pre_sims


def _resolve_multi_member_clique(
    cands: List[int],
    idx_val: int,
    embeddings: torch.Tensor,
    similarity_threshold: float,
) -> Tuple[List[int], float]:
    """Extrai na GPU o subgrupo coeso máximo (Complete Linkage) contendo o líder idx_val."""
    ordered = list(cands)
    if idx_val in ordered:
        pos_leader = ordered.index(idx_val)
        if pos_leader != 0:
            ordered[0], ordered[pos_leader] = ordered[pos_leader], ordered[0]

    k_members = len(ordered)
    cand_t = torch.tensor(ordered, dtype=torch.int64, device=embeddings.device)
    cand_embs = embeddings[cand_t]
    cand_sims = torch.matmul(cand_embs, cand_embs.t())

    if (cand_sims.min(dim=1).values >= similarity_threshold).all():
        cand_sims_f = cand_sims.float()
        diag_sum = cand_sims_f.diagonal().sum().item()
        avg_sim = (cand_sims_f.sum().item() - diag_sum) / (k_members * (k_members - 1)) if k_members > 1 else 1.0
        return ordered, min(1.0, max(0.0, round(float(avg_sim), 4)))

    clique_mask = _fast_gpu_greedy_clique(cand_sims, similarity_threshold)
    clique_indices = cand_t[clique_mask]
    k_clique = clique_indices.numel()
    if k_clique <= 1:
        return [], 1.0

    sub_sims_f = cand_sims[clique_mask][:, clique_mask].float()
    diag_sum = sub_sims_f.diagonal().sum().item()
    avg_sim = (sub_sims_f.sum().item() - diag_sum) / (k_clique * (k_clique - 1))
    members_list = clique_indices.cpu().tolist()
    if idx_val in members_list:
        members_list.remove(idx_val)
        members_list.insert(0, idx_val)
    return members_list, min(1.0, max(0.0, round(float(avg_sim), 4)))


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
        elem_bytes = max(2, embeddings.element_size())
        max_chunk = 1024 if device.type == "cpu" else 4096
        chunk_size = max(64, min(max_chunk, int(target_chunk_bytes / (n * elem_bytes))))
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
        sorted_perm = torch.argsort(composite_keys, descending=True)
        sorted_order = candidates[sorted_perm]
        sorted_degrees_cpu = cand_degrees[sorted_perm].cpu().tolist()

        # Mantém índice em CPU para pular membros já alocados com zero sincronização CUDA
        sorted_cpu = sorted_order.cpu().tolist()
        unassigned_cpu = bytearray(n)
        for c in sorted_cpu:
            unassigned_cpu[c] = 1

        # Compacta pool ativo na GPU descartando imediatamente itens de grau 0
        active_indices = sorted_order
        active_embs = embeddings[active_indices]
        active_durations = durations[active_indices] if durations is not None else None
        active_remaining = total_candidates

        # 4. Formação de grupos por Cohesive Clique Clustering em lote nos Tensor Cores da GPU
        duplicate_groups: List[DuplicateGroup] = []
        group_id = 1
        processed_candidates = 0
        pos = 0
        edge_budget = 250_000
        item_label = "images" if mode == ScanMode.IMAGES else ("videos" if mode == ScanMode.VIDEOS else "files")

        while pos < total_candidates:
            if stop_check and stop_check():
                return []

            if active_remaining > 2048 and active_remaining <= (active_indices.numel() * 3) // 4:
                active_indices = torch.nonzero(unassigned).squeeze(1)
                active_embs = embeddings[active_indices]
                if durations is not None:
                    active_durations = durations[active_indices]

            active_count = max(1, active_indices.numel())
            max_batch_rows = max(64, min(512, int(target_chunk_bytes / (active_count * 4))))

            batch_leaders: List[int] = []
            est_edges = 0
            while pos < total_candidates and len(batch_leaders) < max_batch_rows:
                idx_val = sorted_cpu[pos]
                deg_val = max(1, sorted_degrees_cpu[pos])
                pos += 1
                if not unassigned_cpu[idx_val]:
                    continue
                if batch_leaders and (est_edges + deg_val) > edge_budget:
                    pos -= 1
                    break
                batch_leaders.append(idx_val)
                est_edges += deg_val + 1

            if not batch_leaders:
                break

            # Único GEMM Tensor Core [B, D] @ [D, M] e transferência D2H para todo o lote de líderes
            lead_t = torch.tensor(batch_leaders, dtype=torch.int64, device=device)
            batch_sims = torch.matmul(embeddings[lead_t], active_embs.t())
            batch_masks = unassigned[active_indices].unsqueeze(0) & (batch_sims >= similarity_threshold)

            if active_durations is not None:
                lead_durs = durations[lead_t].unsqueeze(1)
                act_durs = active_durations.unsqueeze(0)
                has_dur = (lead_durs > 0) & (act_durs > 0)
                dur_max = torch.maximum(lead_durs, act_durs)
                dur_diff = torch.abs(lead_durs - act_durs) / torch.clamp(dur_max, min=1e-5)
                batch_masks = batch_masks & ((~has_dur) | (dur_diff <= 0.08))

            nz_rows, nz_cols = torch.nonzero(batch_masks, as_tuple=True)
            nz_rec = active_indices[nz_cols]
            nz_scores = batch_sims[nz_rows, nz_cols]

            rows_cpu = nz_rows.cpu().tolist()
            recs_cpu = nz_rec.cpu().tolist()
            scores_cpu = nz_scores.cpu().tolist()

            cands_by_row: List[List[int]] = [[] for _ in range(len(batch_leaders))]
            scores_by_row: List[List[float]] = [[] for _ in range(len(batch_leaders))]
            for r_b, rec_i, sc in zip(rows_cpu, recs_cpu, scores_cpu):
                cands_by_row[r_b].append(rec_i)
                scores_by_row[r_b].append(sc)

            pre_adj, pre_sims = _precompute_batch_bmm_matrices(
                cands_by_row, embeddings, similarity_threshold
            )

            batch_claimed: List[int] = []
            for b, idx_val in enumerate(batch_leaders):
                if stop_check and stop_check():
                    return []
                if not unassigned_cpu[idx_val]:
                    continue

                cands: List[int] = []
                c_scores: List[float] = []
                keep_pos: List[int] = []
                for pos_i, (r_i, sc) in enumerate(zip(cands_by_row[b], scores_by_row[b])):
                    if unassigned_cpu[r_i]:
                        cands.append(r_i)
                        c_scores.append(sc)
                        keep_pos.append(pos_i)

                k_members = len(cands)
                if k_members <= 1:
                    unassigned_cpu[idx_val] = 0
                    batch_claimed.append(idx_val)
                    active_remaining -= 1
                    processed_candidates += 1
                elif k_members == 2:
                    # Pares (k=2) já tiveram similaridade mútua validada pelo GEMM em lote
                    if cands[0] == idx_val:
                        other, sc_val = cands[1], c_scores[1]
                    else:
                        other, sc_val = cands[0], c_scores[0]
                    duplicate_groups.append(DuplicateGroup(
                        group_id=group_id,
                        files=[records[idx_val], records[other]],
                        similarity=min(1.0, max(0.0, round(float(sc_val), 4))),
                        kind=mode
                    ))
                    group_id += 1
                    unassigned_cpu[idx_val] = 0
                    unassigned_cpu[other] = 0
                    batch_claimed.extend((idx_val, other))
                    active_remaining -= 2
                    processed_candidates += 2
                else:
                    if b in pre_adj:
                        members_list, clamped_sim = _resolve_clique_from_precomputed(
                            pre_adj[b], pre_sims[b], cands, idx_val, keep_pos
                        )
                    else:
                        members_list, clamped_sim = _resolve_multi_member_clique(
                            cands, idx_val, embeddings, similarity_threshold
                        )
                    if len(members_list) > 1:
                        duplicate_groups.append(DuplicateGroup(
                            group_id=group_id,
                            files=[records[m] for m in members_list],
                            similarity=clamped_sim,
                            kind=mode
                        ))
                        group_id += 1
                        for m in members_list:
                            unassigned_cpu[m] = 0
                        batch_claimed.extend(members_list)
                        removed_count = len(members_list)
                        active_remaining -= removed_count
                        processed_candidates += removed_count
                    else:
                        unassigned_cpu[idx_val] = 0
                        batch_claimed.append(idx_val)
                        active_remaining -= 1
                        processed_candidates += 1

            if batch_claimed:
                unassigned[torch.tensor(batch_claimed, dtype=torch.int64, device=device)] = False

            now = time.time()
            if progress_callback and (now - last_progress_time >= 0.08 or processed_candidates >= total_candidates):
                last_progress_time = now
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


def filter_reference_groups(groups: List[DuplicateGroup]) -> List[DuplicateGroup]:
    """
    Filtra e calibra os grupos de duplicatas sob a política estrita de Pastas de Referência:
    - Se o grupo possui arquivos de Referência:
      1. Descarta o grupo caso NÃO existam arquivos de pastas normais (não-referência) para limpar.
         (Um grupo com apenas arquivos de referência não permite exclusão e gera apenas poluição visual).
      2. Preserva apenas UM arquivo representativo da pasta de referência (o canônico: menor caminho / mais antigo).
         (Elimina a duplicação interna redundante da própria pasta de referência nos resultados).
      3. Monta o grupo como: 1 arquivo mestre de referência + todas as duplicatas das demais pastas normais.
    - Se o grupo NÃO possui arquivos de Referência (ex: duplicatas entre pastas normais):
      Mantém o grupo intacto desde que possua 2 ou mais arquivos.
    """
    filtered_groups: List[DuplicateGroup] = []
    new_group_id = 1

    for g in groups:
        ref_files = [f for f in g.files if f.is_reference]
        non_ref_files = [f for f in g.files if not f.is_reference]

        if ref_files:
            # Se não há nenhum arquivo fora da referência, o grupo é puramente interno e inerte
            if not non_ref_files:
                continue

            # Elege o arquivo canônico de referência (menor comprimento de caminho e data mais antiga)
            canonical_ref = min(ref_files, key=lambda f: (len(str(f.path)), f.mtime))

            # Grupo definitivo: 1 arquivo mestre travado + arquivos duplicados das pastas de destino
            new_files = [canonical_ref] + non_ref_files
            filtered_groups.append(DuplicateGroup(
                group_id=new_group_id,
                files=new_files,
                similarity=g.similarity,
                kind=g.kind
            ))
            new_group_id += 1
        else:
            if len(g.files) > 1:
                filtered_groups.append(DuplicateGroup(
                    group_id=new_group_id,
                    files=g.files,
                    similarity=g.similarity,
                    kind=g.kind
                ))
                new_group_id += 1

    return filtered_groups
