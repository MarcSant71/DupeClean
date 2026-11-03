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
from dupeclean.core.matcher import cluster_by_similarity, apply_reference_rules


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

        # Valida regra de pasta de referência (arquivo de referência NÃO pode ser marcado)
        to_remove = apply_reference_rules(groups[0])
        assert file_a not in to_remove, "ERRO: Arquivo de referência foi marcado para exclusão!"
        assert file_b in to_remove, "Arquivo duplicado normal deveria ser marcado para exclusão!"
        print("[OK] Teste de Duplicatas Exatas e Protecao de Referencia passou com sucesso!")


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
        assert img_a not in to_remove, "ERRO: Imagem de referência foi marcada!"
        assert img_b in to_remove, "Cópia da imagem deveria ser marcada!"
        print(f"[OK] Teste de Imagens Similares na RTX 5090 (Similaridade: {groups[0].similarity*100:.1f}%) passou com sucesso!")


if __name__ == "__main__":
    test_exact_duplicates()
    test_image_gpu_embeddings()
    print("\nTODOS OS TESTES DE INTEGRAÇÃO PASSARAM!")
