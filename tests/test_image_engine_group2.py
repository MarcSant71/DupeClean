import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import torch
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dupeclean.core.cache import CacheManager
from dupeclean.core.hasher_image import ALPHA, _decode_image_from_stream, extract_image_embeddings
from dupeclean.core.matcher import cluster_by_similarity
from dupeclean.core.models import FileRecord, ScanMode


class TestImageEngineGroup2(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="dupeclean_img_g2_")
        self.tmp_path = Path(self.temp_dir)
        self.db_path = self.tmp_path / "cache.db"
        self.cache = CacheManager(self.db_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @staticmethod
    def _make_record(path: Path) -> FileRecord:
        st = path.stat()
        return FileRecord(
            path=path,
            size=st.st_size,
            mtime=st.st_mtime,
            volume_id=getattr(st, "st_dev", 0) or 0,
            file_id=getattr(st, "st_ino", 0) or 0,
        )

    @staticmethod
    def _build_rich_photo(width: int = 320, height: int = 200) -> Image.Image:
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        arr = np.zeros((height, width, 3), dtype=np.uint8)
        arr[..., 0] = np.clip(40 + 180 * (xx / width), 0, 255)
        arr[..., 1] = np.clip(200 - 140 * (yy / height), 0, 255)
        arr[..., 2] = np.clip(110 + 90 * np.sin(xx / 28.0) * np.cos(yy / 22.0), 0, 255)
        img = Image.fromarray(arr, "RGB")
        draw = ImageDraw.Draw(img)
        draw.rectangle([25, 25, 130, 165], fill=(240, 210, 40))
        draw.ellipse([180, 40, 290, 170], fill=(35, 65, 195))
        return img

    def test_alpha_calibration_is_moderate(self):
        self.assertAlmostEqual(ALPHA, 1.2, places=3)

    def test_exif_orientation_duplicates_match_above_95_percent(self):
        """Fotos com tag EXIF de rotação/espelhamento casam com a cópia já transposta."""
        upright = self._build_rich_photo(320, 200)
        p_ref = self.tmp_path / "upright.jpg"
        upright.save(p_ref, format="JPEG", quality=95)

        p_rot6 = self.tmp_path / "exif_rot6.jpg"
        exif6 = Image.Exif()
        exif6[0x0112] = 6  # Exige ROTATE_270 para ficar em pé -> armazenada girada +90 CCW
        upright.rotate(90, expand=True).save(p_rot6, format="JPEG", quality=92, exif=exif6)

        p_rot3 = self.tmp_path / "exif_rot3.jpg"
        exif3 = Image.Exif()
        exif3[0x0112] = 3  # Exige ROTATE_180
        upright.rotate(180, expand=True).save(p_rot3, format="JPEG", quality=92, exif=exif3)

        p_rot8 = self.tmp_path / "exif_rot8.jpg"
        exif8 = Image.Exif()
        exif8[0x0112] = 8  # Exige ROTATE_90 -> armazenada girada 270
        upright.rotate(270, expand=True).save(p_rot8, format="JPEG", quality=92, exif=exif8)

        recs = [self._make_record(p) for p in (p_ref, p_rot6, p_rot3, p_rot8)]
        valid_recs, embs = extract_image_embeddings(recs, batch_size=8, cache_manager=self.cache)
        self.assertEqual(len(valid_recs), 4)

        sims = (embs[0:1] @ embs.t()).squeeze(0).cpu().tolist()
        for idx, score in enumerate(sims[1:], start=1):
            self.assertGreaterEqual(score, 0.95, f"EXIF variant {valid_recs[idx].path.name} scored {score:.3f}")

        groups = cluster_by_similarity(valid_recs, embs, similarity_threshold=0.90, mode=ScanMode.IMAGES)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].files), 4)

    def test_transparent_rgba_and_palette_with_different_hidden_rgb_match(self):
        """PNGs RGBA/P com lixo RGB distinto sob alpha=0 produzem assinaturas equivalentes."""
        paths = []
        for idx, junk in enumerate([(0, 0, 0, 0), (255, 0, 255, 0)]):
            arr = np.full((160, 160, 4), junk, dtype=np.uint8)
            arr[36:124, 36:124] = (30, 165, 235, 255)
            arr[52:108, 60:100] = (240, 210, 50, 255)
            p = self.tmp_path / f"sprite_{idx}.png"
            Image.fromarray(arr, "RGBA").save(p, format="PNG")
            paths.append(p)

        # Versão indexada P com transparência
        im_rgba = Image.open(paths[0])
        p_pal = self.tmp_path / "sprite_pal.png"
        im_rgba.quantize(colors=16).save(p_pal, format="PNG")
        paths.append(p_pal)

        recs = [self._make_record(p) for p in paths]
        valid_recs, embs = extract_image_embeddings(recs, batch_size=8, cache_manager=self.cache)
        self.assertEqual(len(valid_recs), 3)

        groups = cluster_by_similarity(valid_recs, embs, similarity_threshold=0.98, mode=ScanMode.IMAGES)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].files), 3)
        self.assertLessEqual(groups[0].similarity, 1.0)

    def test_solid_and_16bit_images_handled_cleanly(self):
        """Imagens sólidas não geram NaN nem falsos matches; PNG 16-bit normaliza sem saturar."""
        p_black = self.tmp_path / "black.png"
        p_white = self.tmp_path / "white.png"
        Image.new("RGB", (128, 128), (0, 0, 0)).save(p_black)
        Image.new("RGB", (128, 128), (255, 255, 255)).save(p_white)

        # I;16 com padrão distinto em 16 bits (> 255)
        y16, x16 = np.mgrid[0:128, 0:128]
        arr16 = ((x16 * 500 + y16 * 10) % 65535).astype(np.uint16)
        p_16 = self.tmp_path / "grad16.png"
        Image.fromarray(arr16).save(p_16)

        with open(p_16, "rb") as f:
            decoded = _decode_image_from_stream(f)
        self.assertEqual(decoded.shape, (64, 64, 3))
        self.assertGreater(int(decoded.max()) - int(decoded.min()), 50)

        recs = [self._make_record(p) for p in (p_black, p_white, p_16)]
        valid_recs, embs = extract_image_embeddings(recs, batch_size=8, cache_manager=self.cache)
        self.assertEqual(len(valid_recs), 3)
        self.assertFalse(torch.isnan(embs).any().item())
        self.assertFalse(torch.isinf(embs).any().item())

        groups = cluster_by_similarity(valid_recs, embs, similarity_threshold=0.70, mode=ScanMode.IMAGES)
        self.assertEqual(len(groups), 0)

    def test_recompressed_thumbnail_matches_while_static_background_differs(self):
        """Cópia reencodada/reduzida casa; sujeitos diferentes em mesmo fundo estático ficam separados."""
        W, H = 480, 360
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        bg = np.zeros((H, W, 3), dtype=np.uint8)
        bg[..., 0] = np.clip(180 - 60 * (yy / H) + 15 * np.sin(xx / 50), 0, 255)
        bg[..., 1] = np.clip(175 - 50 * (yy / H), 0, 255)
        bg[..., 2] = np.clip(165 - 40 * (yy / H), 0, 255)
        bg[30:180, 30:140] = [235, 240, 245]
        bg[260:, :] = [70, 55, 45]

        im_a = Image.fromarray(bg.copy())
        ImageDraw.Draw(im_a).ellipse([190, 95, 310, 255], fill=(25, 70, 185))

        im_b = Image.fromarray(bg.copy())
        ImageDraw.Draw(im_b).rectangle([280, 120, 410, 285], fill=(195, 45, 40))

        p_a = self.tmp_path / "subj_a.jpg"
        p_a_thumb = self.tmp_path / "subj_a_thumb.jpg"
        p_b = self.tmp_path / "subj_b.jpg"
        im_a.save(p_a, format="JPEG", quality=95)
        im_a.resize((120, 90), Image.Resampling.LANCZOS).save(p_a_thumb, format="JPEG", quality=35)
        im_b.save(p_b, format="JPEG", quality=92)

        recs = [self._make_record(p) for p in (p_a, p_a_thumb, p_b)]
        valid_recs, embs = extract_image_embeddings(recs, batch_size=8, cache_manager=self.cache)
        groups = cluster_by_similarity(valid_recs, embs, similarity_threshold=0.85, mode=ScanMode.IMAGES)

        self.assertEqual(len(groups), 1)
        names = {p.name for p in groups[0].files}
        self.assertEqual(names, {"subj_a.jpg", "subj_a_thumb.jpg"})


if __name__ == "__main__":
    unittest.main()
