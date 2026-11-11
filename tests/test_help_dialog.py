import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication
from dupeclean.ui.help_dialog import HelpDialog
from dupeclean.ui.main_window import MainWindow


class TestHelpDialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def test_help_dialog_structure(self):
        gpu_info = {"name": "NVIDIA GeForce RTX 5090", "has_cuda": True, "arch": "Blackwell", "vram_gb": 32}
        dlg = HelpDialog(gpu_info)

        self.assertEqual(dlg.tabs.count(), 5)
        self.assertEqual(dlg.tabs.tabText(0), "📘 Product Overview")
        self.assertEqual(dlg.tabs.tabText(1), "🔬 Technical Pipeline & Stages")
        self.assertEqual(dlg.tabs.tabText(2), "🎯 Scan Modes")
        self.assertEqual(dlg.tabs.tabText(3), "⌨️ Shortcuts & Tips")
        self.assertEqual(dlg.tabs.tabText(4), "📬 Contact Information")

    def test_main_window_help_button_direct_click(self):
        gpu_info = {"name": "NVIDIA GeForce RTX 5090", "has_cuda": True}
        win = MainWindow(gpu_info)

        self.assertIsNotNone(win.btn_help)
        self.assertEqual(win.btn_help.text(), "?")
        self.assertEqual(win.btn_help.objectName(), "helpButton")

        # Must not have a dropdown menu - clicking opens HelpDialog directly in 1 click
        self.assertIsNone(win.btn_help.menu())

        # Test direct click trigger opens HelpDialog
        with patch.object(HelpDialog, "exec") as mock_exec:
            win.btn_help.click()
            mock_exec.assert_called_once()


if __name__ == "__main__":
    unittest.main()
