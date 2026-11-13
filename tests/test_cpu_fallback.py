"""Unit tests for universal CPU fallback mode across hardware, embeddings, matcher, and UI badge."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import torch
from PIL import Image, ImageDraw
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dupeclean.core.cache import CacheManager
from dupeclean.core.hardware import (
    check_gpu_compatibility,
    get_compute_device,
    get_optimal_gpu_budgets,
)
from dupeclean.core.hasher_image import DIM_SIZE, extract_image_embeddings
from dupeclean.core.matcher import cluster_by_similarity
from dupeclean.core.models import FileRecord, ScanMode
from dupeclean.ui.main_window import MainWindow


class TestCpuFallback(unittest.TestCase):
    def test_hardware_no_cuda_returns_cpu_fallback(self):
        with patch("torch.cuda.is_available", return_value=False):
            can_start, err_msg, info = check_gpu_compatibility()
            device, dtype = get_compute_device()
            budgets = get_optimal_gpu_budgets()

        self.assertTrue(can_start)
        self.assertEqual(err_msg, "")
        self.assertFalse(info["has_cuda"])
        self.assertEqual(info["name"], "NO GPU")
        self.assertEqual((device, dtype), ("cpu", torch.float32))
        self.assertEqual(budgets["gpu_name"], "CPU")
        self.assertEqual(budgets["image_batch_size"], 128)

    def test_hardware_legacy_gpu_cc_below_86_falls_back_to_cpu(self):
        with (
            patch("torch.cuda.is_available", return_value=True),
            patch("torch.cuda.device_count", return_value=1),
            patch("torch.cuda.set_device", return_value=None),
            patch("torch.cuda.get_device_name", return_value="NVIDIA GeForce GTX 1060"),
            patch("torch.cuda.get_device_capability", return_value=(6, 1)),
            patch("torch.cuda.get_device_properties", return_value=SimpleNamespace(total_memory=6 * 1024**3)),
        ):
            can_start, err_msg, info = check_gpu_compatibility()
            device, dtype = get_compute_device()
            budgets = get_optimal_gpu_budgets()

        self.assertTrue(can_start)
        self.assertEqual(err_msg, "")
        self.assertFalse(info["has_cuda"])
        self.assertEqual(info["name"], "NO GPU")
        self.assertEqual((device, dtype), ("cpu", torch.float32))
        self.assertEqual(budgets["gpu_name"], "CPU")

    def test_image_pipeline_on_cpu_keeps_fp16_cache_portability(self):
        tmp_dir = tempfile.mkdtemp(prefix="dupeclean_cpu_test_")
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "cpu_test.db"
        cache = CacheManager(db_path=db_path)

        try:
            # Structured photo saved as PNG + re-compressed JPEG + distinct checkerboard photo
            yy, xx = np.mgrid[0:200, 0:320].astype(np.float32)
            arr = np.zeros((200, 320, 3), dtype=np.uint8)
            arr[..., 0] = np.clip(40 + 180 * (xx / 320.0), 0, 255)
            arr[..., 1] = np.clip(200 - 140 * (yy / 200.0), 0, 255)
            arr[..., 2] = np.clip(110 + 90 * np.sin(xx / 28.0) * np.cos(yy / 22.0), 0, 255)
            rich_img = Image.fromarray(arr, "RGB")
            draw = ImageDraw.Draw(rich_img)
            draw.rectangle([25, 25, 130, 165], fill=(240, 210, 40))
            draw.ellipse([180, 40, 290, 170], fill=(35, 65, 195))

            img_a_path = tmp_path / "photo_a.png"
            img_b_path = tmp_path / "photo_b.jpg"
            img_c_path = tmp_path / "distinct_c.png"

            rich_img.save(img_a_path)
            rich_img.resize((160, 100), Image.Resampling.LANCZOS).save(img_b_path, "JPEG", quality=85)

            checker = np.zeros((128, 128, 3), dtype=np.uint8)
            checker[(np.indices((128, 128)).sum(axis=0) // 8) % 2 == 0] = [255, 40, 120]
            Image.fromarray(checker, mode="RGB").save(img_c_path)

            records = [
                FileRecord(path=p, size=p.stat().st_size, mtime=p.stat().st_mtime)
                for p in (img_a_path, img_b_path, img_c_path)
            ]

            with (
                patch("dupeclean.core.hasher_image.get_compute_device", return_value=("cpu", torch.float32)),
                patch("dupeclean.core.hardware.get_compute_device", return_value=("cpu", torch.float32)),
            ):
                valid_recs, emb_first = extract_image_embeddings(records, cache_manager=cache)
                self.assertEqual(len(valid_recs), 3)
                self.assertEqual(emb_first.device.type, "cpu")
                self.assertEqual(emb_first.dtype, torch.float32)
                self.assertEqual(tuple(emb_first.shape), (3, DIM_SIZE))

                # SQLite cache must store compact FP16 blobs (768 * 2 = 1536 bytes) even when computed on CPU
                cached_blobs = cache.get_image_cache(records, expected_bytes=DIM_SIZE * 2)
                self.assertEqual(len(cached_blobs), 3)
                for blob in cached_blobs.values():
                    self.assertEqual(len(blob), DIM_SIZE * 2)

                # Re-reading from SQLite FP16 cache on CPU must reload cleanly as float32 and cluster properly
                valid_cached, emb_cached = extract_image_embeddings(records, cache_manager=cache)
                self.assertEqual(len(valid_cached), 3)
                self.assertEqual(emb_cached.device.type, "cpu")
                self.assertEqual(emb_cached.dtype, torch.float32)

                groups = cluster_by_similarity(
                    records=valid_cached,
                    embeddings=emb_cached,
                    similarity_threshold=0.90,
                    mode=ScanMode.IMAGES,
                )

            self.assertEqual(len(groups), 1)
            grouped_names = {f.path.name for f in groups[0].files}
            self.assertEqual(grouped_names, {"photo_a.png", "photo_b.jpg"})
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_main_window_no_gpu_badge_styling_and_modes_enabled(self):
        app = QApplication.instance() or QApplication(sys.argv)
        self.assertIsNotNone(app)

        with tempfile.TemporaryDirectory() as tmp_dir:
            settings_path = Path(tmp_dir) / "ui_state.ini"
            with patch("dupeclean.ui.main_window.CacheManager") as mock_cm:
                mock_cm.return_value.get_folder_counts.return_value = {}
                mock_cm.return_value.get_folders.return_value = []

                cpu_win = MainWindow({"name": "NO GPU", "has_cuda": False, "arch": "CPU Mode", "vram_gb": 0.0})
                try:
                    self.assertIn("NO GPU", cpu_win.gpu_badge_label.text())
                    self.assertIn("#a1a1aa", cpu_win.gpu_badge_label.styleSheet().lower())
                    self.assertIsNotNone(cpu_win.gpu_dot_label.pixmap())
                    self.assertFalse(cpu_win.gpu_dot_label.pixmap().isNull())
                    for row in range(3):
                        item = cpu_win.sidebar_modes.item(row)
                        self.assertTrue(bool(item.flags() & Qt.ItemFlag.ItemIsEnabled))
                finally:
                    cpu_win.close()

                gpu_win = MainWindow({"name": "NVIDIA GeForce RTX 4090", "has_cuda": True, "arch": "Ada", "vram_gb": 24.0})
                try:
                    self.assertIn("RTX 4090", gpu_win.gpu_badge_label.text())
                    self.assertIn("#4ade80", gpu_win.gpu_badge_label.styleSheet().lower())
                    self.assertIsNotNone(gpu_win.gpu_dot_label.pixmap())
                    self.assertFalse(gpu_win.gpu_dot_label.pixmap().isNull())
                finally:
                    gpu_win.close()
            _ = settings_path


if __name__ == "__main__":
    unittest.main()
