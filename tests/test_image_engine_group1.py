"""
Testes unitários das otimizações do Grupo 1 no motor de imagens:
- Abertura única com fstat + prefix_hash idêntico
- Tombstones em cache_images para imagens ilegíveis/não suportadas
- Compactação de pool ativo e telemetria no cluster_by_similarity (Fase 4)
- Telemetria contínua sem saltos regressivos em get_image_cache
"""

import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import numpy as np
import torch
from PIL import Image

from dupeclean.core.cache import (
    CacheManager,
    compute_prefix_hash,
    compute_prefix_hash_from_bytes,
)
from dupeclean.core.hasher_image import extract_image_embeddings
from dupeclean.core.matcher import cluster_by_similarity
from dupeclean.core.models import FileRecord, ScanMode


class TestImageEngineGroup1(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="dupeclean_img_g1_")
        self.db_path = Path(self.temp_dir) / "DupeClean.db"
        self.cache = CacheManager(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_png(self, name: str, arr: np.ndarray) -> FileRecord:
        p = Path(self.temp_dir) / name
        Image.fromarray(arr).save(p)
        st = p.stat()
        return FileRecord(path=p, size=st.st_size, mtime=st.st_mtime)

    def test_prefix_hash_from_bytes_parity(self):
        """Garante paridade exata entre compute_prefix_hash_from_bytes e compute_prefix_hash."""
        small = Path(self.temp_dir) / "small.bin"
        large = Path(self.temp_dir) / "large.bin"
        small_data = b"IMG_SMALL_" * 200  # <= 8KB
        large_data = b"HEAD_DATA_" * 500 + b"MID_" * 1000 + b"TAIL_DATA_" * 500  # > 8KB
        small.write_bytes(small_data)
        large.write_bytes(large_data)

        self.assertEqual(compute_prefix_hash_from_bytes(small_data), compute_prefix_hash(small, len(small_data)))
        self.assertEqual(compute_prefix_hash_from_bytes(large_data), compute_prefix_hash(large, len(large_data)))

    def test_corrupt_image_writes_tombstone_and_skips_reopen_on_next_scan(self):
        """Imagem ilegível grava tombstone b'' e não é reaberta no scan seguinte."""
        arr = np.tile(np.linspace(0, 255, 128, dtype=np.uint8), (128, 1))
        ok_rec = self._make_png("ok.png", arr)

        bad_path = Path(self.temp_dir) / "unsupported.heic"
        bad_path.write_bytes(b"FTYPHEIC_CORRUPT_UNSUPPORTED_PAYLOAD_" * 300)
        bad_st = bad_path.stat()
        bad_rec = FileRecord(path=bad_path, size=bad_st.st_size, mtime=bad_st.st_mtime)

        valid1, embs1 = extract_image_embeddings([ok_rec, bad_rec], batch_size=8, cache_manager=self.cache)
        self.assertEqual([r.path for r in valid1], [ok_rec.path])
        self.assertEqual(embs1.shape[0], 1)

        with closing(sqlite3.connect(str(self.db_path))) as conn:
            row = conn.execute(
                "SELECT length(embedding), prefix_hash FROM cache_images WHERE path = ?;",
                (str(bad_path),),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 0)
            self.assertIsNotNone(row[1])

        # Segundo scan: nem ok.png nem unsupported.heic devem entrar no pipeline de decode
        events = []
        with patch("dupeclean.core.hasher_image._decode_image_from_stream") as decode_mock:
            valid2, embs2 = extract_image_embeddings(
                [ok_rec, bad_rec],
                batch_size=8,
                progress_callback=lambda m, c, t: events.append((m, c, t)),
                cache_manager=self.cache,
            )
            decode_mock.assert_not_called()

        self.assertEqual([r.path for r in valid2], [ok_rec.path])
        self.assertEqual(embs2.shape[0], 1)
        self.assertTrue(any("Loading cached images" in m for m, _, _ in events))

    def test_large_image_stream_fallback_path(self):
        """Garante caminho de stream direto (sem leitura total em RAM) acima do limite."""
        arr = np.random.randint(0, 255, (128, 128, 3), dtype=np.uint8)
        rec = self._make_png("stream_test.png", arr)

        with patch("dupeclean.core.hasher_image._SINGLE_READ_IMAGE_THRESHOLD", 1):
            valid, embs = extract_image_embeddings([rec], batch_size=4, cache_manager=self.cache)

        self.assertEqual(len(valid), 1)
        self.assertEqual(embs.shape, (1, 768))
        self.assertIsNotNone(rec.prefix_hash)

    def test_matcher_phase4_compaction_and_progress(self):
        """Valida compactação ativa e exatidão dos grupos na Fase 4 do cluster_by_similarity."""
        device = "cuda" if torch.cuda.is_available() else "cpu"
        torch.manual_seed(42)

        # 2600 embeddings aleatórios ortogonais aproximados + 40 clusters de pares idênticos
        total = 2600
        vecs = torch.randn((total, 128), device=device, dtype=torch.float16)
        for g in range(40):
            i1 = g * 2
            i2 = i1 + 1
            vecs[i2] = vecs[i1]
        vecs = vecs / torch.norm(vecs, dim=1, keepdim=True).clamp(min=1e-6)

        recs = [
            FileRecord(path=Path(self.temp_dir) / f"f_{i}.jpg", size=100, mtime=float(i), is_reference=(i % 2 == 0 and i < 80))
            for i in range(total)
        ]

        events = []
        groups = cluster_by_similarity(
            recs,
            vecs,
            similarity_threshold=0.98,
            mode=ScanMode.IMAGES,
            progress_callback=lambda m, c, t: events.append((m, c, t)),
        )

        self.assertEqual(len(groups), 40)
        for grp in groups:
            self.assertEqual(len(grp.files), 2)
            self.assertTrue(grp.files[0].is_reference)
        self.assertTrue(any("Grouping similar images" in m for m, _, _ in events))


if __name__ == "__main__":
    unittest.main()
