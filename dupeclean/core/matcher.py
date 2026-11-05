"""
Agrupador e Comparador Matricial por Similaridade na GPU.
Executa multiplicação de tensores na VRAM e agrupa duplicatas por componentes conexos (Union-Find).
Aplica rigorosamente as diretrizes da Pasta de Referência (Ref).
"""

from pathlib import Path
from typing import List, Dict, Set, Optional, Callable
import torch

from dupeclean.core.models import FileRecord, DuplicateGroup, ScanMode


class DisjointSet:
    """Estrutura Union-Find para agrupar pares similares em clusters."""
    def __init__(self, size: int):
        self.parent = list(range(size))

    def find(self, i: int) -> int:
        if self.parent[i] == i:
            return i
        self.parent[i] = self.find(self.parent[i])
        return self.parent[i]

    def union(self, i: int, j: int):
        root_i = self.find(i)
        root_j = self.find(j)
        if root_i != root_j:
            self.parent[root_i] = root_j


def cluster_by_similarity(
    records: List[FileRecord],
    embeddings: torch.Tensor,
    similarity_threshold: float,
    mode: ScanMode,
    stop_check: Optional[Callable[[], bool]] = None
) -> List[DuplicateGroup]:
    """
    Calcula similaridade por cosseno em blocos GEMM na GPU e agrupa duplicatas por Complete Linkage (cliques).
    Garante rigorosamente que TODOS os arquivos de um grupo possuam similaridade mútua >= limiar
    e tolerância temporal (duração <= 8% em vídeos), eliminando falsos positivos e encadeamento espúrio.
    """
    n = len(records)
    if n <= 1 or embeddings.shape[0] != n:
        return []
    if stop_check and stop_check():
        return []

    # Vetor de durações para filtro GPU em vídeos
    durations = None
    if mode == ScanMode.VIDEOS:
        dur_vals = [getattr(r, "duration", None) or 0.0 for r in records]
        durations = torch.tensor(dur_vals, device=embeddings.device, dtype=torch.float32)

    pair_similarities: Dict[tuple, float] = {}
    candidate_pairs: List[Tuple[float, int, int]] = []

    # Multiplicação matricial em blocos para evitar explosão de memória (40 GB em 140k itens)
    chunk_size = 2048
    emb_t = embeddings.t()  # [dim, n]

    for i in range(0, n, chunk_size):
        if stop_check and stop_check():
            return []

        end_i = min(i + chunk_size, n)
        chunk = embeddings[i:end_i]  # [chunk_len, dim]

        # sim_block: [chunk_len, n]
        sim_block = torch.matmul(chunk, emb_t)

        # Máscara estrita do triângulo superior (j > i_global) e acima do limiar
        row_indices = torch.arange(i, end_i, device=embeddings.device).unsqueeze(1)
        col_indices = torch.arange(n, device=embeddings.device).unsqueeze(0)
        mask = (col_indices > row_indices) & (sim_block >= similarity_threshold)

        # Filtro temporal estrito para vídeos: arquivos duplicados devem ter durações similares (<= 8%)
        if durations is not None:
            dur_row = durations[i:end_i].unsqueeze(1)
            dur_col = durations.unsqueeze(0)
            has_dur = (dur_row > 0) & (dur_col > 0)
            dur_max = torch.maximum(dur_row, dur_col)
            dur_diff = torch.abs(dur_row - dur_col) / torch.clamp(dur_max, min=1e-5)
            dur_valid = (~has_dur) | (dur_diff <= 0.08)
            mask = mask & dur_valid

        nonzero = torch.nonzero(mask)
        if nonzero.shape[0] > 0:
            r_locals = nonzero[:, 0]
            c_globals = nonzero[:, 1]
            scores = sim_block[r_locals, c_globals].cpu().float().numpy()
            r_globals = (r_locals + i).cpu().numpy()
            c_globals_np = c_globals.cpu().numpy()

            for k in range(len(scores)):
                if stop_check and stop_check():
                    return []
                g_i = int(r_globals[k])
                g_j = int(c_globals_np[k])
                sc = float(scores[k])
                pair_similarities[(min(g_i, g_j), max(g_i, g_j))] = sc
                candidate_pairs.append((sc, g_i, g_j))

    if stop_check and stop_check():
        return []

    # Agrupamento por Complete Linkage (Clique exato):
    # Ordena pares por similaridade decrescente para priorizar os pares mais próximos
    candidate_pairs.sort(key=lambda x: x[0], reverse=True)

    clusters: List[List[int]] = []
    item_to_cluster: Dict[int, List[int]] = {}

    for s, g_i, g_j in candidate_pairs:
        if stop_check and stop_check():
            return []

        c_i = item_to_cluster.get(g_i)
        c_j = item_to_cluster.get(g_j)

        if c_i is None and c_j is None:
            new_c = [g_i, g_j]
            clusters.append(new_c)
            item_to_cluster[g_i] = new_c
            item_to_cluster[g_j] = new_c
        elif c_i is not None and c_j is None:
            # g_j só é adicionado ao grupo se for similar a TODOS os membros já no grupo
            if all((min(g_j, m), max(g_j, m)) in pair_similarities for m in c_i):
                c_i.append(g_j)
                item_to_cluster[g_j] = c_i
        elif c_j is not None and c_i is None:
            # g_i só é adicionado ao grupo se for similar a TODOS os membros já no grupo
            if all((min(g_i, m), max(g_i, m)) in pair_similarities for m in c_j):
                c_j.append(g_i)
                item_to_cluster[g_i] = c_j
        elif c_i is not c_j:
            # Fusão de dois grupos: só mescla se TODOS os membros forem mutuamente similares
            if all((min(a, b), max(a, b)) in pair_similarities for a in c_i for b in c_j):
                for m in c_j:
                    c_i.append(m)
                    item_to_cluster[m] = c_i
                clusters.remove(c_j)

    duplicate_groups: List[DuplicateGroup] = []
    group_id = 1

    for cl in clusters:
        if stop_check and stop_check():
            return []

        if len(cl) > 1:
            group_files = [records[i] for i in cl]

            # Similaridade real média de todos os pares do clique
            scores = []
            for a_idx in range(len(cl)):
                for b_idx in range(a_idx + 1, len(cl)):
                    pair = (min(cl[a_idx], cl[b_idx]), max(cl[a_idx], cl[b_idx]))
                    if pair in pair_similarities:
                        scores.append(pair_similarities[pair])

            avg_score = sum(scores) / len(scores) if scores else similarity_threshold

            duplicate_groups.append(DuplicateGroup(
                group_id=group_id,
                files=group_files,
                similarity=round(avg_score, 4),
                kind=mode
            ))
            group_id += 1

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
