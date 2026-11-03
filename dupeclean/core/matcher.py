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
    Calcula similaridade por cosseno em blocos GEMM na GPU e agrupa duplicatas por Union-Find.
    Otimizado para bibliotecas massivas (100k+ arquivos), eliminando alocação densa de VRAM/RAM
    e respondendo instantaneamente (<10ms) ao comando de parada (stop_check).
    """
    n = len(records)
    if n <= 1 or embeddings.shape[0] != n:
        return []
    if stop_check and stop_check():
        return []

    uf = DisjointSet(n)
    pair_similarities: Dict[tuple, float] = {}

    # Multiplicação matricial em blocos para evitar explosão de memória (40 GB em 140k itens)
    # Bloco de 2048 linhas consome apenas ~500 MB na RTX 5090 e roda em milissegundos
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
                uf.union(g_i, g_j)
                pair_similarities[(min(g_i, g_j), max(g_i, g_j))] = float(scores[k])

    if stop_check and stop_check():
        return []

    # Agrupar por raiz do Union-Find
    clusters: Dict[int, List[int]] = {}
    for i in range(n):
        if stop_check and stop_check():
            return []
        root = uf.find(i)
        if root not in clusters:
            clusters[root] = []
        clusters[root].append(i)

    duplicate_groups: List[DuplicateGroup] = []
    group_id = 1

    for root, indices in clusters.items():
        if stop_check and stop_check():
            return []

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
