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

    def test_help_dialog_proportional_and_zoom(self):
        win = MainWindow(gpu_info={"name": "RTX"})
        win.resize(1920, 1080)
        dlg = HelpDialog(parent=win)

        # Maximize and minimize hints enabled
        from PySide6.QtCore import Qt
        self.assertTrue(bool(dlg.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint))
        self.assertTrue(bool(dlg.windowFlags() & Qt.WindowType.WindowMinimizeButtonHint))

        # Proportional size check (~82% width, ~86% height)
        self.assertAlmostEqual(dlg.width(), int(1920 * 0.82), delta=5)
        self.assertAlmostEqual(dlg.height(), int(1080 * 0.86), delta=5)

        # Zoom functionality & actual text scaling + focus-independent shortcuts
        from PySide6.QtGui import QTextCursor
        from PySide6.QtTest import QTest
        from dupeclean.ui.help_dialog import ZoomableBrowser

        dlg.tabs.setCurrentIndex(3)
        dlg.show()
        self.app.processEvents()

        browser = dlg.tabs.currentWidget()
        self.assertIsInstance(browser, ZoomableBrowser)

        def _body_px_and_height():
            self.app.processEvents()
            doc = browser.document()
            b = doc.begin().next()
            c = QTextCursor(b)
            c.movePosition(QTextCursor.MoveOperation.NextCharacter)
            return c.charFormat().font().pixelSize(), doc.size().height()

        px_0, h_0 = _body_px_and_height()
        # Focus starts on QTabBar, NOT ZoomableBrowser: shortcuts must still work
        fw = dlg.focusWidget()
        self.assertIsNotNone(fw)
        QTest.keyClick(fw, Qt.Key.Key_Equal, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(browser._zoom_level, 1)
        QTest.keyClick(fw, Qt.Key.Key_Plus, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(browser._zoom_level, 2)
        px_2, h_2 = _body_px_and_height()
        self.assertGreater(px_2, px_0)
        self.assertGreater(h_2, h_0)

        QTest.keyClick(fw, Qt.Key.Key_0, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(browser._zoom_level, 0)
        px_reset, _ = _body_px_and_height()
        self.assertEqual(px_reset, px_0)

        # Focused directly on ZoomableBrowser: must step by 1 (never double-trigger)
        browser.setFocus()
        self.app.processEvents()
        QTest.keyClick(browser, Qt.Key.Key_Minus, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(browser._zoom_level, -1)
        px_neg, h_neg = _body_px_and_height()
        self.assertLess(px_neg, px_0)
        self.assertLess(h_neg, h_0)

        browser.reset_zoom()
        self.assertEqual(browser._zoom_level, 0)
        dlg.close()


if __name__ == "__main__":
    unittest.main()
