"""
Agrupador e Comparador Matricial por Similaridade na GPU.
Performs tensor multiplication in VRAM and groups duplicates by connected components (Union-Find).
Strictly enforces Reference Folder (Ref) policies.
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
    mutual similarity >= thresh (strictly eliminates false positives and indirect chaining).
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
    """Resolve Complete Linkage on an adjacency/similarity matrix already computed on GPU without CUDA roundtrips."""
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
    """Pre-compute in batch via Tensor Core BMM all KxK matrices (3 <= K <= 96) for the batch."""
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
        # Sub-chunks cap peak memory on 8 GB GPUs even if K_max = 96
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
    """Extract on GPU the maximal cohesive subgroup (Complete Linkage) containing leader idx_val."""
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
    ultra-high-throughput Exemplar/Star Clustering algorithm directly in VRAM.
    Eliminates any CPU bottleneck on massive collections, ensuring continuous telemetry,
    high GPU occupancy (RTX Tensor Cores), and video duration tolerance (<= 8%).
    """
    n = len(records)
    if n <= 1 or embeddings.shape[0] != n:
        return []
    if stop_check and stop_check():
        return []

    device = embeddings.device

    with torch.inference_mode():
        # 1. Duration vector for GPU video filtering
        durations = None
        if mode == ScanMode.VIDEOS:
            dur_vals = [getattr(r, "duration", None) or 0.0 for r in records]
            durations = torch.tensor(dur_vals, device=device, dtype=torch.float32)

        # 2. Batch connectivity analysis (GEMM) on GPU
        # Sizes the chunk based on hardware to protect 8 GB cards and scale up to 32 GB
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

        # Degree-0 files have no similar pairs
        unassigned[degrees <= 0] = False
        candidates = torch.nonzero(unassigned).squeeze(1)
        total_candidates = candidates.numel()

        if total_candidates == 0:
            return []

        # 3. Smart leader ordering:
        # Prioritizes Reference files (is_reference=True) and higher connectivity degree/density
        is_ref_arr = np.array([1 if getattr(r, "is_reference", False) else 0 for r in records], dtype=np.int32)
        is_ref_tensor = torch.from_numpy(is_ref_arr).to(device)

        cand_degrees = degrees[candidates]
        cand_refs = is_ref_tensor[candidates]
        composite_keys = (cand_refs.to(torch.int64) * 1_000_000_000) + cand_degrees.to(torch.int64)
        sorted_perm = torch.argsort(composite_keys, descending=True)
        sorted_order = candidates[sorted_perm]
        sorted_degrees_cpu = cand_degrees[sorted_perm].cpu().tolist()

        # Keep CPU index to skip already-allocated members with zero CUDA synchronization
        sorted_cpu = sorted_order.cpu().tolist()
        unassigned_cpu = bytearray(n)
        for c in sorted_cpu:
            unassigned_cpu[c] = 1

        # Compacta pool ativo na GPU descartando imediatamente itens de grau 0
        active_indices = sorted_order
        active_embs = embeddings[active_indices]
        active_durations = durations[active_indices] if durations is not None else None
        active_remaining = total_candidates

        # 4. Group formation via batched Cohesive Clique Clustering on GPU Tensor Cores
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

            # Single Tensor Core GEMM [B, D] @ [D, M] and D2H transfer for the entire leader batch
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
                    # Pairs (k=2) already had mutual similarity validated by batched GEMM
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
    Return the set of files that MAY be selected for deletion,
    GUARANTEEING that no file from a Reference folder is ever marked.
    
    Regra:
    - If Reference files exist in the group:
      -> NO reference file is selected.
      -> All normal (non-reference) files are marked for deletion.
    - If NO Reference files exist:
      -> Preserva o mais novo (ou maior) e marca o restante.
    """
    ref_files = [f for f in group.files if f.is_reference]
    non_ref_files = [f for f in group.files if not f.is_reference]

    selected_for_removal: Set[Path] = set()

    if ref_files:
        # At least one protected reference file exists!
        # All non-reference files can be cleaned
        for f in non_ref_files:
            selected_for_removal.add(f.path)
    else:
        # None is reference: preserve the first/newest
        sorted_files = sorted(group.files, key=lambda x: (x.size, x.mtime), reverse=True)
        # First is preserved, others are marked
        for f in sorted_files[1:]:
            selected_for_removal.add(f.path)

    return selected_for_removal


def filter_reference_groups(groups: List[DuplicateGroup]) -> List[DuplicateGroup]:
    """
    Filter and calibrate duplicate groups under the strict Reference Folder policy:
    - If the group has Reference files:
      1. Discard the group if NO normal (non-reference) files exist to clean.
         (A group with only reference files allows no deletion and only creates visual clutter.)
      2. Preserve only ONE representative file from the reference folder (canonical: shortest path / oldest).
         (Eliminates redundant internal duplication within the reference folder itself from results.)
      3. Assemble the group as: 1 master reference file + all duplicates from normal folders.
    - If the group has NO Reference files (e.g., duplicates across normal folders):
      Keep the group intact as long as it has 2 or more files.
    """
    filtered_groups: List[DuplicateGroup] = []
    new_group_id = 1

    for g in groups:
        ref_files = [f for f in g.files if f.is_reference]
        non_ref_files = [f for f in g.files if not f.is_reference]

        if ref_files:
            # If no file exists outside reference, the group is purely internal and inert
            if not non_ref_files:
                continue

            # Elect the canonical reference file (shortest path length and oldest timestamp)
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
