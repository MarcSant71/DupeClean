"""
Automated tests for virtualized column sorting, 3-state cycling,
checkbox preservation, header rendering, and expand/collapse toggles.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from dupeclean.core.models import FileRecord, DuplicateGroup, ScanMode
from dupeclean.ui.results_table import ResultsWidget

app = QApplication.instance() or QApplication(sys.argv)


class TestResultsSorting(unittest.TestCase):
    def setUp(self):
        self.widget = ResultsWidget()

        self.g_small = DuplicateGroup(
            group_id=1,
            files=[
                FileRecord(path=Path("C:/demo/small_a.txt"), size=100, mtime=1000.0, name="small_a.txt", hash_value="h1"),
                FileRecord(path=Path("C:/demo/small_b.txt"), size=100, mtime=1001.0, name="small_b.txt", hash_value="h1"),
            ],
            similarity=1.0,
            kind=ScanMode.EXACT,
        )
        self.g_huge = DuplicateGroup(
            group_id=2,
            files=[
                FileRecord(path=Path("C:/demo/huge_a.iso"), size=5000000, mtime=500.0, name="huge_a.iso", hash_value="h2"),
                FileRecord(path=Path("C:/demo/huge_b.iso"), size=5000000, mtime=501.0, name="huge_b.iso", hash_value="h2"),
            ],
            similarity=1.0,
            kind=ScanMode.EXACT,
        )
        self.g_medium = DuplicateGroup(
            group_id=3,
            files=[
                FileRecord(path=Path("C:/demo/medium_a.mp4"), size=250000, mtime=2000.0, name="medium_a.mp4", hash_value="h3"),
                FileRecord(path=Path("C:/demo/medium_b.mp4"), size=250000, mtime=2001.0, name="medium_b.mp4", hash_value="h3"),
            ],
            similarity=0.95,
            kind=ScanMode.EXACT,
        )

        self.widget.set_results([self.g_small, self.g_huge, self.g_medium])

    def _top_group(self, pos: int = 0) -> DuplicateGroup:
        return self.widget.model._entries[pos].group

    def _header_text(self, col: int) -> str:
        return self.widget.model.headerData(col, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)

    def _set_group_file_checked(self, group_id: int, file_idx: int, checked: bool = True):
        row = self.widget.model.row_for_group_file(group_id, file_idx)
        self.assertGreaterEqual(row, 0)
        idx = self.widget.model.index(row, 0)
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.widget.model.setData(idx, state, Qt.ItemDataRole.CheckStateRole)

    def test_size_sort_three_state_cycle(self):
        # 1st double click on col 2 (Size): Ascending (smallest first)
        self.widget._on_header_double_clicked(2)
        self.assertEqual(self.widget._current_sort_order, Qt.SortOrder.AscendingOrder)
        self.assertEqual(self._top_group(0).group_id, 1)
        self.assertEqual(self._header_text(2), "Size")
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())
        self.assertEqual(self.widget.tree.header().sortIndicatorOrder(), Qt.SortOrder.AscendingOrder)

        # 2nd double click: Descending (largest first)
        self.widget._on_header_double_clicked(2)
        self.assertEqual(self.widget._current_sort_order, Qt.SortOrder.DescendingOrder)
        self.assertEqual(self._top_group(0).group_id, 2)
        self.assertEqual(self._header_text(2), "Size")
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())
        self.assertEqual(self.widget.tree.header().sortIndicatorOrder(), Qt.SortOrder.DescendingOrder)

        # 3rd double click: Normal (restores original scan order)
        self.widget._on_header_double_clicked(2)
        self.assertIsNone(self.widget._current_sort_order)
        self.assertEqual(self._top_group(0).group_id, 1)
        self.assertEqual(self._top_group(1).group_id, 2)
        self.assertEqual(self._top_group(2).group_id, 3)
        self.assertEqual(self._header_text(2), "Size")
        self.assertFalse(self.widget.tree.header().isSortIndicatorShown())

    def test_checkmark_preservation_during_sort(self):
        self._set_group_file_checked(1, 0, True)

        self.widget._on_header_double_clicked(2)  # Size asc
        self.widget._on_header_double_clicked(2)  # Size desc
        self.widget._on_header_double_clicked(2)  # Normal
        self.widget._on_header_double_clicked(1)  # Name asc
        self.widget._on_header_double_clicked(1)  # Name desc
        self.widget._on_header_double_clicked(1)  # Normal

        entry_g1 = next((e for e in self.widget.model._entries if e.group.group_id == 1), None)
        self.assertIsNotNone(entry_g1)
        states = {rec.name: chk for rec, chk in zip(entry_g1.files, entry_g1.checked)}
        self.assertTrue(states.get("small_a.txt"))
        self.assertFalse(states.get("small_b.txt"))

    def test_file_name_sort_three_state_cycle(self):
        self.widget._on_header_double_clicked(1)
        self.assertEqual(self._top_group(0).files[0].name, "huge_a.iso")
        self.assertEqual(self._header_text(1), "File Name")
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())

        self.widget._on_header_double_clicked(1)
        self.assertEqual(self._top_group(0).files[0].name, "small_a.txt")
        self.assertEqual(self._header_text(1), "File Name")
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())

        self.widget._on_header_double_clicked(1)
        self.assertIsNone(self.widget._current_sort_order)
        self.assertEqual(self._top_group(0).files[0].name, "small_a.txt")
        self.assertEqual(self._top_group(1).files[0].name, "huge_a.iso")
        self.assertEqual(self._header_text(1), "File Name")
        self.assertFalse(self.widget.tree.header().isSortIndicatorShown())

    def test_date_sort_three_state_cycle(self):
        self.widget._on_header_double_clicked(3)
        self.assertEqual(self._top_group(0).group_id, 2)
        self.assertEqual(self._header_text(3), "Date Modified")
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())

        self.widget._on_header_double_clicked(3)
        self.assertEqual(self._top_group(0).group_id, 3)
        self.assertEqual(self._header_text(3), "Date Modified")
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())

        self.widget._on_header_double_clicked(3)
        self.assertIsNone(self.widget._current_sort_order)
        self.assertEqual(self._top_group(0).group_id, 1)
        self.assertEqual(self._header_text(3), "Date Modified")
        self.assertFalse(self.widget.tree.header().isSortIndicatorShown())

    def test_selection_column_sort_three_state_cycle(self):
        self._set_group_file_checked(3, 0, True)

        self.widget._on_header_double_clicked(0)
        self.assertNotEqual(self._top_group(0).group_id, 3)
        self.assertEqual(self._header_text(0), "Selection")
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())

        self.widget._on_header_double_clicked(0)
        self.assertEqual(self._top_group(0).group_id, 3)
        self.assertEqual(self._header_text(0), "Selection")
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())

        self.widget._on_header_double_clicked(0)
        self.assertIsNone(self.widget._current_sort_order)
        self.assertEqual(self._top_group(0).group_id, 1)
        self.assertEqual(self._header_text(0), "Selection")
        self.assertFalse(self.widget.tree.header().isSortIndicatorShown())

    def test_similarity_and_path_sort_three_state_cycle(self):
        self.widget._on_header_double_clicked(4)
        self.assertEqual(self._top_group(0).group_id, 3)
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())

        self.widget._on_header_double_clicked(4)
        self.assertNotEqual(self._top_group(0).group_id, 3)
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())

        self.widget._on_header_double_clicked(4)
        self.assertIsNone(self.widget._current_sort_order)
        self.assertEqual(self._top_group(0).group_id, 1)
        self.assertFalse(self.widget.tree.header().isSortIndicatorShown())

        self.widget._on_header_double_clicked(5)
        self.assertEqual(self._top_group(0).files[0].name, "huge_a.iso")
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())

        self.widget._on_header_double_clicked(5)
        self.assertEqual(self._top_group(0).files[0].name, "small_a.txt")
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())

        self.widget._on_header_double_clicked(5)
        self.assertIsNone(self.widget._current_sort_order)
        self.assertEqual(self._top_group(0).group_id, 1)
        self.assertFalse(self.widget.tree.header().isSortIndicatorShown())

    def test_double_click_signal_integration(self):
        header = self.widget.tree.header()

        header.sectionClicked.emit(1)
        self.assertIsNone(self.widget._current_sort_order)
        self.assertEqual(self._header_text(1), "File Name")

        header.sectionDoubleClicked.emit(1)
        self.assertEqual(self.widget._current_sort_order, Qt.SortOrder.AscendingOrder)
        self.assertEqual(self._header_text(1), "File Name")
        self.assertTrue(header.isSortIndicatorShown())

        header.sectionDoubleClicked.emit(1)
        self.assertEqual(self.widget._current_sort_order, Qt.SortOrder.DescendingOrder)
        self.assertEqual(self._header_text(1), "File Name")
        self.assertTrue(header.isSortIndicatorShown())

        header.sectionDoubleClicked.emit(1)
        self.assertIsNone(self.widget._current_sort_order)
        self.assertEqual(self._header_text(1), "File Name")
        self.assertFalse(header.isSortIndicatorShown())

    def test_header_sections_always_clickable(self):
        header = self.widget.tree.header()
        self.assertTrue(header.sectionsClickable())

        self.widget.select_smart_duplicates()
        self.assertTrue(header.sectionsClickable())

        self.widget.select_custom("except_newest")
        self.assertTrue(header.sectionsClickable())

        self.widget.select_all()
        self.assertTrue(header.sectionsClickable())

        self.widget.unselect_all()
        self.assertTrue(header.sectionsClickable())

        self.widget.invert_all()
        self.assertTrue(header.sectionsClickable())

        self.widget.remove_deleted_files({Path("C:/demo/small_b.txt")})
        self.assertTrue(header.sectionsClickable())

        self.widget.clear()
        self.assertTrue(self.widget.tree.header().sectionsClickable())

    def test_sorting_status_messages(self):
        emitted_messages = []
        self.widget.status_message.connect(lambda msg: emitted_messages.append(msg))

        self.widget._on_header_double_clicked(2)
        self.assertIn("Sorting results by Size (Ascending)...", emitted_messages)
        self.assertEqual(emitted_messages[-1], "")

        emitted_messages.clear()
        self.widget._on_header_double_clicked(2)
        self.assertIn("Sorting results by Size (Descending)...", emitted_messages)
        self.assertEqual(emitted_messages[-1], "")

        emitted_messages.clear()
        self.widget._on_header_double_clicked(2)
        self.assertIn("Restoring original scan order...", emitted_messages)
        self.assertEqual(emitted_messages[-1], "")

    def test_group_header_omits_similarity_while_column_preserves_it(self):
        hdr_row = self.widget.model.row_for_group(3)
        file_row = self.widget.model.row_for_group_file(3, 0)
        header_text = self.widget.model.data(self.widget.model.index(hdr_row, 1), Qt.ItemDataRole.DisplayRole)
        sim_text = self.widget.model.data(self.widget.model.index(file_row, 4), Qt.ItemDataRole.DisplayRole)
        self.assertIn("Group #3", header_text)
        self.assertIn("2 Files", header_text)
        self.assertIn("Reclaimable:", header_text)
        self.assertNotIn("Similarity:", header_text)
        self.assertEqual(sim_text, "95.0%")

    def test_collapse_and_expand_single_and_all_groups(self):
        # Initially all 3 groups (2 files each) = 3 * 3 = 9 visible rows
        self.assertEqual(self.widget.model.rowCount(), 9)

        # Collapse Group #2 (row index 3 in initial layout)
        g2_row = self.widget.model.row_for_group(2)
        self.widget.model.toggle_group_at_row(g2_row)
        self.assertEqual(self.widget.model.rowCount(), 7)

        # Expand Group #2 back
        self.widget.model.toggle_group_at_row(g2_row)
        self.assertEqual(self.widget.model.rowCount(), 9)

        # Collapse all -> only 3 header rows; Expand all -> back to 9 rows
        self.widget.collapse_all()
        self.assertEqual(self.widget.model.rowCount(), 3)
        self.widget.expand_all()
        self.assertEqual(self.widget.model.rowCount(), 9)


if __name__ == "__main__":
    unittest.main()
