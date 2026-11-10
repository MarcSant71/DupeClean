"""
Regression test for large file grids and 64-bit size signal emissions.
Verifies that selecting Smart Duplicates with thousands of items and >2 GB
of total reclaimable bytes does not crash or overflow.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from dupeclean.core.models import DuplicateGroup, FileRecord, ScanMode
from dupeclean.ui.main_window import MainWindow


def run_test():
    app = QApplication.instance() or QApplication(sys.argv)
    win = MainWindow(gpu_info={})

    groups = []
    fid = 1
    # 2,050 groups * 3 files = 6,150 files with >3.3 GB total
    for g_id in range(1, 2050):
        files = []
        for f_idx in range(3):
            rec = FileRecord(
                path=Path(f"C:/fake_photos/group_{g_id}/file_{fid}.jpg"),
                size=546050,
                mtime=1700000000.0 + fid,
                is_reference=False,
                name=f"file_{fid}.jpg",
                ext=".jpg"
            )
            files.append(rec)
            fid += 1
        groups.append(DuplicateGroup(group_id=g_id, files=files, similarity=0.92, kind=ScanMode.IMAGES))

    win._on_scan_finished(groups)
    app.processEvents()

    # 1. Test Smart Duplicates
    t0 = time.time()
    win.results_widget.select_smart_duplicates()
    t1 = time.time()
    app.processEvents()
    assert (t1 - t0) < 5.0
    assert "duplicate file(s)" in win.lbl_status.text()
    print(f"Smart selection completed in {t1 - t0:.2f}s: {win.lbl_status.text()}")

    # 2. Test with Column 0 (Selection) sorting enabled
    win.results_widget.tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    app.processEvents()
    win.results_widget.select_smart_duplicates()
    app.processEvents()
    print("Column 0 sort + select_smart_duplicates OK")

    # 3. Test Select All and Unselect All
    win.results_widget.select_all()
    app.processEvents()
    assert win.btn_delete_trash.isEnabled()
    print(f"Select All OK: {win.lbl_status.text()}")

    win.results_widget.unselect_all()
    app.processEvents()
    assert not win.btn_delete_trash.isEnabled()
    print("Unselect All OK")

    print("\nRegression test passed successfully!")


if __name__ == "__main__":
    run_test()
