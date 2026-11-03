"""
Painel de Pastas Incluídas e Excluídas com suporte completo a Drag & Drop do Windows
e botão toggle da Pasta de Referência (Ref).
"""

from pathlib import Path
from typing import List
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QGroupBox, QTableWidget,
    QTableWidgetItem, QPushButton, QFileDialog, QHeaderView, QListWidget, QListWidgetItem
)

from dupeclean.core.models import ScanPath


class DropTableWidget(QTableWidget):
    """Tabela de pastas incluídas com suporte nativo a Drag & Drop do Windows Explorer."""
    paths_dropped = Signal(list)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAcceptDrops(True)
        self.setDragDropMode(QTableWidget.DragDropMode.DropOnly)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            paths = []
            for url in event.mimeData().urls():
                p = Path(url.toLocalFile())
                if p.is_dir():
                    paths.append(p)
            if paths:
                self.paths_dropped.emit(paths)
            event.acceptProposedAction()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete:
            selected_rows = set(index.row() for index in self.selectedIndexes())
            for row in sorted(selected_rows, reverse=True):
                self.removeRow(row)
            event.accept()
        else:
            super().keyPressEvent(event)


class DeletableListWidget(QListWidget):
    """Lista de pastas com suporte a Drag & Drop e à remoção via tecla Delete."""
    paths_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DragDropMode.DropOnly)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            paths = []
            for url in event.mimeData().urls():
                p = Path(url.toLocalFile())
                if p.is_dir():
                    paths.append(p)
            if paths:
                self.paths_dropped.emit(paths)
            event.acceptProposedAction()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete:
            for item in self.selectedItems():
                self.takeItem(self.row(item))
            event.accept()
        else:
            super().keyPressEvent(event)


class FolderPanel(QWidget):
    """Painel inferior com pastas incluídas e excluídas."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(10)

        # -------------------------------------------------------------
        # 1. Pastas Incluídas (com botão Ref e Drag & Drop)
        # -------------------------------------------------------------
        inc_group = QGroupBox("Pastas a Escanear (Arraste e solte pastas aqui)")
        inc_layout = QVBoxLayout(inc_group)

        self.table_included = DropTableWidget(0, 2)
        self.table_included.setHorizontalHeaderLabels(["Ref", "Caminho da Pasta"])
        self.table_included.verticalHeader().setVisible(False)  # Remove o numerador lateral
        self.table_included.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table_included.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table_included.setColumnWidth(0, 50)
        self.table_included.paths_dropped.connect(self._add_multiple_included_paths)
        inc_layout.addWidget(self.table_included)

        # Botões de ação para pastas incluídas
        btn_layout_inc = QHBoxLayout()
        self.btn_add_inc = QPushButton("+ Adicionar Pasta")
        self.btn_add_inc.clicked.connect(self._browse_included_folder)
        self.btn_remove_inc = QPushButton("- Remover Selecionada")
        self.btn_remove_inc.clicked.connect(self._remove_selected_included)
        self.btn_clear_inc = QPushButton("Limpar Tudo")
        self.btn_clear_inc.clicked.connect(self._clear_all_included)

        btn_layout_inc.addWidget(self.btn_add_inc)
        btn_layout_inc.addWidget(self.btn_remove_inc)
        btn_layout_inc.addWidget(self.btn_clear_inc)
        inc_layout.addLayout(btn_layout_inc)

        main_layout.addWidget(inc_group, stretch=1)

        # -------------------------------------------------------------
        # 2. Pastas Excluídas (com suporte a Drag & Drop)
        # -------------------------------------------------------------
        exc_group = QGroupBox("Pastas Excluídas (Arraste e solte pastas aqui)")
        exc_layout = QVBoxLayout(exc_group)

        self.list_excluded = DeletableListWidget()
        self.list_excluded.paths_dropped.connect(self._add_multiple_excluded_paths)
        exc_layout.addWidget(self.list_excluded)

        btn_layout_exc = QHBoxLayout()
        self.btn_add_exc = QPushButton("+ Adicionar Exclusão")
        self.btn_add_exc.clicked.connect(self._browse_excluded_folder)
        self.btn_remove_exc = QPushButton("- Remover")
        self.btn_remove_exc.clicked.connect(self._remove_selected_excluded)

        btn_layout_exc.addWidget(self.btn_add_exc)
        btn_layout_exc.addWidget(self.btn_remove_exc)
        exc_layout.addLayout(btn_layout_exc)

        main_layout.addWidget(exc_group, stretch=1)

    def _browse_included_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Selecionar Pasta para Escaneamento")
        if folder:
            self.add_included_path(Path(folder), is_reference=False)

    def _add_multiple_included_paths(self, paths: List[Path]):
        for p in paths:
            self.add_included_path(p, is_reference=False)

    def _add_multiple_excluded_paths(self, paths: List[Path]):
        for p in paths:
            self.add_excluded_path(p)

    def add_included_path(self, path: Path, is_reference: bool = False):
        path_str = str(path.resolve())

        # Evita duplicatas na lista de pastas
        for row in range(self.table_included.rowCount()):
            item = self.table_included.item(row, 1)
            if item and item.text().lower() == path_str.lower():
                return

        row = self.table_included.rowCount()
        self.table_included.insertRow(row)

        # Botão quadradinho com Checkmark toggle (Estilo Krokiet)
        cell_widget = QWidget()
        cell_layout = QHBoxLayout(cell_widget)
        cell_layout.setContentsMargins(0, 0, 0, 0)
        cell_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        ref_btn = QPushButton("✓" if is_reference else "")
        ref_btn.setObjectName("refSquare")
        ref_btn.setCheckable(True)
        ref_btn.setChecked(is_reference)
        ref_btn.setToolTip("Marcar como Pasta de Referência.\nArquivos nesta pasta NUNCA serão sugeridos para exclusão.")
        ref_btn.toggled.connect(lambda checked, b=ref_btn: b.setText("✓" if checked else ""))

        cell_layout.addWidget(ref_btn)
        self.table_included.setCellWidget(row, 0, cell_widget)

        # Caminho
        path_item = QTableWidgetItem(path_str)
        path_item.setFlags(path_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.table_included.setItem(row, 1, path_item)

    def _remove_selected_included(self):
        selected_rows = set(index.row() for index in self.table_included.selectedIndexes())
        for row in sorted(selected_rows, reverse=True):
            self.table_included.removeRow(row)

    def _clear_all_included(self):
        self.table_included.setRowCount(0)

    def _browse_excluded_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Selecionar Pasta a Ignorar")
        if folder:
            self.add_excluded_path(Path(folder))

    def add_excluded_path(self, path: Path):
        path_str = str(path.resolve())
        # Evita duplicatas
        for idx in range(self.list_excluded.count()):
            if self.list_excluded.item(idx).text().lower() == path_str.lower():
                return
        self.list_excluded.addItem(QListWidgetItem(path_str))

    def _remove_selected_excluded(self):
        for item in self.list_excluded.selectedItems():
            self.list_excluded.takeItem(self.list_excluded.row(item))

    def get_scan_paths(self) -> List[ScanPath]:
        """Extrai a lista de ScanPath configurada com a flag de referência."""
        scan_paths: List[ScanPath] = []
        for row in range(self.table_included.rowCount()):
            path_item = self.table_included.item(row, 1)
            cell_widget = self.table_included.cellWidget(row, 0)
            btn = cell_widget.findChild(QPushButton) if cell_widget else None
            if path_item:
                is_ref = btn.isChecked() if btn else False
                scan_paths.append(ScanPath(
                    path=Path(path_item.text()),
                    is_reference=is_ref
                ))
        return scan_paths

    def get_excluded_paths(self) -> List[Path]:
        """Extrai a lista de pastas excluídas."""
        excluded: List[Path] = []
        for idx in range(self.list_excluded.count()):
            excluded.append(Path(self.list_excluded.item(idx).text()))
        return excluded
