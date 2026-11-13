"""Unit tests for context-aware enabling/disabling of UI action and folder buttons."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dupeclean.core.models import DuplicateGroup, FileRecord
from dupeclean.ui.main_window import MainWindow


class TestUiButtonStates(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication(sys.argv)

    def _make_window(self) -> MainWindow:
        with patch("dupeclean.ui.main_window.CacheManager") as mock_cm:
            mock_cm.return_value.get_setting.return_value = None
            return MainWindow({"name": "NO GPU", "has_cuda": False, "arch": "CPU Mode", "vram_gb": 0.0})

    def test_empty_state_disables_scan_results_and_folder_removal_buttons(self):
        win = self._make_window()
        try:
            self.assertFalse(win.btn_scan.isEnabled())
            self.assertFalse(win.btn_stop.isEnabled())
            self.assertFalse(win.btn_select_menu.isEnabled())
            self.assertFalse(win.btn_save.isEnabled())
            self.assertFalse(win.btn_move.isEnabled())
            self.assertFalse(win.btn_delete_trash.isEnabled())
            self.assertFalse(win.btn_delete_perm.isEnabled())

            self.assertTrue(win.btn_clear_cache.isEnabled())
            self.assertTrue(win.folder_panel.btn_add_inc.isEnabled())
            self.assertTrue(win.folder_panel.btn_add_exc.isEnabled())
            self.assertFalse(win.folder_panel.btn_remove_inc.isEnabled())
            self.assertFalse(win.folder_panel.btn_clear_inc.isEnabled())
            self.assertFalse(win.folder_panel.btn_remove_exc.isEnabled())
        finally:
            win.close()

    def test_buttons_track_folders_selection_and_results_lifecycle(self):
        win = self._make_window()
        try:
            # 1. Add included folder -> Scan and Clear All enable; Remove Selected needs row selection
            win.folder_panel.add_included_path(Path(r"C:\Photos"))
            self.assertTrue(win.btn_scan.isEnabled())
            self.assertTrue(win.folder_panel.btn_clear_inc.isEnabled())
            self.assertFalse(win.folder_panel.btn_remove_inc.isEnabled())
            self.assertFalse(win.btn_select_menu.isEnabled())
            self.assertFalse(win.btn_save.isEnabled())

            win.folder_panel.table_included.selectRow(0)
            self.assertTrue(win.folder_panel.btn_remove_inc.isEnabled())

            # 2. Add excluded folder -> Remove Exclusion enables only on selection
            win.folder_panel.add_excluded_path(Path(r"C:\Photos\Skip"))
            self.assertFalse(win.folder_panel.btn_remove_exc.isEnabled())
            win.folder_panel.list_excluded.setCurrentRow(0)
            self.assertTrue(win.folder_panel.btn_remove_exc.isEnabled())

            # 3. Scan results arrive -> Select menu and Save enable; Move/Trash/Delete wait for checked files
            f1 = FileRecord(path=Path(r"C:\Photos\a.jpg"), size=1024, mtime=1000.0)
            f2 = FileRecord(path=Path(r"C:\Photos\b.jpg"), size=1024, mtime=1000.0)
            group = DuplicateGroup(group_id=1, files=[f1, f2], similarity=1.0)

            win._on_scan_finished([group])
            self.assertTrue(win.btn_select_menu.isEnabled())
            self.assertTrue(win.btn_save.isEnabled())
            self.assertFalse(win.btn_move.isEnabled())
            self.assertFalse(win.btn_delete_trash.isEnabled())
            self.assertFalse(win.btn_delete_perm.isEnabled())

            # 4. Select duplicate -> file action buttons enable
            win.results_widget.select_smart_duplicates()
            self.assertTrue(win.btn_move.isEnabled())
            self.assertTrue(win.btn_delete_trash.isEnabled())
            self.assertTrue(win.btn_delete_perm.isEnabled())

            # 5. Clear all included folders -> everything returns to disabled cleanly
            win.folder_panel._clear_all_included()
            self.assertFalse(win.btn_scan.isEnabled())
            self.assertFalse(win.btn_select_menu.isEnabled())
            self.assertFalse(win.btn_save.isEnabled())
            self.assertFalse(win.btn_move.isEnabled())
            self.assertFalse(win.btn_delete_trash.isEnabled())
            self.assertFalse(win.btn_delete_perm.isEnabled())
            self.assertFalse(win.folder_panel.btn_clear_inc.isEnabled())
            self.assertFalse(win.folder_panel.btn_remove_inc.isEnabled())
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
