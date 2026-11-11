"""
Testes de validação dos motores de deduplicação do DupeClean.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tempfile
from PIL import Image
import torch
import numpy as np

from dupeclean.core.models import ScanPath, ScanMode, FileRecord, DuplicateGroup
from dupeclean.core.scanner import crawl_directories
from dupeclean.core.hasher_exact import find_exact_duplicates
from dupeclean.core.hasher_image import extract_image_embeddings
from dupeclean.core.matcher import cluster_by_similarity, apply_reference_rules, filter_reference_groups


def test_exact_duplicates():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        
        # Cria 2 arquivos idênticos e 1 diferente
        file_a = tmp_path / "foto1.jpg"
        file_b = tmp_path / "foto1_copia.jpg"
        file_c = tmp_path / "foto2.jpg"

        content = b"ConteudoIdenticoParaTesteDeHash1234567890" * 100
        file_a.write_bytes(content)
        file_b.write_bytes(content)
        file_c.write_bytes(b"ConteudoDiferente9876543210" * 100)

        records = [
            FileRecord(path=file_a, size=len(content), mtime=100.0, is_reference=True),
            FileRecord(path=file_b, size=len(content), mtime=200.0, is_reference=False),
            FileRecord(path=file_c, size=len(content), mtime=300.0, is_reference=False),
        ]

        groups = find_exact_duplicates(records)
        assert len(groups) == 1, f"Esperado 1 grupo, obtido {len(groups)}"
        assert len(groups[0].files) == 2, f"Esperado 2 arquivos no grupo, obtido {len(groups[0].files)}"

        # Validate reference folder rule (reference files CANNOT be selected)
        to_remove = apply_reference_rules(groups[0])
        assert file_a not in to_remove, "ERROR: Reference file was marked for deletion!"
        assert file_b in to_remove, "Duplicate file should be marked for deletion!"
        print("[OK] Exact Duplicates & Reference Protection test passed successfully!")


def test_image_gpu_embeddings():
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        # Cria 2 imagens idênticas (ou quase idênticas) e 1 completamente diferente
        img_a = tmp_path / "img_a.png"
        img_b = tmp_path / "img_b.png"
        img_c = tmp_path / "img_c.png"

        # Imagem 1: Padrão com gradiente
        arr1 = np.tile(np.linspace(0, 255, 200, dtype=np.uint8), (200, 1))
        im1 = Image.fromarray(arr1, mode="L")
        
        # Imagem 2: Mesma imagem redimensionada (resolução diferente, conteúdo idêntico)
        im2 = im1.resize((400, 400), Image.Resampling.BILINEAR)
        
        # Imagem 3: Padrão invertido/completamente diferente
        arr3 = np.tile(np.linspace(255, 0, 200, dtype=np.uint8).reshape(-1, 1), (1, 200))
        im3 = Image.fromarray(arr3, mode="L")

        im1.save(img_a)
        im2.save(img_b)
        im3.save(img_c)

        records = [
            FileRecord(path=img_a, size=img_a.stat().st_size, mtime=10.0, is_reference=True),
            FileRecord(path=img_b, size=img_b.stat().st_size, mtime=20.0, is_reference=False),
            FileRecord(path=img_c, size=img_c.stat().st_size, mtime=30.0, is_reference=False),
        ]

        valid_records, embeddings = extract_image_embeddings(records, batch_size=4)
        assert len(valid_records) == 3
        assert embeddings.is_cuda, "Embeddings devem estar na memória GPU (CUDA)!"

        groups = cluster_by_similarity(valid_records, embeddings, similarity_threshold=0.90, mode=ScanMode.IMAGES)
        assert len(groups) == 1, f"Esperado 1 grupo de imagens similares, obtido {len(groups)}"
        assert groups[0].similarity >= 0.95, f"Similaridade esperada >= 95%, obtida: {groups[0].similarity}"

        to_remove = apply_reference_rules(groups[0])
        assert img_a not in to_remove, "ERROR: Reference image was marked for deletion!"
        assert img_b in to_remove, "Image copy should be marked for deletion!"
        print(f"[OK] Similar Images GPU Tensor Core test (Similarity: {groups[0].similarity*100:.1f}%) passed successfully!")


def test_video_clustering_rules():
    """Testa Complete Linkage e filtro estrito de tolerância de duração em vídeos."""
    # 4 vídeos sintéticos:
    # v1 e v2: mesmo conteúdo, duração idêntica (600s) -> DUPLICATA
    # v3: conteúdo similar a v1/v2, mas duração muito diferente (1200s, +100%) -> NÃO DEVE AGRUPAR
    # v4: conteúdo completamente diferente, mesma duração (600s) -> NÃO DEVE AGRUPAR
    
    dim = 320
    v1_vec = torch.randn(1, dim)
    v1_vec = v1_vec / torch.norm(v1_vec)
    
    # v2 quase idêntico a v1 (cosseno ~0.99)
    v2_vec = v1_vec + 0.005 * torch.randn(1, dim)
    v2_vec = v2_vec / torch.norm(v2_vec)
    
    # v3 muito similar a v2 (cosseno ~0.99), mas duração 1200s
    v3_vec = v2_vec + 0.005 * torch.randn(1, dim)
    v3_vec = v3_vec / torch.norm(v3_vec)
    
    # v4 ortogonal/diferente
    v4_vec = torch.randn(1, dim)
    v4_vec = v4_vec / torch.norm(v4_vec)
    
    embeddings = torch.cat([v1_vec, v2_vec, v3_vec, v4_vec], dim=0)
    if torch.cuda.is_available():
        embeddings = embeddings.cuda()
    
    records = [
        FileRecord(path=Path("v1.mp4"), size=1000, mtime=1.0, duration=600.0),
        FileRecord(path=Path("v2.mp4"), size=1000, mtime=2.0, duration=602.0), # dur_diff = 0.3%
        FileRecord(path=Path("v3.mp4"), size=2000, mtime=3.0, duration=1200.0), # dur_diff = 50%
        FileRecord(path=Path("v4.mp4"), size=1000, mtime=4.0, duration=600.0),
    ]
    
    groups = cluster_by_similarity(records, embeddings, similarity_threshold=0.90, mode=ScanMode.VIDEOS)
    assert len(groups) == 1, f"Esperado exatamente 1 grupo de duplicatas, obtido {len(groups)}"
    assert len(groups[0].files) == 2, f"Esperado 2 arquivos (v1 e v2), obtido {len(groups[0].files)}"
    names = {f.name for f in groups[0].files}
    assert names == {"v1.mp4", "v2.mp4"}, f"Grupo deve conter apenas v1 e v2, obtido: {names}"
    print(f"[OK] Video Complete Linkage & Duration Filter test passed successfully!")


def test_reference_filtering():
    """Valida o filtro estrito de pastas de referência."""
    # 1. Grupo com 2 arquivos de Referência e 1 Não-Referência
    r1 = FileRecord(path=Path("C:/SET1/vid.mp4"), size=500, mtime=10.0, is_reference=True)
    r2 = FileRecord(path=Path("C:/SET1/sub/vid_dup.mp4"), size=500, mtime=20.0, is_reference=True)
    dup1 = FileRecord(path=Path("C:/SET2/vid.mp4"), size=500, mtime=30.0, is_reference=False)
    g1 = DuplicateGroup(group_id=1, files=[r1, r2, dup1], similarity=1.0)

    # 2. Grupo com 2 arquivos de Referência e ZERO Não-Referência (duplicação 100% interna da referência)
    r3 = FileRecord(path=Path("C:/SET1/photo1.jpg"), size=200, mtime=10.0, is_reference=True)
    r4 = FileRecord(path=Path("C:/SET1/photo1_copy.jpg"), size=200, mtime=20.0, is_reference=True)
    g2 = DuplicateGroup(group_id=2, files=[r3, r4], similarity=1.0)

    # 3. Grupo sem referência (duplicatas normais entre SET2 e SET3)
    n1 = FileRecord(path=Path("C:/SET2/doc.pdf"), size=300, mtime=10.0, is_reference=False)
    n2 = FileRecord(path=Path("C:/SET3/doc.pdf"), size=300, mtime=20.0, is_reference=False)
    g3 = DuplicateGroup(group_id=3, files=[n1, n2], similarity=1.0)

    filtered = filter_reference_groups([g1, g2, g3])

    # Esperado: 2 grupos (g1 filtrado para r1+dup1, g2 descartado, g3 mantido)
    assert len(filtered) == 2, f"Esperado 2 grupos, obtido {len(filtered)}"

    # Checa g1 filtrado
    group_1_files = filtered[0].files
    assert len(group_1_files) == 2, f"Esperado 2 arquivos no Grupo 1, obtido {len(group_1_files)}"
    assert group_1_files[0] == r1, "Arquivo canônico de referência deve ser r1 (menor caminho)"
    assert group_1_files[1] == dup1, "Segundo arquivo deve ser a duplicata de SET2"
    assert filtered[0].reclaimable_bytes == 500, f"Espaço recuperável esperado 500, obtido {filtered[0].reclaimable_bytes}"

    # Checa g3 preservado
    group_2_files = filtered[1].files
    assert len(group_2_files) == 2
    assert filtered[1].reclaimable_bytes == 300

    print("[OK] Reference filtering (discard internal ref dupes & canonical anchor) test passed successfully!")


if __name__ == "__main__":
    test_exact_duplicates()
    test_image_gpu_embeddings()
    test_video_clustering_rules()
    test_reference_filtering()
    print("\nALL INTEGRATION TESTS PASSED SUCCESSFULLY!")
