import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dupeclean.core.cache import CacheManager, compute_prefix_hash
from dupeclean.core.hasher_exact import find_exact_duplicates
from dupeclean.core.models import FileRecord, ScanMode, ScanPath
from dupeclean.core.scanner import crawl_directories, get_volume_capabilities


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

    def test_end_to_end_crawl_and_exact_move(self):
        """Valida fluxo real completo via crawl_directories() (onde scandir retorna 0,0) e L2 Lazy após mover pasta."""
        folder_orig = Path(self.temp_dir) / "pasta_original"
        folder_orig.mkdir()
        payload = b"REAL_DUP_DATA_" * 2048  # > 8 KB
        (folder_orig / "dup1.bin").write_bytes(payload)
        (folder_orig / "dup2.bin").write_bytes(payload)

        # 1. Crawl + deduplicação real (workers capturam volume_id e file_id via os.fstat)
        records1 = crawl_directories([ScanPath(folder_orig)], [], ScanMode.EXACT)
        self.assertEqual(len(records1), 2)
        self.assertEqual(records1[0].file_id, 0)  # scandir no Windows retorna 0

        groups = find_exact_duplicates(records1, cache_manager=self.cache)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].files), 2)

        # 2. Renomeia a pasta inteira no NTFS
        folder_moved = Path(self.temp_dir) / "pasta_renomeada"
        folder_orig.rename(folder_moved)

        # 3. Novo crawl retorna novos caminhos ainda com file_id=0
        records2 = crawl_directories([ScanPath(folder_moved)], [], ScanMode.EXACT)
        self.assertEqual(len(records2), 2)
        self.assertEqual(records2[0].file_id, 0)

        # 4. get_exact_cache deve resolver via L2 Lazy sem recalcular
        cached = self.cache.get_exact_cache(records2)
        self.assertEqual(len(cached), 2)
        for rec in records2:
            p_hash, f_hash = cached[str(rec.path)]
            self.assertIsNotNone(f_hash)

    def test_legacy_db_backfill_on_l1_then_move(self):
        """Valida que registros de bancos antigos (volume_id=0, file_id=0) recebem backfill no L1 e sobrevivem a move."""
        folder_orig = Path(self.temp_dir) / "legado"
        folder_orig.mkdir()
        img = folder_orig / "antiga.jpg"
        img.write_bytes(b"LEGACY_IMAGE_" * 1024)
        dummy_emb = b"\x33\x44" * 768

        st = os.stat(img)
        # Grava no estilo antigo (sem volume_id, file_id ou prefix_hash)
        self.cache.save_image_batch([(str(img), st.st_size, st.st_mtime, dummy_emb)])

        # Scan normal via crawl_directories antes do move -> L1 acerta e faz backfill do File ID
        recs1 = crawl_directories([ScanPath(folder_orig)], [], ScanMode.IMAGES)
        hit_l1 = self.cache.get_image_cache(recs1, expected_bytes=1536)
        self.assertIn(str(img), hit_l1)

        with self.cache._get_connection() as conn:
            row = conn.execute("SELECT volume_id, file_id FROM cache_images WHERE path = ?;", (str(img),)).fetchone()
            self.assertGreater(row[0], 0)
            self.assertGreater(row[1], 0)

        # Agora move a pasta inteira e garante que o L2 recupera pelo File ID backfilled
        folder_moved = Path(self.temp_dir) / "legado_movido"
        folder_orig.rename(folder_moved)
        recs2 = crawl_directories([ScanPath(folder_moved)], [], ScanMode.IMAGES)
        hit_l2 = self.cache.get_image_cache(recs2, expected_bytes=1536)
        self.assertIn(str(folder_moved / "antiga.jpg"), hit_l2)
        self.assertEqual(hit_l2[str(folder_moved / "antiga.jpg")], dummy_emb)

    def test_hardlinks_l2_does_not_delete_existing_link(self):
        """Garante que mover/renomear um hardlink NTFS não remove do cache outro hardlink válido existente no disco."""
        orig = Path(self.temp_dir) / "hl_orig.jpg"
        link2 = Path(self.temp_dir) / "hl_link2.jpg"
        orig.write_bytes(b"HARDLINK_IMG_" * 1024)
        try:
            os.link(orig, link2)
        except OSError:
            self.skipTest("Hardlinks não suportados neste diretório")

        st = os.stat(orig)
        vol_id, file_id = st.st_dev, st.st_ino
        dummy_emb = b"\x55\x66" * 768
        self.cache.save_image_batch([
            (str(orig), st.st_size, st.st_mtime, dummy_emb, vol_id, file_id, "hl_p"),
            (str(link2), st.st_size, st.st_mtime, dummy_emb, vol_id, file_id, "hl_p"),
        ])

        # Renomeia apenas link2; orig continua existindo no disco
        link2_renamed = Path(self.temp_dir) / "hl_link2_novo.jpg"
        link2.rename(link2_renamed)

        rec_renamed = FileRecord(path=link2_renamed, size=st.st_size, mtime=st.st_mtime)
        hit = self.cache.get_image_cache([rec_renamed], expected_bytes=1536)
        self.assertIn(str(link2_renamed), hit)

        with self.cache._get_connection() as conn:
            orig_exists_in_db = conn.execute("SELECT COUNT(*) FROM cache_images WHERE path = ?;", (str(orig),)).fetchone()[0]
            link2_old_in_db = conn.execute("SELECT COUNT(*) FROM cache_images WHERE path = ?;", (str(link2),)).fetchone()[0]
            self.assertEqual(orig_exists_in_db, 1)  # Preservado!
            self.assertEqual(link2_old_in_db, 0)     # Limpo porque não existe mais no disco!

    def test_dav_l3_requires_mtime_proximity(self):
        """Arquivos .dav pré-alocados exigem proximidade de mtime no L3 para evitar colisão de container vazio."""
        dav_a = Path(self.temp_dir) / "cam1.dav"
        dav_b = Path(self.temp_dir) / "cam2.dav"
        payload = b"\x00DAV_HEADER" + (b"\x00" * 16384)
        dav_a.write_bytes(payload)
        dav_b.write_bytes(payload)

        st_a = os.stat(dav_a)
        # Define mtime de dav_b para 100 segundos diferente
        os.utime(dav_b, (st_a.st_atime, st_a.st_mtime + 100.0))
        st_b = os.stat(dav_b)

        p_hash = compute_prefix_hash(dav_a, st_a.st_size)
        dummy_emb = b"\x77\x88" * 3840

        self.cache.save_video_batch([
            (str(dav_a), st_a.st_size, st_a.st_mtime, 30.0, dummy_emb, 0, 0, p_hash)
        ])

        # Simula volume sem suporte a File ID (exFAT) para forçar caminho L3
        with patch("dupeclean.core.cache.get_volume_capabilities", return_value=("EXFAT", False)):
            rec_b = FileRecord(path=dav_b, size=st_b.st_size, mtime=st_b.st_mtime, ext=".dav")
            hit_diff_mtime = self.cache.get_video_cache([rec_b], expected_bytes=7680)
            self.assertNotIn(str(dav_b), hit_diff_mtime)

            # Ajusta mtime dentro da janela de tolerância (<= 2s) -> deve acertar L3
            os.utime(dav_b, (st_a.st_atime, st_a.st_mtime + 1.0))
            st_b_close = os.stat(dav_b)
            rec_b_close = FileRecord(path=dav_b, size=st_b_close.st_size, mtime=st_b_close.st_mtime, ext=".dav")
            hit_close_mtime = self.cache.get_video_cache([rec_b_close], expected_bytes=7680)
            self.assertIn(str(dav_b), hit_close_mtime)

    def test_partial_indexes_used_for_l2_and_l3_queries(self):
        """Garante que os SELECTs de L2 e L3 incluam o predicado do índice parcial e nunca façam SCAN."""
        queries = [
            ("idx_exact_fileid", "EXPLAIN QUERY PLAN SELECT path, size, mtime, prefix_hash, full_hash, volume_id, file_id FROM cache_exact WHERE file_id IN (1, 2) AND file_id != 0"),
            ("idx_images_fileid", "EXPLAIN QUERY PLAN SELECT path, size, mtime, embedding, volume_id, file_id, prefix_hash FROM cache_images WHERE file_id IN (1, 2) AND file_id != 0"),
            ("idx_videos_fileid", "EXPLAIN QUERY PLAN SELECT path, size, mtime, duration, embedding, volume_id, file_id, prefix_hash FROM cache_videos WHERE file_id IN (1, 2) AND file_id != 0"),
            ("idx_images_prefix_h", "EXPLAIN QUERY PLAN SELECT size, prefix_hash, embedding FROM cache_images WHERE prefix_hash IN ('a', 'b') AND prefix_hash IS NOT NULL"),
            ("idx_videos_prefix_h", "EXPLAIN QUERY PLAN SELECT size, prefix_hash, mtime, duration, embedding FROM cache_videos WHERE prefix_hash IN ('a', 'b') AND prefix_hash IS NOT NULL"),
        ]
        with self.cache._get_connection() as conn:
            for idx_name, sql in queries:
                plan = " ".join(str(r[-1]) for r in conn.execute(sql).fetchall())
                self.assertIn(idx_name, plan, msg=f"Esperado {idx_name} em: {plan}")
                self.assertIn("INDEX ", plan, msg=f"Esperado uso de índice em: {plan}")
                self.assertNotIn("SCAN ", plan, msg=f"Full table scan detectado: {plan}")

    def test_exact_cache_emits_stage2_progress_callback(self):
        """Garante sinalização determinística de progresso do Estágio 2 durante consulta ao cache exato."""
        dummy_records = [
            FileRecord(path=Path(self.temp_dir) / f"f_{i}.bin", size=128 + (i % 10), mtime=1000.0)
            for i in range(2500)
        ]
        events = []
        self.cache.get_exact_cache(
            dummy_records,
            progress_callback=lambda msg, curr, tot: events.append((msg, curr, tot))
        )
        self.assertGreater(len(events), 0)
        self.assertTrue(any("Checking cache" in m for m, _, _ in events))
        self.assertTrue(all(tot == len(dummy_records) for _, _, tot in events))
        self.assertEqual(events[-1][1], len(dummy_records))
        currs = [curr for _, curr, _ in events]
        self.assertEqual(currs, sorted(currs))

    def test_image_cache_emits_deterministic_progress_callback(self):
        """Garante que a consulta ao cache de imagens emita progresso determinístico (total > 0)."""
        dummy_records = [
            FileRecord(path=Path(self.temp_dir) / f"img_{i}.jpg", size=2048 + (i % 20), mtime=1000.0)
            for i in range(3000)
        ]
        events = []
        self.cache.get_image_cache(
            dummy_records,
            expected_bytes=512,
            progress_callback=lambda msg, curr, tot: events.append((msg, curr, tot))
        )
        self.assertGreater(len(events), 1)
        self.assertTrue(any("Checking cache" in m for m, _, _ in events))
        self.assertTrue(all(tot == len(dummy_records) for _, _, tot in events))
        self.assertGreater(events[0][1], 0)
        self.assertEqual(events[-1][1], len(dummy_records))
        currs = [curr for _, curr, _ in events]
        self.assertEqual(currs, sorted(currs))


if __name__ == "__main__":
    unittest.main()
