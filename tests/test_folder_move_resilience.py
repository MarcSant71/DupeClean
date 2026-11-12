import os
import shutil
import tempfile
import unittest
from pathlib import Path

from dupeclean.core.cache import CacheManager, compute_prefix_hash
from dupeclean.core.models import FileRecord
from dupeclean.core.scanner import get_volume_capabilities


class TestFolderMoveResilience(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="dc_resilience_test_")
        self.db_path = Path(self.temp_dir) / "test_cache.db"
        self.cache = CacheManager(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_volume_capabilities_detection(self):
        """Valida que volumes NTFS são identificados com suporte a File ID ativo."""
        fs_name, supports_file_id = get_volume_capabilities(Path(self.temp_dir))
        self.assertEqual(fs_name, "NTFS")
        self.assertTrue(supports_file_id)

    def test_exact_cache_l2_ntfs_move(self):
        """Valida Tier 2 (L2 File ID) em modo Exato quando um arquivo é renomeado ou movido."""
        file_a = Path(self.temp_dir) / "origem.dat"
        file_a.write_bytes(b"A" * 16384)

        st_a = os.stat(file_a)
        vol_id = getattr(st_a, "st_dev", 0)
        file_id = getattr(st_a, "st_ino", 0)
        self.assertGreater(file_id, 0)

        rec_a = FileRecord(
            path=file_a,
            size=st_a.st_size,
            mtime=st_a.st_mtime,
            volume_id=vol_id,
            file_id=file_id
        )

        # 1. Salva no cache com o caminho original
        self.cache.save_exact_batch([(
            str(file_a), rec_a.size, rec_a.mtime, "pref_hash_1", "full_hash_1", vol_id, file_id
        )])

        # 2. Confirma hit L1
        hit_l1 = self.cache.get_exact_cache([rec_a])
        self.assertIn(str(file_a), hit_l1)
        self.assertEqual(hit_l1[str(file_a)], ("pref_hash_1", "full_hash_1"))

        # 3. Move o arquivo para uma nova pasta / novo nome no mesmo volume
        sub_folder = Path(self.temp_dir) / "pasta_movida"
        sub_folder.mkdir()
        file_b = sub_folder / "destino.dat"
        file_a.rename(file_b)

        st_b = os.stat(file_b)
        self.assertEqual(st_b.st_dev, vol_id)
        self.assertEqual(st_b.st_ino, file_id)

        rec_b = FileRecord(
            path=file_b,
            size=st_b.st_size,
            mtime=st_b.st_mtime,
            volume_id=vol_id,
            file_id=file_id
        )

        # 4. Consulta com o novo caminho (L1 falha, L2 recupera pelo File ID instantaneamente)
        hit_l2 = self.cache.get_exact_cache([rec_b])
        self.assertIn(str(file_b), hit_l2)
        self.assertEqual(hit_l2[str(file_b)], ("pref_hash_1", "full_hash_1"))

        # 5. Verifica se o caminho no SQLite foi atualizado para file_b
        with self.cache._get_connection() as conn:
            old_count = conn.execute("SELECT COUNT(*) FROM cache_exact WHERE path = ?;", (str(file_a),)).fetchone()[0]
            new_count = conn.execute("SELECT COUNT(*) FROM cache_exact WHERE path = ?;", (str(file_b),)).fetchone()[0]
            self.assertEqual(old_count, 0)
            self.assertEqual(new_count, 1)

    def test_image_cache_l2_ntfs_move(self):
        """Valida Tier 2 (L2 File ID) em modo Imagens quando fotos são movidas no mesmo volume."""
        img_a = Path(self.temp_dir) / "foto_a.jpg"
        img_a.write_bytes(b"IMG_CONTENT_" * 1024)

        st_a = os.stat(img_a)
        vol_id = getattr(st_a, "st_dev", 0)
        file_id = getattr(st_a, "st_ino", 0)
        dummy_embedding = b"\x01\x02" * 768  # 1536 bytes

        rec_a = FileRecord(
            path=img_a,
            size=st_a.st_size,
            mtime=st_a.st_mtime,
            volume_id=vol_id,
            file_id=file_id
        )

        self.cache.save_image_batch([(
            str(img_a), rec_a.size, rec_a.mtime, dummy_embedding, vol_id, file_id, "p_img_hash"
        )])

        # Move o arquivo
        img_b = Path(self.temp_dir) / "foto_renomeada.jpg"
        img_a.rename(img_b)

        st_b = os.stat(img_b)
        rec_b = FileRecord(
            path=img_b,
            size=st_b.st_size,
            mtime=st_b.st_mtime,
            volume_id=vol_id,
            file_id=file_id
        )

        hit = self.cache.get_image_cache([rec_b], expected_bytes=1536)
        self.assertIn(str(img_b), hit)
        self.assertEqual(hit[str(img_b)], dummy_embedding)

    def test_image_cache_l3_content_sieve_fallback(self):
        """Valida Tier 3 (L3 Content Sieve) quando File ID não está disponível (exFAT, FAT32 ou cópia)."""
        content = b"TEST_CONTENT_FOR_L3_SIEVE_" * 500  # > 8 KB
        file_orig = Path(self.temp_dir) / "orig_exfat.jpg"
        file_copy = Path(self.temp_dir) / "copy_exfat.jpg"

        file_orig.write_bytes(content)
        file_copy.write_bytes(content)

        st_orig = os.stat(file_orig)
        st_copy = os.stat(file_copy)

        dummy_embedding = b"\xAA\xBB" * 768
        p_hash = compute_prefix_hash(file_orig, st_orig.st_size)
        self.assertIsNotNone(p_hash)

        # Simula gravação sem File ID (volume_id=0, file_id=0)
        self.cache.save_image_batch([(
            str(file_orig), st_orig.st_size, st_orig.st_mtime, dummy_embedding, 0, 0, p_hash
        )])

        # Agora consulta a cópia com File ID zerado (simulando disco externo exFAT)
        rec_copy = FileRecord(
            path=file_copy,
            size=st_copy.st_size,
            mtime=st_copy.st_mtime,
            volume_id=0,
            file_id=0
        )

        # L1 falha (caminho diferente), L2 falha (file_id=0), L3 deve acertar pelo tamanho + prefix_hash!
        hit_l3 = self.cache.get_image_cache([rec_copy], expected_bytes=1536)
        self.assertIn(str(file_copy), hit_l3)
        self.assertEqual(hit_l3[str(file_copy)], dummy_embedding)

    def test_video_cache_l2_ntfs_move(self):
        """Valida Tier 2 (L2 File ID) em modo Vídeos quando vídeos são movidos no mesmo volume."""
        vid_a = Path(self.temp_dir) / "video_a.mp4"
        vid_a.write_bytes(b"VID_DATA_" * 2048)

        st_a = os.stat(vid_a)
        vol_id = getattr(st_a, "st_dev", 0)
        file_id = getattr(st_a, "st_ino", 0)
        dummy_embedding = b"\x11\x22" * 3840  # 7680 bytes

        rec_a = FileRecord(
            path=vid_a,
            size=st_a.st_size,
            mtime=st_a.st_mtime,
            volume_id=vol_id,
            file_id=file_id
        )

        self.cache.save_video_batch([(
            str(vid_a), rec_a.size, rec_a.mtime, 42.5, dummy_embedding, vol_id, file_id, "p_vid_hash"
        )])

        # Move o vídeo
        vid_b = Path(self.temp_dir) / "video_renomeado.mp4"
        vid_a.rename(vid_b)

        st_b = os.stat(vid_b)
        rec_b = FileRecord(
            path=vid_b,
            size=st_b.st_size,
            mtime=st_b.st_mtime,
            volume_id=vol_id,
            file_id=file_id
        )

        hit = self.cache.get_video_cache([rec_b], expected_bytes=7680)
        self.assertIn(str(vid_b), hit)
        dur, emb = hit[str(vid_b)]
        self.assertEqual(dur, 42.5)
        self.assertEqual(emb, dummy_embedding)

    def test_cache_invalidation_on_content_change(self):
        """Garante que se o conteúdo/tamanho/mtime do arquivo mudar, o cache invalida corretamente."""
        file_a = Path(self.temp_dir) / "teste.dat"
        file_a.write_bytes(b"INITIAL_CONTENT" * 100)

        st = os.stat(file_a)
        vol_id = getattr(st, "st_dev", 0)
        file_id = getattr(st, "st_ino", 0)

        self.cache.save_exact_batch([(
            str(file_a), st.st_size, st.st_mtime, "p1", "f1", vol_id, file_id
        )])

        # Altera conteúdo e tamanho
        file_a.write_bytes(b"MODIFIED_CONTENT_LONGER" * 200)
        st_mod = os.stat(file_a)

        rec_mod = FileRecord(
            path=file_a,
            size=st_mod.st_size,
            mtime=st_mod.st_mtime,
            volume_id=vol_id,
            file_id=file_id
        )

        hit = self.cache.get_exact_cache([rec_mod])
        self.assertNotIn(str(file_a), hit)


if __name__ == "__main__":
    unittest.main()
