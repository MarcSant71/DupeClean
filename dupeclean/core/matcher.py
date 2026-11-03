"""
Agrupador e Comparador Matricial por Similaridade na GPU.
Executa multiplicação de tensores na VRAM e agrupa duplicatas por componentes conexos (Union-Find).
Aplica rigorosamente as diretrizes da Pasta de Referência (Ref).
"""

from pathlib import Path
from typing import List, Dict, Set
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
    mode: ScanMode
) -> List[DuplicateGroup]:
    """
    Calcula a matriz de similaridade por cosseno na GPU e agrupa arquivos acima do limiar.
    """
    n = len(records)
    if n <= 1 or embeddings.shape[0] != n:
        return []

    # Multiplicação matricial na RTX 5090 (GEMM em FP16)
    # sim_matrix[i, j] = cosseno entre embedding i e embedding j
    sim_matrix = torch.matmul(embeddings, embeddings.t())

    # Zera a diagonal e a parte inferior para não duplicar comparações
    triu_mask = torch.triu(torch.ones(n, n, device="cuda", dtype=torch.bool), diagonal=1)

    # Identifica pares acima da tolerância
    matches = (sim_matrix >= similarity_threshold) & triu_mask
    match_indices = torch.nonzero(matches)

    if match_indices.shape[0] == 0:
        return []

    uf = DisjointSet(n)
    pair_similarities: Dict[tuple, float] = {}

    for idx in range(match_indices.shape[0]):
        i = int(match_indices[idx, 0])
        j = int(match_indices[idx, 1])
        score = float(sim_matrix[i, j])
        uf.union(i, j)
        pair_similarities[(min(i, j), max(i, j))] = score

    # Agrupar por raiz do Union-Find
    clusters: Dict[int, List[int]] = {}
    for i in range(n):
        root = uf.find(i)
        if root not in clusters:
            clusters[root] = []
        clusters[root].append(i)

    duplicate_groups: List[DuplicateGroup] = []
    group_id = 1

    for root, indices in clusters.items():
        if len(indices) > 1:
            group_files = [records[i] for i in indices]

            # Calcular a similaridade média do grupo
            scores = []
            for a_idx in range(len(indices)):
                for b_idx in range(a_idx + 1, len(indices)):
                    pair = (min(indices[a_idx], indices[b_idx]), max(indices[a_idx], indices[b_idx]))
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
