"""
Agrupador e Comparador Matricial por Similaridade na GPU.
Performs upper-triangular tensor multiplication in VRAM with single-pass sparse edge extraction
(eliminating redundant Pass-2 GEMMs on sparse libraries) and complete-linkage clique clustering.
Strictly enforces Reference Folder (Ref) policies and universal CPU fallback parity.
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
# Max undirected edges cached from Pass 1 for zero-GEMM Pass 2 (~40 MB RAM at 2M edges).
# Ultra-dense degenerate datasets exceeding this budget fall back seamlessly to Pass-2 batched GEMM.
_SPARSE_EDGE_BUDGET = 2_000_000


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
    """Pre-compute in batch via Tensor Core BMM all KxK matrices (3 <= K <= _BMM_CLIQUE_MAX_K) for the batch."""
    buckets: Tuple[List[int], List[int]] = ([], [])
    for b, cl in enumerate(cands_by_row):
        lc = len(cl)
        if 3 <= lc <= min(24, _BMM_CLIQUE_MAX_K):
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


def _filter_pairs_by_duration(
    row_idx: torch.Tensor,
    col_idx: torch.Tensor,
    durations: torch.Tensor,
) -> torch.Tensor:
    """Apply <= 8% video duration guard ONLY on candidate pairs that already passed similarity threshold."""
    d_a = durations[row_idx]
    d_b = durations[col_idx]
    has_dur = (d_a > 0) & (d_b > 0)
    dur_max = torch.maximum(d_a, d_b)
    dur_diff = torch.abs(d_a - d_b) / torch.clamp(dur_max, min=1e-5)
    return (~has_dur) | (dur_diff <= 0.08)


def _collapse_exact_embeddings(
    records: List[FileRecord],
    embeddings: torch.Tensor,
    mode: ScanMode,
) -> Optional[Tuple[List[int], List[List[int]]]]:
    """
    Collapse bitwise-identical embeddings into single representatives before N x N GPU GEMM.
    Mathematically equivalent to uncollapsed Complete Linkage (identical vectors have dot=1.0
    and identical dot products with all other vectors), turning 10x duplicate libraries from
    O(N^2) into O((N/10)^2) = 100x fewer FLOPs and edges.
    Returns (rep_indices, buckets) if any exact duplicates exist, else None.
    """
    n, dim = embeddings.shape
    if n < 2:
        return None
    elem_size = embeddings.element_size()
    if elem_size not in (2, 4):
        return None

    device = embeddings.device
    canon = embeddings + 0.0  # Canonicalize -0.0 -> +0.0 for exact bitwise hashing
    raw = canon.view(torch.int16 if elem_size == 2 else torch.int32).to(torch.int64)

    rng = np.random.default_rng(0x5EED1234)
    w_np = rng.integers(1, (1 << 62) - 1, size=(dim, 2), dtype=np.int64)
    w_t = torch.from_numpy(w_np).to(device)
    # 128-bit universal polynomial hash computed in a single GPU pass
    h0 = (raw * w_t[:, 0]).sum(dim=1)
    h1 = (raw * w_t[:, 1]).sum(dim=1)
    hashes_cpu = torch.stack((h0, h1), dim=1).cpu().numpy()

    use_dur = (mode == ScanMode.VIDEOS)
    key_to_bucket: Dict[Tuple, int] = {}
    buckets: List[List[int]] = []

    if use_dur:
        for i in range(n):
            dur_val = round(float(getattr(records[i], "duration", None) or 0.0), 3)
            key = (int(hashes_cpu[i, 0]), int(hashes_cpu[i, 1]), dur_val)
            b_idx = key_to_bucket.get(key)
            if b_idx is None:
                key_to_bucket[key] = len(buckets)
                buckets.append([i])
            else:
                buckets[b_idx].append(i)
    else:
        for i in range(n):
            key = (int(hashes_cpu[i, 0]), int(hashes_cpu[i, 1]))
            b_idx = key_to_bucket.get(key)
            if b_idx is None:
                key_to_bucket[key] = len(buckets)
                buckets.append([i])
            else:
                buckets[b_idx].append(i)

    if len(buckets) == n:
        return None

    # Exact GPU verification across all candidate duplicate pairs (eliminates any hash collision)
    check_reps: List[int] = []
    check_mems: List[int] = []
    check_bidx: List[int] = []
    for b_idx, members in enumerate(buckets):
        if len(members) > 1:
            rep = members[0]
            for m in members[1:]:
                check_reps.append(rep)
                check_mems.append(m)
                check_bidx.append(b_idx)

    if check_mems:
        reps_t = torch.tensor(check_reps, dtype=torch.int64, device=device)
        mems_t = torch.tensor(check_mems, dtype=torch.int64, device=device)
        eq_mask = (canon[reps_t] == canon[mems_t]).all(dim=1).cpu().tolist()
        if not all(eq_mask):
            # Split any rare hash collision into its own singleton bucket
            mismatched_by_b: Dict[int, Set[int]] = {}
            for ok, b_idx, m in zip(eq_mask, check_bidx, check_mems):
                if not ok:
                    mismatched_by_b.setdefault(b_idx, set()).add(m)
            for b_idx, bad_set in mismatched_by_b.items():
                buckets[b_idx] = [m for m in buckets[b_idx] if m not in bad_set]
                for m in bad_set:
                    buckets.append([m])

    if len(buckets) == n:
        return None

    # Order each bucket so any Reference file comes first (preserves Reference leader invariant)
    rep_indices: List[int] = []
    for members in buckets:
        if len(members) > 1:
            members.sort(key=lambda idx: not getattr(records[idx], "is_reference", False))
        rep_indices.append(members[0])

    return rep_indices, buckets


def _expand_collapsed_groups(
    rep_groups: List[DuplicateGroup],
    records: List[FileRecord],
    unique_embs: torch.Tensor,
    buckets: List[List[int]],
    mode: ScanMode,
) -> List[DuplicateGroup]:
    """Expand clustered unique-representative groups back to all original FileRecords with exact avg_sim."""
    expanded: List[DuplicateGroup] = []
    claimed_buckets: Set[int] = set()
    next_gid = 1

    # Map each unique representative FileRecord object id -> bucket index
    rep_to_bucket: Dict[int, int] = {id(records[members[0]]): b_idx for b_idx, members in enumerate(buckets)}

    for grp in rep_groups:
        grp_b_indices: List[int] = []
        for f in grp.files:
            b_idx = rep_to_bucket[id(f)]
            grp_b_indices.append(b_idx)
            claimed_buckets.add(b_idx)

        all_files: List[FileRecord] = []
        weights: List[int] = []
        for b_idx in grp_b_indices:
            b_members = buckets[b_idx]
            weights.append(len(b_members))
            for orig_idx in b_members:
                all_files.append(records[orig_idx])

        # Compute exact weighted pairwise average similarity across all expanded members
        total_w = sum(weights)
        if all(w == 1 for w in weights):
            avg_sim = grp.similarity
        else:
            b_t = torch.tensor(grp_b_indices, dtype=torch.int64, device=unique_embs.device)
            sub_embs = unique_embs[b_t]
            sim_mat = torch.matmul(sub_embs, sub_embs.t()).float().cpu().numpy()
            w_arr = np.asarray(weights, dtype=np.float64)
            # Off-diagonal inter-bucket sum + intra-bucket identical pairs (similarity = 1.0)
            np.fill_diagonal(sim_mat, 0.0)
            inter_sum = float((w_arr[:, None] * w_arr[None, :] * sim_mat).sum())
            intra_sum = float((w_arr * (w_arr - 1.0)).sum())
            avg_sim = (inter_sum + intra_sum) / (total_w * (total_w - 1.0)) if total_w > 1 else 1.0
            avg_sim = min(1.0, max(0.0, round(avg_sim, 4)))

        expanded.append(DuplicateGroup(
            group_id=next_gid,
            files=all_files,
            similarity=avg_sim,
            kind=mode,
        ))
        next_gid += 1

    # Emit exact-duplicate groups for multi-copy buckets with no distinct visual neighbors
    for b_idx, members in enumerate(buckets):
        if b_idx not in claimed_buckets and len(members) >= 2:
            expanded.append(DuplicateGroup(
                group_id=next_gid,
                files=[records[orig_idx] for orig_idx in members],
                similarity=1.0,
                kind=mode,
            ))
            next_gid += 1

    return expanded


def cluster_by_similarity(
    records: List[FileRecord],
    embeddings: torch.Tensor,
    similarity_threshold: float,
    mode: ScanMode,
    stop_check: Optional[Callable[[], bool]] = None,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    _allow_collapse: bool = True,
    _node_weights: Optional[List[int]] = None,
) -> List[DuplicateGroup]:
    """
    Calcula similaridade por cosseno em blocos GEMM triangulares superiores na GPU
    e agrupa duplicatas via Cohesive Clique Clustering (Complete Linkage).
    - Colapsa embeddings idênticos antes do GEMM (até 100x menos FLOPs/arestas em bibliotecas com cópias).
    - Passada 1 calcula apenas o triângulo superior (2x menos FLOPs/VRAM) e aplica
      o filtro de duração apenas nos pares candidatos (nonzero), capturando arestas esparsas.
    - Para bibliotecas normais (<= 2M arestas), resolve todos os cliques diretamente das
      arestas da Passada 1 sem precisar repetir o GEMM N×N na Passada 2.
    - Mantém fallback automático para Passada 2 em lote em grafos ultradensos.
    """
    n = len(records)
    if n <= 1 or embeddings.shape[0] != n:
        return []
    if stop_check and stop_check():
        return []

    device = embeddings.device

    with torch.inference_mode():
        # 0. Exact-embedding collapse (O(N) GPU/CPU universal hash + exact verification)
        if _allow_collapse:
            collapsed = _collapse_exact_embeddings(records, embeddings, mode)
            if collapsed is not None:
                rep_indices, buckets = collapsed
                rep_t = torch.tensor(rep_indices, dtype=torch.int64, device=device)
                unique_embs = embeddings[rep_t]
                unique_recs = [records[idx] for idx in rep_indices]
                weights = [len(b) for b in buckets]
                # If Stage 3 spilled raw embeddings to CPU FP16 RAM to protect 8-12 GB VRAM,
                # check if the collapsed unique matrix now fits on CUDA for full Tensor Core GEMM!
                if unique_embs.device.type == "cpu":
                    from dupeclean.core.hardware import get_compute_device
                    c_dev, c_dtype = get_compute_device()
                    if c_dev == "cuda":
                        try:
                            free_vram, _ = torch.cuda.mem_get_info(0)
                            if unique_embs.numel() * 2 <= int(free_vram * 0.45):
                                unique_embs = unique_embs.to(device="cuda", dtype=c_dtype)
                        except Exception:
                            pass
                rep_groups = cluster_by_similarity(
                    records=unique_recs,
                    embeddings=unique_embs,
                    similarity_threshold=similarity_threshold,
                    mode=mode,
                    stop_check=stop_check,
                    progress_callback=progress_callback,
                    _allow_collapse=False,
                    _node_weights=weights,
                )
                if stop_check and stop_check():
                    return []
                expanded = _expand_collapsed_groups(rep_groups, records, unique_embs, buckets, mode)
                if progress_callback and expanded:
                    item_label = "images" if mode == ScanMode.IMAGES else ("videos" if mode == ScanMode.VIDEOS else "files")
                    progress_callback(
                        f"Grouping similar {item_label} ({n:,}/{n:,})...",
                        n,
                        n,
                    )
                return expanded

        # Ensure spilled CPU FP16 embeddings either promote to CUDA (if fitting) or cast to FP32 for CPU matmul
        if embeddings.device.type == "cpu" and embeddings.dtype == torch.float16:
            from dupeclean.core.hardware import get_compute_device
            c_dev, c_dtype = get_compute_device()
            promoted = False
            if c_dev == "cuda":
                try:
                    free_vram, _ = torch.cuda.mem_get_info(0)
                    if embeddings.numel() * 2 <= int(free_vram * 0.45):
                        embeddings = embeddings.to(device="cuda", dtype=c_dtype)
                        device = embeddings.device
                        promoted = True
                except Exception:
                    pass
            if not promoted:
                embeddings = embeddings.float()
        # 1. Duration vector for GPU video filtering
        durations = None
        if mode == ScanMode.VIDEOS:
            dur_vals = [getattr(r, "duration", None) or 0.0 for r in records]
            durations = torch.tensor(dur_vals, device=device, dtype=torch.float32)

        # 2. Upper-triangular connectivity & sparse edge extraction (GEMM) on GPU/CPU
        from dupeclean.core.hardware import get_optimal_gpu_budgets
        budgets = get_optimal_gpu_budgets()
        target_chunk_bytes = budgets.get("target_chunk_bytes", 256 * 1024 * 1024)
        elem_bytes = max(2, embeddings.element_size())
        max_chunk = 1024 if device.type == "cpu" else 4096
        chunk_size = max(64, min(max_chunk, int(target_chunk_bytes / (n * elem_bytes))))
        emb_t = embeddings.t().contiguous()

        degrees = torch.zeros(n, dtype=torch.int32, device=device)
        w_tensor = (
            torch.tensor(_node_weights, dtype=torch.int32, device=device)
            if _node_weights is not None
            else None
        )
        unassigned = torch.ones(n, dtype=torch.bool, device=device)

        keep_sparse_edges = True
        edge_chunks_u: List[np.ndarray] = []
        edge_chunks_v: List[np.ndarray] = []
        edge_chunks_s: List[np.ndarray] = []
        total_sparse_edges = 0

        last_progress_time = 0.0

        for i in range(0, n, chunk_size):
            if stop_check and stop_check():
                return []
            end_i = min(i + chunk_size, n)
            rows_in_chunk = end_i - i
            chunk = embeddings[i:end_i]

            # Upper-triangular GEMM: only compute columns [i .. n-1] (cuts FLOPs & VRAM bandwidth in half!)
            sim_block = torch.matmul(chunk, emb_t[:, i:])
            mask = sim_block >= similarity_threshold

            # Mask out lower triangle and diagonal within the leading square block [0:rows_in_chunk, 0:rows_in_chunk]
            mask[:, :rows_in_chunk].triu_(diagonal=1)

            nz_r, nz_c = torch.nonzero(mask, as_tuple=True)
            if nz_r.numel() > 0:
                global_r = nz_r + i
                global_c = nz_c + i

                if durations is not None:
                    dur_ok = _filter_pairs_by_duration(global_r, global_c, durations)
                    if not dur_ok.all():
                        nz_r = nz_r[dur_ok]
                        nz_c = nz_c[dur_ok]
                        global_r = global_r[dur_ok]
                        global_c = global_c[dur_ok]

                edge_cnt = global_r.numel()
                if edge_cnt > 0:
                    if w_tensor is not None:
                        degrees.index_add_(0, global_r, w_tensor[global_c])
                        degrees.index_add_(0, global_c, w_tensor[global_r])
                    else:
                        ones = torch.ones(edge_cnt, dtype=torch.int32, device=device)
                        degrees.index_add_(0, global_r, ones)
                        degrees.index_add_(0, global_c, ones)

                    if keep_sparse_edges:
                        if total_sparse_edges + edge_cnt <= _SPARSE_EDGE_BUDGET:
                            nz_scores = sim_block[nz_r, nz_c]
                            edge_chunks_u.append(global_r.cpu().numpy())
                            edge_chunks_v.append(global_c.cpu().numpy())
                            edge_chunks_s.append(nz_scores.float().cpu().numpy())
                            total_sparse_edges += edge_cnt
                        else:
                            keep_sparse_edges = False
                            edge_chunks_u.clear()
                            edge_chunks_v.clear()
                            edge_chunks_s.clear()

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
        if w_tensor is not None:
            cand_degrees = cand_degrees + (w_tensor[candidates] - 1)
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

        duplicate_groups: List[DuplicateGroup] = []
        group_id = 1
        processed_candidates = 0
        item_label = "images" if mode == ScanMode.IMAGES else ("videos" if mode == ScanMode.VIDEOS else "files")

        # 4A. Zero-GEMM Fast Path: resolve all cliques directly from Pass-1 sparse edges!
        if keep_sparse_edges and edge_chunks_u:
            all_u = np.concatenate(edge_chunks_u)
            all_v = np.concatenate(edge_chunks_v)
            all_s = np.concatenate(edge_chunks_s)
            edge_chunks_u.clear()
            edge_chunks_v.clear()
            edge_chunks_s.clear()

            adj_neighbors: Dict[int, List[int]] = {}
            adj_scores: Dict[int, List[float]] = {}
            for u_i, v_i, s_i in zip(all_u.tolist(), all_v.tolist(), all_s.tolist()):
                if u_i not in adj_neighbors:
                    adj_neighbors[u_i] = [u_i]
                    adj_scores[u_i] = [1.0]
                adj_neighbors[u_i].append(v_i)
                adj_scores[u_i].append(s_i)
                if v_i not in adj_neighbors:
                    adj_neighbors[v_i] = [v_i]
                    adj_scores[v_i] = [1.0]
                adj_neighbors[v_i].append(u_i)
                adj_scores[v_i].append(s_i)

            # Process leaders in batches so multi-member cliques (3 <= K <= _BMM_CLIQUE_MAX_K) still use GPU BMM
            pos = 0
            while pos < total_candidates:
                if stop_check and stop_check():
                    return []

                batch_leaders: List[int] = []
                cands_by_row: List[List[int]] = []
                scores_by_row: List[List[float]] = []

                while pos < total_candidates and len(batch_leaders) < 512:
                    idx_val = sorted_cpu[pos]
                    pos += 1
                    if not unassigned_cpu[idx_val]:
                        continue
                    nbrs = adj_neighbors.get(idx_val)
                    scrs = adj_scores.get(idx_val)
                    if not nbrs:
                        unassigned_cpu[idx_val] = 0
                        processed_candidates += 1
                        continue
                    # Snapshot currently unassigned candidates for this leader
                    row_cands = []
                    row_scores = []
                    for r_i, sc in zip(nbrs, scrs):
                        if unassigned_cpu[r_i]:
                            row_cands.append(r_i)
                            row_scores.append(sc)
                    if len(row_cands) <= 1:
                        unassigned_cpu[idx_val] = 0
                        processed_candidates += 1
                        continue
                    batch_leaders.append(idx_val)
                    cands_by_row.append(row_cands)
                    scores_by_row.append(row_scores)

                if not batch_leaders:
                    break

                pre_adj, pre_sims = _precompute_batch_bmm_matrices(
                    cands_by_row, embeddings, similarity_threshold
                )

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
                        processed_candidates += 1
                    elif k_members == 2:
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
                            processed_candidates += len(members_list)
                        else:
                            unassigned_cpu[idx_val] = 0
                            processed_candidates += 1

                now = time.time()
                if progress_callback and (now - last_progress_time >= 0.08 or processed_candidates >= total_candidates):
                    last_progress_time = now
                    progress_callback(
                        f"Grouping similar {item_label} ({min(processed_candidates, total_candidates):,}/{total_candidates:,})...",
                        min(processed_candidates, total_candidates),
                        total_candidates
                    )

            if progress_callback:
                progress_callback(
                    f"Grouping similar {item_label} ({total_candidates:,}/{total_candidates:,})...",
                    total_candidates,
                    total_candidates
                )
            return duplicate_groups

        # 4B. Dense Graph Fallback: batched Cohesive Clique Clustering on GPU Tensor Cores
        active_indices = sorted_order
        active_embs = embeddings[active_indices]
        active_remaining = total_candidates

        pos = 0
        edge_budget = 250_000

        while pos < total_candidates:
            if stop_check and stop_check():
                return []

            if active_remaining > 2048 and active_remaining <= (active_indices.numel() * 3) // 4:
                active_indices = torch.nonzero(unassigned).squeeze(1)
                active_embs = embeddings[active_indices]

            active_count = max(1, active_indices.numel())
            max_batch_rows = max(64, min(512, int(target_chunk_bytes / (active_count * 4))))

            batch_leaders = []
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

            lead_t = torch.tensor(batch_leaders, dtype=torch.int64, device=device)
            batch_sims = torch.matmul(embeddings[lead_t], active_embs.t())
            batch_masks = unassigned[active_indices].unsqueeze(0) & (batch_sims >= similarity_threshold)

            nz_rows, nz_cols = torch.nonzero(batch_masks, as_tuple=True)
            nz_rec = active_indices[nz_cols]
            if durations is not None and nz_rows.numel() > 0:
                lead_global = lead_t[nz_rows]
                dur_ok = _filter_pairs_by_duration(lead_global, nz_rec, durations)
                if not dur_ok.all():
                    nz_rows = nz_rows[dur_ok]
                    nz_cols = nz_cols[dur_ok]
                    nz_rec = nz_rec[dur_ok]

            nz_scores = batch_sims[nz_rows, nz_cols]

            rows_cpu = nz_rows.cpu().tolist()
            recs_cpu = nz_rec.cpu().tolist()
            scores_cpu = nz_scores.cpu().tolist()

            cands_by_row = [[] for _ in range(len(batch_leaders))]
            scores_by_row = [[] for _ in range(len(batch_leaders))]
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

                cands = []
                c_scores = []
                keep_pos = []
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
