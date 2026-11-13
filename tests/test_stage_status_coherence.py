import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication
from dupeclean.ui.main_window import MainWindow


class TestStageStatusCoherence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def test_stage_and_loading_results_coherence_images_mode(self):
        win = MainWindow(gpu_info={"name": "NVIDIA GeForce RTX 5090", "has_cuda": True})
        self.addCleanup(win.close)

        win._on_stage_progress("Grouping similar images (17,230/135,928)...", 17230, 135928, 4, 4)
        self.assertEqual(
            win.lbl_status.text(),
            "Stage 4/5: Grouping similar images (17,230/135,928)...",
        )
        self.assertEqual(win.lbl_pct_all.text(), "62%")

        win._is_scan_mounting = True
        win._on_operation_progress("Loading results", 1234, 5678)
        self.assertEqual(
            win.lbl_status.text(),
            "Stage 5/5: Loading results (1,234/5,678)...",
        )
        self.assertEqual(win.lbl_pct_all.text(), "84%")

    def test_stage_and_loading_results_coherence_exact_mode(self):
        win = MainWindow(gpu_info={"name": "NVIDIA GeForce RTX 5090", "has_cuda": True})
        self.addCleanup(win.close)

        win._on_stage_progress("Calculating full hashes (500/1,000)...", 500, 1000, 3, 3)
        self.assertEqual(
            win.lbl_status.text(),
            "Stage 3/4: Calculating full hashes (500/1,000)...",
        )

        win._is_scan_mounting = True
        win._on_operation_progress("Loading results", 250, 500)
        self.assertEqual(
            win.lbl_status.text(),
            "Stage 4/4: Loading results (250/500)...",
        )

    def test_non_scan_operation_compact_counter(self):
        win = MainWindow(gpu_info={"name": "NVIDIA GeForce RTX 5090", "has_cuda": True})
        self.addCleanup(win.close)

        win._is_scan_mounting = False
        win._on_operation_progress("Selecting... ", 10, 50)
        self.assertEqual(win.lbl_status.text(), "Selecting (10/50)...")

    def test_stalled_telemetry_drops_fake_throughput_after_idle_window(self):
        import time
        from collections import deque

        win = MainWindow(gpu_info={"name": "NVIDIA GeForce RTX 5090", "has_cuda": True})
        self.addCleanup(win.close)

        now = time.time()
        win.start_time = now - 20.0
        win.stage_start_time = now - 10.0
        win.current_stage_num = 2
        win.total_stages = 3
        win.latest_current = 50000
        win.latest_total = 100000
        win._last_progress_change_time = now - 2.0
        win._telemetry_window = deque([(now - 2.0, 50000), (now - 1.0, 50000)])

        win._update_telemetry_tick()
        self.assertNotIn("it/s", win.lbl_timer.text())
        self.assertRegex(win.lbl_timer.text(), r"^\d{2}:\d{2}$")

    def test_exact_stage2_progress_never_regresses_or_hits_100_early(self):
        import shutil
        import tempfile
        from dupeclean.core.cache import CacheManager
        from dupeclean.core.hasher_exact import find_exact_duplicates
        from dupeclean.core.models import FileRecord

        tmp = tempfile.mkdtemp(prefix="dupeclean_stage2_prog_")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        cache = CacheManager(Path(tmp) / "DupeClean.db")

        recs = []
        payload = b"ABCD" * 256  # 1 KB identical candidates
        for i in range(6):
            p = Path(tmp) / f"f_{i}.bin"
            p.write_bytes(payload)
            st = p.stat()
            recs.append(FileRecord(path=p, size=st.st_size, mtime=st.st_mtime))

        stage2_fractions = []

        def _cb(msg, curr, tot, stage_idx=2):
            if stage_idx == 2 and tot > 0:
                stage2_fractions.append(curr / tot)

        find_exact_duplicates(recs, progress_callback=_cb, cache_manager=cache)
        self.assertGreaterEqual(len(stage2_fractions), 2)
        self.assertEqual(stage2_fractions, sorted(stage2_fractions))
        self.assertTrue(all(f < 1.0 for f in stage2_fractions[:-1]))


if __name__ == "__main__":
    unittest.main()
