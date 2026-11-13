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

        win._on_stage_progress("Calculating hashes (500/1,000)...", 500, 1000, 3, 3)
        self.assertEqual(
            win.lbl_status.text(),
            "Stage 3/4: Calculating hashes (500/1,000)...",
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


if __name__ == "__main__":
    unittest.main()
