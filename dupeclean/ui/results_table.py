"""
Virtualized duplicate results grid backed by QTableView + QAbstractTableModel.
Supports hundreds of thousands of groups and files with O(1) viewport rendering,
fast Python Timsort column sorting, selection rules, and Reference Folder protection.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple
import os
import subprocess

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, QPointF, QRect, QSize, Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QMenu,
    QStyle,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from dupeclean.core.matcher import apply_reference_rules
from dupeclean.core.models import DuplicateGroup, FileRecord


def format_size(size_bytes: int) -> str:
    """Format byte counts into human-readable units."""
    val = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024.0:
            return f"{val:.2f} {unit}"
        val /= 1024.0
    return f"{val:.2f} PB"


_format_size = format_size


@dataclass(slots=True)
class _GroupEntry:
    """Lightweight in-memory representation of a duplicate group and per-file selection."""
    group: DuplicateGroup
    orig_index: int
    header_text: str
    sim_str: str
    k_name: str
    k_size: int
    k_mtime: float
    k_reclaim: int
    k_sim: float
    k_path: str
    k_chk: int
    files: List[FileRecord]
    checked: List[bool]
    display_order: Optional[List[int]] = None
    expanded: bool = True


def _build_group_header(group: DuplicateGroup) -> str:
    reclaim_str = _format_size(group.reclaimable_bytes)
    return (
        f"Group #{group.group_id}  •  {len(group.files)} Files  •  "
        f"Reclaimable: {reclaim_str}"
    )


def _format_similarity(similarity: float) -> str:
    return "100.0%" if similarity >= 0.9999 else f"{similarity * 100:.1f}%"


def _build_entry(group: DuplicateGroup, orig_index: int) -> _GroupEntry:
    files = group.files
    if files:
        f0 = files[0]
        k_name = (f0.name or f0.path.name or "").lower()
        k_size = f0.size
        k_path = str(f0.path).lower()
        k_mtime = max((f.mtime for f in files), default=0.0)
    else:
        k_name = ""
        k_size = 0
        k_path = ""
        k_mtime = 0.0

    return _GroupEntry(
        group=group,
        orig_index=orig_index,
        header_text=_build_group_header(group),
        sim_str=_format_similarity(group.similarity),
        k_name=k_name,
        k_size=k_size,
        k_mtime=k_mtime,
        k_reclaim=group.reclaimable_bytes,
        k_sim=group.similarity,
        k_path=k_path,
        k_chk=0,
        files=files,
        checked=[False] * len(files),
        display_order=None,
        expanded=True,
    )


def _group_sort_key(entry: _GroupEntry, col: int):
    if col == 0:
        return (entry.k_chk, entry.orig_index)
    if col == 1:
        return (entry.k_name, entry.orig_index)
    if col == 2:
        return (entry.k_size, entry.k_reclaim, entry.orig_index)
    if col == 3:
        return (entry.k_mtime, entry.orig_index)
    if col == 4:
        return (entry.k_sim, entry.k_reclaim, entry.orig_index)
    if col == 5:
        return (entry.k_path, entry.orig_index)
    return entry.orig_index


def _sort_entry_files(entry: _GroupEntry, col: int, reverse: bool) -> None:
    """Permute display_order of files within a group to match column sorting rules."""
    n_files = len(entry.files)
    if n_files <= 1:
        entry.display_order = None
        return

    files = entry.files
    checked = entry.checked
    sim = entry.k_sim

    if col == 0:
        order = sorted(
            range(n_files),
            key=lambda fi: (
                files[fi].is_reference,
                checked[fi] if not files[fi].is_reference else False,
                fi,
            ),
            reverse=reverse,
        )
    elif col == 1:
        order = sorted(
            range(n_files),
            key=lambda fi: ((files[fi].name or files[fi].path.name or "").lower(), fi),
            reverse=reverse,
        )
    elif col == 2:
        order = sorted(
            range(n_files),
            key=lambda fi: (files[fi].size, fi),
            reverse=reverse,
        )
    elif col == 3:
        order = sorted(
            range(n_files),
            key=lambda fi: (files[fi].mtime, fi),
            reverse=reverse,
        )
    elif col == 4:
        order = sorted(
            range(n_files),
            key=lambda fi: (sim, fi),
            reverse=reverse,
        )
    elif col == 5:
        order = sorted(
            range(n_files),
            key=lambda fi: (str(files[fi].path).lower(), fi),
            reverse=reverse,
        )
    else:
        entry.display_order = None
        return

    entry.display_order = order


class ResultsModel(QAbstractTableModel):
    """Flat virtualized table model mapping visible rows to group headers or file records."""

    COLUMN_LABELS = [
        "Selection", "File Name", "Size", "Date Modified",
        "Similarity", "Full Path"
    ]

    selection_delta = Signal(int, "qint64")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._entries: List[_GroupEntry] = []
        self._row_group: List[int] = []
        self._row_file: List[int] = []
        self._date_cache: Dict[int, str] = {}
        self._ref_row_bg = QBrush(QColor("#202024"))
        self._ref_row_fg = QBrush(QColor("#71717a"))
        self._selected_count: int = 0
        self._selected_bytes: int = 0

    def _get_mtime_str(self, ts: float) -> str:
        sec = int(ts)
        val = self._date_cache.get(sec)
        if val is None:
            val = datetime.fromtimestamp(ts).strftime("%m/%d/%Y %H:%M:%S")
            if len(self._date_cache) < 50000:
                self._date_cache[sec] = val
        return val

    def _rebuild_flat_index(self) -> None:
        row_group: List[int] = []
        row_file: List[int] = []
        append_g = row_group.append
        append_f = row_file.append
        extend_g = row_group.extend
        extend_f = row_file.extend

        for gi, entry in enumerate(self._entries):
            append_g(gi)
            append_f(-1)
            if entry.expanded:
                cnt = len(entry.files)
                extend_g([gi] * cnt)
                extend_f(range(cnt))

        self._row_group = row_group
        self._row_file = row_file

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._row_group)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return 6

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            if 0 <= section < len(self.COLUMN_LABELS):
                return self.COLUMN_LABELS[section]
        return None

    def is_group_row(self, row: int) -> bool:
        return 0 <= row < len(self._row_file) and self._row_file[row] < 0

    def entry_at_row(self, row: int) -> Optional[_GroupEntry]:
        if 0 <= row < len(self._row_group):
            return self._entries[self._row_group[row]]
        return None

    def file_at_row(self, row: int) -> Optional[Tuple[_GroupEntry, int, FileRecord]]:
        if not (0 <= row < len(self._row_group)):
            return None
        fi_vis = self._row_file[row]
        if fi_vis < 0:
            return None
        entry = self._entries[self._row_group[row]]
        orig_fi = entry.display_order[fi_vis] if entry.display_order is not None else fi_vis
        return entry, orig_fi, entry.files[orig_fi]

    def row_for_group_file(self, group_id: int, file_idx: int = 0) -> int:
        """Locate the flat row index of a file inside a given group_id (used by tests/navigation)."""
        for gi, entry in enumerate(self._entries):
            if entry.group.group_id == group_id:
                for row_idx in range(len(self._row_group)):
                    if self._row_group[row_idx] == gi and self._row_file[row_idx] == file_idx:
                        return row_idx
        return -1

    def row_for_group(self, group_id: int) -> int:
        """Locate the flat row index of a group header row."""
        for gi, entry in enumerate(self._entries):
            if entry.group.group_id == group_id:
                for row_idx in range(len(self._row_group)):
                    if self._row_group[row_idx] == gi and self._row_file[row_idx] < 0:
                        return row_idx
        return -1

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        row = index.row()
        if self.is_group_row(row):
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        info = self.file_at_row(row)
        if info is None:
            return Qt.ItemFlag.NoItemFlags
        _, _, rec = info
        if index.column() == 0 and not rec.is_reference:
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        if not (0 <= row < len(self._row_group)):
            return None

        gi = self._row_group[row]
        fi_vis = self._row_file[row]
        entry = self._entries[gi]

        if fi_vis < 0:
            if role == Qt.ItemDataRole.DisplayRole and col == 1:
                return entry.header_text
            return None

        orig_fi = entry.display_order[fi_vis] if entry.display_order is not None else fi_vis
        rec = entry.files[orig_fi]

        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return ""
            if col == 1:
                return rec.name
            if col == 2:
                return _format_size(rec.size)
            if col == 3:
                return self._get_mtime_str(rec.mtime)
            if col == 4:
                return entry.sim_str
            if col == 5:
                return str(rec.path)
            return None

        if role == Qt.ItemDataRole.CheckStateRole and col == 0:
            if rec.is_reference:
                return None
            return Qt.CheckState.Checked if entry.checked[orig_fi] else Qt.CheckState.Unchecked

        if role == Qt.ItemDataRole.UserRole:
            return rec

        if rec.is_reference:
            if role == Qt.ItemDataRole.BackgroundRole:
                return self._ref_row_bg
            if role == Qt.ItemDataRole.ForegroundRole:
                return self._ref_row_fg
            if role == Qt.ItemDataRole.ToolTipRole:
                return "Protected file (Reference Folder). Cannot be selected or deleted."

        return None

    def setData(self, index: QModelIndex, value, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid() or role != Qt.ItemDataRole.CheckStateRole or index.column() != 0:
            return False
        info = self.file_at_row(index.row())
        if info is None:
            return False
        entry, orig_fi, rec = info
        if rec.is_reference:
            return False

        target_state = Qt.CheckState(value) if not isinstance(value, Qt.CheckState) else value
        is_checked = target_state == Qt.CheckState.Checked
        if entry.checked[orig_fi] != is_checked:
            entry.checked[orig_fi] = is_checked
            if is_checked:
                self._selected_count += 1
                self._selected_bytes += rec.size
            else:
                self._selected_count = max(0, self._selected_count - 1)
                self._selected_bytes = max(0, self._selected_bytes - rec.size)
            self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
            self.selection_delta.emit(self._selected_count, self._selected_bytes)
        return True

    def set_entries(self, groups: List[DuplicateGroup]) -> None:
        self.beginResetModel()
        try:
            self._entries = [_build_entry(g, idx) for idx, g in enumerate(groups, 1)]
            self._selected_count = 0
            self._selected_bytes = 0
            self._rebuild_flat_index()
        finally:
            self.endResetModel()

    def clear_entries(self) -> None:
        self.beginResetModel()
        try:
            self._entries = []
            self._row_group = []
            self._row_file = []
            self._selected_count = 0
            self._selected_bytes = 0
        finally:
            self.endResetModel()

    def sort_entries(self, col: Optional[int], order: Optional[Qt.SortOrder]) -> List[DuplicateGroup]:
        if not self._entries:
            return []

        self.layoutAboutToBeChanged.emit()
        try:
            if order is None or col is None:
                self._entries.sort(key=lambda e: e.orig_index)
                for entry in self._entries:
                    entry.display_order = None
            else:
                reverse = order == Qt.SortOrder.DescendingOrder
                if col == 0:
                    for entry in self._entries:
                        entry.k_chk = sum(entry.checked)
                self._entries.sort(key=lambda e: _group_sort_key(e, col), reverse=reverse)
                for entry in self._entries:
                    _sort_entry_files(entry, col, reverse)
            self._rebuild_flat_index()
        finally:
            self.layoutChanged.emit()

        return [e.group for e in self._entries]

    def set_all_expanded(self, expanded: bool) -> None:
        if not self._entries:
            return
        self.beginResetModel()
        try:
            for entry in self._entries:
                entry.expanded = expanded
            self._rebuild_flat_index()
        finally:
            self.endResetModel()

    def toggle_group_at_row(self, row: int) -> None:
        if not self.is_group_row(row):
            return
        gi = self._row_group[row]
        entry = self._entries[gi]
        n_files = len(entry.files)
        if n_files == 0:
            return

        if entry.expanded:
            self.beginRemoveRows(QModelIndex(), row + 1, row + n_files)
            del self._row_group[row + 1:row + 1 + n_files]
            del self._row_file[row + 1:row + 1 + n_files]
            entry.expanded = False
            self.endRemoveRows()
        else:
            self.beginInsertRows(QModelIndex(), row + 1, row + n_files)
            self._row_group[row + 1:row + 1] = [gi] * n_files
            self._row_file[row + 1:row + 1] = list(range(n_files))
            entry.expanded = True
            self.endInsertRows()

    def apply_selection(
        self,
        updater: Callable[[_GroupEntry], Tuple[int, int]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> Tuple[int, int]:
        """Run a batch selection function in pure Python memory and refresh column 0 once."""
        total_groups = len(self._entries)
        if total_groups == 0:
            return 0, 0

        selected_count = 0
        selected_bytes = 0
        chunk_size = 25000 if total_groups > 25000 else total_groups

        for idx, entry in enumerate(self._entries, 1):
            c_delta, b_delta = updater(entry)
            selected_count += c_delta
            selected_bytes += b_delta
            if progress_callback and (idx % chunk_size == 0 or idx == total_groups):
                progress_callback(idx, total_groups)

        self._selected_count = selected_count
        self._selected_bytes = selected_bytes
        rc = len(self._row_group)
        if rc > 0:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(rc - 1, 0),
                [Qt.ItemDataRole.CheckStateRole],
            )
        return selected_count, selected_bytes

    def remove_deleted_paths(self, deleted_paths: Set[Path]) -> List[DuplicateGroup]:
        if not deleted_paths or not self._entries:
            return [e.group for e in self._entries]

        self.beginResetModel()
        selected_count = 0
        selected_bytes = 0
        new_entries: List[_GroupEntry] = []

        try:
            for entry in self._entries:
                rem_files: List[FileRecord] = []
                rem_checked: List[bool] = []
                for rec, is_chk in zip(entry.files, entry.checked):
                    if rec.path not in deleted_paths:
                        rem_files.append(rec)
                        keep_chk = bool(is_chk and not rec.is_reference)
                        rem_checked.append(keep_chk)
                        if keep_chk:
                            selected_count += 1
                            selected_bytes += rec.size

                if len(rem_files) > 1:
                    entry.group.files = rem_files
                    entry.files = rem_files
                    entry.checked = rem_checked
                    entry.display_order = None
                    entry.header_text = _build_group_header(entry.group)
                    entry.k_reclaim = entry.group.reclaimable_bytes
                    entry.k_mtime = max((f.mtime for f in rem_files), default=0.0)
                    f0 = rem_files[0]
                    entry.k_name = (f0.name or f0.path.name or "").lower()
                    entry.k_size = f0.size
                    entry.k_path = str(f0.path).lower()
                    new_entries.append(entry)

            self._entries = new_entries
            self._selected_count = selected_count
            self._selected_bytes = selected_bytes
            self._rebuild_flat_index()
        finally:
            self.endResetModel()

        return [e.group for e in self._entries]


class _ResultsCellDelegate(QStyledItemDelegate):
    """Paints standard file cells while skipping group rows (drawn full-width by the view)."""

    def __init__(self, model: ResultsModel, parent=None):
        super().__init__(parent)
        self._model = model
        self._chk_border_pen = QPen(QColor("#71717a"), 1.2)
        self._chk_checked_pen = QPen(QColor("#38bdf8"), 1.2)
        self._chk_tick_pen = QPen(QColor("#ffffff"), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        self._chk_unchecked_bg = QColor("#18181b")
        self._chk_checked_bg = QColor("#0284c7")

    def _checkbox_rect(self, cell_rect: QRect) -> QRect:
        size = 15
        x = cell_rect.left() + 14
        y = cell_rect.top() + (cell_rect.height() - size) // 2
        return QRect(x, y, size, size)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        row = index.row()
        if self._model.is_group_row(row):
            return
        if index.column() == 0:
            # Draw row background + selection + QSS border without native check indicator
            opt = QStyleOptionViewItem(option)
            self.initStyleOption(opt, index)
            opt.text = ""
            style = opt.widget.style() if opt.widget else QApplication.style()
            style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, opt, painter, opt.widget)

            info = self._model.file_at_row(row)
            if info is not None and not info[2].is_reference:
                box = self._checkbox_rect(option.rect)
                is_checked = info[0].checked[info[1]]
                painter.save()
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                if is_checked:
                    painter.setPen(self._chk_checked_pen)
                    painter.setBrush(self._chk_checked_bg)
                    painter.drawRoundedRect(box, 3, 3)
                    tick = QPainterPath()
                    tick.moveTo(QPointF(box.left() + 3.2, box.top() + 7.8))
                    tick.lineTo(QPointF(box.left() + 6.3, box.top() + 10.8))
                    tick.lineTo(QPointF(box.left() + 11.8, box.top() + 4.4))
                    painter.setPen(self._chk_tick_pen)
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawPath(tick)
                else:
                    painter.setPen(self._chk_border_pen)
                    painter.setBrush(self._chk_unchecked_bg)
                    painter.drawRoundedRect(box, 3, 3)
                painter.restore()
            return
        super().paint(painter, option, index)

    def editorEvent(self, event, model, option, index) -> bool:
        row = index.row()
        if self._model.is_group_row(row):
            return False
        if index.column() == 0:
            etype = event.type()
            if etype == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                info = self._model.file_at_row(row)
                if info is not None and not info[2].is_reference:
                    box = self._checkbox_rect(option.rect).adjusted(-4, -4, 4, 4)
                    pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
                    if box.contains(pos):
                        next_state = Qt.CheckState.Unchecked if info[0].checked[info[1]] else Qt.CheckState.Checked
                        return model.setData(index, next_state, Qt.ItemDataRole.CheckStateRole)
                return False
            if etype == QEvent.Type.MouseButtonPress or etype == QEvent.Type.MouseButtonDblClick:
                return False
        return super().editorEvent(event, model, option, index)


class _VirtualResultsView(QTableView):
    """High-throughput QTableView painting full-width collapsible group headers."""

    header_sort_requested = Signal(int)
    direct_sort_requested = Signal(int, object)

    def __init__(self, model: ResultsModel, parent=None):
        super().__init__(parent)
        self._model = model
        self._group_bg = QColor("#27272a")
        self._group_sel_bg = QColor("#0369a1")
        self._group_border = QColor("#2e2e33")
        self._group_fg = QColor("#38bdf8")
        self._group_sel_fg = QColor("#ffffff")
        self._chevron_pen = QPen(QColor("#a1a1aa"), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        self._group_font = QFont()
        self._group_font.setBold(True)
        self._zoom_level: int = 0

        self.setModel(model)
        self.setItemDelegate(_ResultsCellDelegate(model, self))
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        v_header = self.verticalHeader()
        v_header.setVisible(False)
        v_header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        v_header.setDefaultSectionSize(25)

    def header(self) -> QHeaderView:
        """Compatibility helper mirroring QTreeWidget.header()."""
        return self.horizontalHeader()

    def sortByColumn(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        self.direct_sort_requested.emit(column, order)

    def mousePressEvent(self, event) -> None:
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        idx = self.indexAt(pos)
        if idx.isValid() and self._model.is_group_row(idx.row()):
            col1_x = max(26, self.columnViewportPosition(1))
            if pos.x() < col1_x:
                super().mousePressEvent(event)
                self._model.toggle_group_at_row(idx.row())
                return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
                self.zoom_in()
                event.accept()
                return
            if event.key() == Qt.Key.Key_Minus:
                self.zoom_out()
                event.accept()
                return
            if event.key() == Qt.Key.Key_0:
                self.reset_zoom()
                event.accept()
                return
        if event.key() == Qt.Key.Key_Space:
            cur = self.currentIndex()
            if cur.isValid():
                row = cur.row()
                if self._model.is_group_row(row):
                    self._model.toggle_group_at_row(row)
                    return
                info = self._model.file_at_row(row)
                if info is not None and not info[2].is_reference:
                    chk_idx = self._model.index(row, 0)
                    next_state = Qt.CheckState.Unchecked if info[0].checked[info[1]] else Qt.CheckState.Checked
                    self._model.setData(chk_idx, next_state, Qt.ItemDataRole.CheckStateRole)
                    return
        super().keyPressEvent(event)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self.zoom_in()
            elif delta < 0:
                self.zoom_out()
            event.accept()
            return
        super().wheelEvent(event)

    def _apply_zoom(self) -> None:
        """Scale table font, group headers, and row height while preserving O(1) virtualization."""
        scale = 1.0 + 0.1 * self._zoom_level
        font_px = max(9, round(13 * scale))
        row_h = max(18, round(25 * scale))
        self.setStyleSheet(f"QTableView {{ font-size: {font_px}px; }}")
        f = self.font()
        f.setPixelSize(font_px)
        self.setFont(f)
        self._group_font.setPixelSize(font_px)
        self.verticalHeader().setDefaultSectionSize(row_h)
        self.viewport().update()

    def zoom_in(self) -> None:
        if self._zoom_level < 6:
            self._zoom_level += 1
            self._apply_zoom()

    def zoom_out(self) -> None:
        if self._zoom_level > -3:
            self._zoom_level -= 1
            self._apply_zoom()

    def reset_zoom(self) -> None:
        if self._zoom_level != 0:
            self._zoom_level = 0
            self._apply_zoom()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        rc = self._model.rowCount()
        if rc == 0:
            return

        rect = event.rect()
        top_row = self.rowAt(rect.top())
        bot_row = self.rowAt(rect.bottom())
        if top_row < 0:
            top_row = 0
        if bot_row < 0:
            bot_row = rc - 1

        vp_width = self.viewport().width()
        col0_x = self.columnViewportPosition(0)
        col1_x = self.columnViewportPosition(1)
        sel_model = self.selectionModel()

        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        try:
            for r in range(top_row, bot_row + 1):
                if not self._model.is_group_row(r):
                    continue
                entry = self._model.entry_at_row(r)
                if entry is None:
                    continue

                y = self.rowViewportPosition(r)
                h = self.rowHeight(r)
                row_rect = QRect(0, y, vp_width, h)
                is_sel = bool(sel_model and sel_model.isRowSelected(r, QModelIndex()))

                painter.fillRect(row_rect, self._group_sel_bg if is_sel else self._group_bg)
                painter.setPen(self._group_border)
                painter.drawLine(0, y + h - 1, vp_width, y + h - 1)

                # Vector chevron inside column 0 left gutter
                cx = col0_x + 12
                cy = y + h * 0.5
                chevron = QPainterPath()
                if entry.expanded:
                    chevron.moveTo(QPointF(cx - 4, cy - 2))
                    chevron.lineTo(QPointF(cx, cy + 3))
                    chevron.lineTo(QPointF(cx + 4, cy - 2))
                else:
                    chevron.moveTo(QPointF(cx - 2, cy - 4))
                    chevron.lineTo(QPointF(cx + 3, cy))
                    chevron.lineTo(QPointF(cx - 2, cy + 4))
                painter.setPen(self._chevron_pen)
                painter.drawPath(chevron)

                # Full-width group title starting aligned with File Name column
                text_x = max(col0_x + 24, col1_x + 4)
                text_rect = QRect(text_x, y, max(0, vp_width - text_x - 8), h)
                painter.setFont(self._group_font)
                painter.setPen(self._group_sel_fg if is_sel else self._group_fg)
                elided = painter.fontMetrics().elidedText(
                    entry.header_text, Qt.TextElideMode.ElideRight, text_rect.width()
                )
                painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)
        finally:
            painter.end()


class ResultsWidget(QWidget):
    """Duplicate results viewer backed by a virtualized flat QTableView."""

    COLUMN_LABELS = ResultsModel.COLUMN_LABELS
    selection_changed = Signal(int, "qint64")
    selection_progress = Signal(str, int, int)
    status_message = Signal(str)
    file_selected = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.groups: List[DuplicateGroup] = []
        self._selected_count: int = 0
        self._selected_bytes: int = 0
        self._current_sort_col: Optional[int] = None
        self._current_sort_order: Optional[Qt.SortOrder] = None
        self._init_ui()

    def _create_tree(self) -> _VirtualResultsView:
        self.model = ResultsModel(self)
        view = _VirtualResultsView(self.model, self)

        header = view.header()
        header.setStretchLastSection(True)
        header.setMinimumSectionSize(50)
        for col_idx in range(6):
            header.setSectionResizeMode(col_idx, QHeaderView.ResizeMode.Interactive)

        view.setColumnWidth(0, 75)   # Selection
        view.setColumnWidth(1, 260)  # File Name
        view.setColumnWidth(2, 90)   # Size
        view.setColumnWidth(3, 135)  # Date Modified
        view.setColumnWidth(4, 80)   # Similarity
        view.setColumnWidth(5, 200)  # Full Path

        header.setSectionsClickable(True)
        header.setSortIndicatorShown(False)
        header.setToolTip("Double-click column header to cycle sort: Ascending ▲ / Descending ▼ / Normal")
        header.sectionDoubleClicked.connect(self._on_header_double_clicked)

        view.direct_sort_requested.connect(self._apply_sort_state)
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        view.customContextMenuRequested.connect(self._show_context_menu)
        view.doubleClicked.connect(self._on_index_double_clicked)
        view.clicked.connect(self._on_index_clicked)
        if view.selectionModel():
            view.selectionModel().currentChanged.connect(self._on_current_index_changed)
        self.model.selection_delta.connect(self._on_model_selection_delta)
        return view

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tree = self._create_tree()
        layout.addWidget(self.tree)

    def _on_model_selection_delta(self, count: int, total_bytes: int) -> None:
        self._selected_count = count
        self._selected_bytes = total_bytes
        self.selection_changed.emit(count, total_bytes)

    def set_results(self, groups: List[DuplicateGroup]):
        """Populate the virtualized grid in O(groups) without creating widget items."""
        header = self.tree.header()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(False)
        self.groups = list(groups)
        self._selected_count = 0
        self._selected_bytes = 0
        self._current_sort_col = None
        self._current_sort_order = None

        self.model.set_entries(groups)
        total_groups = len(groups)
        if total_groups > 0:
            self.selection_progress.emit("Loading results", total_groups, total_groups)
            app = QApplication.instance()
            if app:
                app.processEvents()

        self.tree.viewport().update()
        self.file_selected.emit(None)
        self.selection_changed.emit(0, 0)

    def _reset_header_labels(self):
        """Force header repaint while keeping static clean column titles."""
        header = self.tree.header()
        if header:
            header.viewport().update()

    def _on_header_double_clicked(self, col: int):
        """Cycle column sort state: Ascending -> Descending -> Normal."""
        if not self.groups or self.model.rowCount() == 0:
            return

        current_col = self._current_sort_col
        current_order = self._current_sort_order

        if current_col != col or current_order is None:
            new_order: Optional[Qt.SortOrder] = Qt.SortOrder.AscendingOrder
        elif current_order == Qt.SortOrder.AscendingOrder:
            new_order = Qt.SortOrder.DescendingOrder
        else:
            new_order = None

        self._apply_sort_state(col, new_order)

    def _apply_sort_state(self, col: int, new_order: Optional[Qt.SortOrder]):
        if not self.groups or self.model.rowCount() == 0:
            return

        self._current_sort_col = col if new_order is not None else None
        self._current_sort_order = new_order

        app = QApplication.instance()
        if app:
            app.setOverrideCursor(Qt.CursorShape.WaitCursor)

        col_name = self.COLUMN_LABELS[col] if 0 <= col < len(self.COLUMN_LABELS) else ""
        if new_order is None:
            msg = "Restoring original scan order..."
        else:
            order_name = "Ascending" if new_order == Qt.SortOrder.AscendingOrder else "Descending"
            msg = f"Sorting results by {col_name} ({order_name})..."

        self.status_message.emit(msg)
        if app:
            app.processEvents()

        header = self.tree.header()
        try:
            if new_order is None:
                header.setSortIndicatorShown(False)
            else:
                header.setSortIndicatorShown(True)
                header.setSortIndicator(col, new_order)

            self.groups = self.model.sort_entries(col, new_order)
            self._reset_header_labels()
        finally:
            self.tree.viewport().update()
            self.status_message.emit("")
            if app:
                app.restoreOverrideCursor()

    _on_header_clicked = _on_header_double_clicked

    def clear(self):
        """Clear results while preserving headers and column geometry."""
        self.groups = []
        self._selected_count = 0
        self._selected_bytes = 0
        self._current_sort_col = None
        self._current_sort_order = None
        self.tree.header().setSortIndicatorShown(False)
        self._reset_header_labels()
        self.model.clear_entries()
        self.tree.viewport().update()
        self.file_selected.emit(None)
        self.selection_changed.emit(0, 0)

    def has_selected_files(self) -> bool:
        return self._selected_count > 0

    def get_selected_count(self) -> int:
        return self._selected_count

    def get_selected_bytes(self) -> int:
        return self._selected_bytes

    def expand_all(self):
        self.model.set_all_expanded(True)

    def collapse_all(self):
        self.model.set_all_expanded(False)

    def _run_batch_selection(self, progress_title: str, updater: Callable[[_GroupEntry], Tuple[int, int]]) -> None:
        if not self.groups:
            return
        app = QApplication.instance()
        try:
            def _on_prog(curr: int, total: int):
                self.selection_progress.emit(progress_title, curr, total)
                if app:
                    app.processEvents()

            count, total_bytes = self.model.apply_selection(updater, progress_callback=_on_prog)
            self._selected_count = count
            self._selected_bytes = total_bytes
        finally:
            self.selection_progress.emit("", 0, 0)
            self.selection_changed.emit(self._selected_count, self._selected_bytes)

    def select_smart_duplicates(self):
        """Select duplicates per group while strictly protecting Reference Folder files."""
        def _update(entry: _GroupEntry) -> Tuple[int, int]:
            paths_to_remove = apply_reference_rules(entry.group)
            cnt = 0
            b_sum = 0
            for idx, rec in enumerate(entry.files):
                if not rec.is_reference and rec.path in paths_to_remove:
                    entry.checked[idx] = True
                    cnt += 1
                    b_sum += rec.size
                else:
                    entry.checked[idx] = False
            return cnt, b_sum

        self._run_batch_selection("Selecting... ", _update)

    def select_custom(self, rule: str):
        """Apply custom per-group selection rule, keeping Reference files unchecked."""
        def _update(entry: _GroupEntry) -> Tuple[int, int]:
            non_ref = [(idx, rec) for idx, rec in enumerate(entry.files) if not rec.is_reference]
            if not non_ref:
                return 0, 0

            if rule == "invert_in_group":
                cnt = 0
                b_sum = 0
                for idx, rec in non_ref:
                    new_state = not entry.checked[idx]
                    entry.checked[idx] = new_state
                    if new_state:
                        cnt += 1
                        b_sum += rec.size
                return cnt, b_sum

            if any(rec.is_reference for rec in entry.files):
                cnt = 0
                b_sum = 0
                for idx, rec in non_ref:
                    entry.checked[idx] = True
                    cnt += 1
                    b_sum += rec.size
                return cnt, b_sum

            preserved_idx: Optional[int] = None
            if rule == "except_biggest_size":
                preserved_idx = max(non_ref, key=lambda x: x[1].size)[0]
            elif rule == "except_smallest_size":
                preserved_idx = min(non_ref, key=lambda x: x[1].size)[0]
            elif rule == "except_newest":
                preserved_idx = max(non_ref, key=lambda x: x[1].mtime)[0]
            elif rule == "except_oldest":
                preserved_idx = min(non_ref, key=lambda x: x[1].mtime)[0]
            elif rule == "except_longest_path":
                preserved_idx = max(non_ref, key=lambda x: len(str(x[1].path)))[0]
            elif rule == "except_shortest_path":
                preserved_idx = min(non_ref, key=lambda x: len(str(x[1].path)))[0]

            cnt = 0
            b_sum = 0
            for idx, rec in non_ref:
                if idx == preserved_idx:
                    entry.checked[idx] = False
                else:
                    entry.checked[idx] = True
                    cnt += 1
                    b_sum += rec.size
            return cnt, b_sum

        self._run_batch_selection("Selecting... ", _update)

    def invert_all(self):
        """Invert selection across all non-reference files."""
        def _update(entry: _GroupEntry) -> Tuple[int, int]:
            cnt = 0
            b_sum = 0
            for idx, rec in enumerate(entry.files):
                if not rec.is_reference:
                    new_state = not entry.checked[idx]
                    entry.checked[idx] = new_state
                    if new_state:
                        cnt += 1
                        b_sum += rec.size
            return cnt, b_sum

        self._run_batch_selection("Inverting... ", _update)

    def select_all(self):
        """Check all non-reference files."""
        def _update(entry: _GroupEntry) -> Tuple[int, int]:
            cnt = 0
            b_sum = 0
            for idx, rec in enumerate(entry.files):
                if not rec.is_reference:
                    entry.checked[idx] = True
                    cnt += 1
                    b_sum += rec.size
            return cnt, b_sum

        self._run_batch_selection("Selecting... ", _update)

    def unselect_all(self):
        """Uncheck all files."""
        def _update(entry: _GroupEntry) -> Tuple[int, int]:
            for idx in range(len(entry.checked)):
                entry.checked[idx] = False
            return 0, 0

        self._run_batch_selection("Unselecting... ", _update)

    def get_selected_files(self) -> List[Path]:
        """Return paths of all checked non-reference files."""
        return [rec.path for rec in self.get_selected_records()]

    def get_selected_records(self) -> List[FileRecord]:
        """Return FileRecord objects for all checked non-reference files."""
        selected: List[FileRecord] = []
        for entry in self.model._entries:
            order = entry.display_order if entry.display_order is not None else range(len(entry.files))
            for fi in order:
                rec = entry.files[fi]
                if entry.checked[fi] and not rec.is_reference:
                    selected.append(rec)
        return selected

    def remove_deleted_files(self, deleted_paths: Set[Path]):
        """Remove deleted paths from the model and discard groups left with <= 1 file."""
        if not deleted_paths:
            return

        cur_idx = self.tree.currentIndex()
        if cur_idx.isValid():
            info = self.model.file_at_row(cur_idx.row())
            if info is not None and info[2].path in deleted_paths:
                self.file_selected.emit(None)

        self.groups = self.model.remove_deleted_paths(deleted_paths)
        self._selected_count = self.model._selected_count
        self._selected_bytes = self.model._selected_bytes
        self.selection_changed.emit(self._selected_count, self._selected_bytes)

    def _emit_preview_for_index(self, index: QModelIndex) -> None:
        if not index.isValid():
            self.file_selected.emit(None)
            return
        info = self.model.file_at_row(index.row())
        self.file_selected.emit(info[2] if info is not None else None)

    def _on_index_clicked(self, index: QModelIndex):
        self._emit_preview_for_index(index)

    def _on_current_index_changed(self, current: QModelIndex, previous: QModelIndex):
        self._emit_preview_for_index(current)

    def _on_index_double_clicked(self, index: QModelIndex):
        if not index.isValid():
            return
        row = index.row()
        if self.model.is_group_row(row):
            self.model.toggle_group_at_row(row)
            return

        info = self.model.file_at_row(row)
        if info is not None and info[2].path.exists():
            try:
                os.startfile(str(info[2].path))
            except Exception:
                pass

    def _show_context_menu(self, pos):
        idx = self.tree.indexAt(pos)
        menu = QMenu(self)

        if idx.isValid():
            info = self.model.file_at_row(idx.row())
            if info is not None:
                file_rec = info[2]
                act_open_folder = QAction("Open Containing Folder", self)
                act_open_folder.triggered.connect(lambda: self._open_in_explorer(file_rec.path))
                menu.addAction(act_open_folder)

                act_open_file = QAction("Open File", self)
                act_open_file.triggered.connect(lambda: os.startfile(str(file_rec.path)))
                menu.addAction(act_open_file)
                menu.addSeparator()

        act_expand_all = QAction("Expand All Groups", self)
        act_expand_all.triggered.connect(self.expand_all)
        menu.addAction(act_expand_all)

        act_collapse_all = QAction("Collapse All Groups", self)
        act_collapse_all.triggered.connect(self.collapse_all)
        menu.addAction(act_collapse_all)

        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _open_in_explorer(self, path: Path):
        if path.exists():
            subprocess.run(["explorer", f"/select,{path}"])
