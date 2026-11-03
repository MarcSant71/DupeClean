"""
Duplicate results tree widget with group headers,
selection rules, and visual Reference Folder protection.
"""

from pathlib import Path
from datetime import datetime
from typing import List, Set
import os
import subprocess

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor, QBrush, QFont, QAction
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem,
    QHeaderView, QMenu
)

from dupeclean.core.models import DuplicateGroup, FileRecord
from dupeclean.core.matcher import apply_reference_rules


def _format_size(size_bytes: int) -> str:
    """Formata bytes em KB, MB ou GB legíveis."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


class SortableGroupItem(QTreeWidgetItem):
    """Item de cabeçalho do grupo com ordenação inteligente por coluna."""
    def __init__(self, group: DuplicateGroup, parent=None):
        super().__init__(parent)
        self.group = group

    def __lt__(self, other: 'QTreeWidgetItem') -> bool:
        if not isinstance(other, SortableGroupItem):
            return super().__lt__(other)
        tree = self.treeWidget()
        col = tree.sortColumn() if tree else 1

        if col in (0, 1):
            return self.group.group_id < other.group.group_id
        elif col == 2:
            return self.group.reclaimable_bytes < other.group.reclaimable_bytes
        elif col == 3:
            m_self = max((f.mtime for f in self.group.files), default=0.0)
            m_other = max((f.mtime for f in other.group.files), default=0.0)
            return m_self < m_other
        elif col == 4:
            return self.group.similarity < other.group.similarity
        elif col == 5:
            ref_self = any(f.is_reference for f in self.group.files)
            ref_other = any(f.is_reference for f in other.group.files)
            return ref_self < ref_other
        elif col == 6:
            p_self = str(self.group.files[0].path).lower() if self.group.files else ""
            p_other = str(other.group.files[0].path).lower() if other.group.files else ""
            return p_self < p_other

        return super().__lt__(other)


class SortableFileItem(QTreeWidgetItem):
    """Item de arquivo filho com ordenação exata por tipo de dado (bytes, timestamp, texto)."""
    def __init__(self, file_rec: FileRecord, similarity: float, parent=None):
        super().__init__(parent)
        self.file_rec = file_rec
        self.similarity = similarity

    def __lt__(self, other: 'QTreeWidgetItem') -> bool:
        if not isinstance(other, SortableFileItem):
            return super().__lt__(other)
        tree = self.treeWidget()
        col = tree.sortColumn() if tree else 1

        if col == 0:
            is_chk_self = (self.checkState(0) == Qt.CheckState.Checked) if not self.file_rec.is_reference else False
            is_chk_other = (other.checkState(0) == Qt.CheckState.Checked) if not other.file_rec.is_reference else False
            if self.file_rec.is_reference != other.file_rec.is_reference:
                return self.file_rec.is_reference < other.file_rec.is_reference
            return is_chk_self < is_chk_other
        elif col == 1:
            return self.file_rec.name.lower() < other.file_rec.name.lower()
        elif col == 2:
            return self.file_rec.size < other.file_rec.size
        elif col == 3:
            return self.file_rec.mtime < other.file_rec.mtime
        elif col == 4:
            return self.similarity < other.similarity
        elif col == 5:
            return self.file_rec.is_reference < other.file_rec.is_reference
        elif col == 6:
            return str(self.file_rec.path).lower() < str(other.file_rec.path).lower()

        return super().__lt__(other)


class ResultsWidget(QWidget):
    """Visualizador em árvore das duplicatas encontradas."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.groups: List[DuplicateGroup] = []
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(7)
        self.tree.setHeaderLabels([
            "Selection", "File Name", "Size", "Date Modified",
            "Similarity", "Origin", "Full Path"
        ])

        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)

        # Habilita ordenação interativa ao clicar nos cabeçalhos
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        self.tree.setSortingEnabled(True)

        self.tree.setColumnWidth(1, 260)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.itemChanged.connect(self._on_item_changed)

        layout.addWidget(self.tree)

    def set_results(self, groups: List[DuplicateGroup]):
        """Popula a árvore com os grupos de duplicatas encontrados."""
        self.tree.setSortingEnabled(False)
        self.groups = groups
        self.tree.clear()

        for group in groups:
            # Item Pai: Cabeçalho do Grupo
            reclaim_str = _format_size(group.reclaimable_bytes)
            sim_pct = f"{group.similarity * 100:.1f}%"
            group_header = (
                f"Group #{group.group_id}  •  {len(group.files)} Files  •  "
                f"Similarity: {sim_pct}  •  Reclaimable: {reclaim_str}"
            )

            parent_item = SortableGroupItem(group, self.tree)
            parent_item.setText(1, group_header)
            parent_item.setFirstColumnSpanned(True)

            font = QFont()
            font.setBold(True)
            parent_item.setFont(1, font)
            parent_item.setBackground(0, QBrush(QColor("#27272a")))
            parent_item.setForeground(1, QBrush(QColor("#38bdf8")))

            # Child items: Files in group
            for file_rec in group.files:
                child_item = SortableFileItem(file_rec, group.similarity, parent_item)

                # Selection control (Reference files are 100% protected and uncheckable)
                if file_rec.is_reference:
                    child_item.setFlags(child_item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                    child_item.setText(0, "🔒")
                    child_item.setTextAlignment(0, Qt.AlignmentFlag.AlignCenter)
                    child_item.setToolTip(0, "Protected file (Reference Folder). Cannot be selected or deleted.")
                else:
                    child_item.setFlags(child_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    child_item.setCheckState(0, Qt.CheckState.Unchecked)

                # Name
                child_item.setText(1, file_rec.name)

                # Size
                child_item.setText(2, _format_size(file_rec.size))

                # Date Modified
                mtime_str = datetime.fromtimestamp(file_rec.mtime).strftime("%m/%d/%Y %H:%M:%S")
                child_item.setText(3, mtime_str)

                # Similarity
                child_item.setText(4, sim_pct)

                # Origin (Highlight reference files)
                if file_rec.is_reference:
                    child_item.setText(5, "[REFERENCE]")
                    child_item.setForeground(5, QBrush(QColor("#f59e0b")))
                    child_item.setToolTip(5, "Protected file in Reference Folder.\nWill never be suggested for deletion.")
                else:
                    child_item.setText(5, "Duplicate")
                    child_item.setForeground(5, QBrush(QColor("#a1a1aa")))

                # Caminho Completo
                child_item.setText(6, str(file_rec.path))
                child_item.setData(0, Qt.ItemDataRole.UserRole, file_rec)

            parent_item.setExpanded(True)

        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(1, Qt.SortOrder.AscendingOrder)

    def select_smart_duplicates(self):
        """
        Aplica a seleção inteligente:
        - Protege 100% dos arquivos de referência.
        - Se houver arquivos de referência no grupo, marca os demais.
        - Se não houver referência, mantém o maior/mais recente e marca os outros.
        """
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            parent_item = root.child(i)
            group_files = []
            child_items = []

            for j in range(parent_item.childCount()):
                child = parent_item.child(j)
                file_rec: FileRecord = child.data(0, Qt.ItemDataRole.UserRole)
                group_files.append(file_rec)
                child_items.append(child)

            if not group_files:
                continue

            temp_group = DuplicateGroup(group_id=0, files=group_files)
            to_remove = apply_reference_rules(temp_group)

            for child in child_items:
                file_rec: FileRecord = child.data(0, Qt.ItemDataRole.UserRole)
                if not file_rec.is_reference:
                    if file_rec.path in to_remove:
                        child.setCheckState(0, Qt.CheckState.Checked)
                    else:
                        child.setCheckState(0, Qt.CheckState.Unchecked)

    def select_custom(self, rule: str):
        """
        Aplica regras de seleção avançadas por grupo (estilo Krokiet).
        Arquivos de referência são 100% protegidos e nunca marcados.
        """
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            parent_item = root.child(i)
            child_items = []
            non_ref_children = []

            for j in range(parent_item.childCount()):
                child = parent_item.child(j)
                file_rec: FileRecord = child.data(0, Qt.ItemDataRole.UserRole)
                child_items.append((child, file_rec))
                if not file_rec.is_reference:
                    non_ref_children.append((child, file_rec))

            if not non_ref_children:
                continue

            # Inverter seleção dentro do próprio grupo
            if rule == "invert_in_group":
                for child, _ in non_ref_children:
                    curr = child.checkState(0)
                    new_st = Qt.CheckState.Unchecked if curr == Qt.CheckState.Checked else Qt.CheckState.Checked
                    child.setCheckState(0, new_st)
                continue

            has_reference = any(rec.is_reference for _, rec in child_items)
            # Se o grupo já possui arquivo de referência, ele é o preservado. Todos os outros são marcados.
            if has_reference:
                for child, _ in non_ref_children:
                    child.setCheckState(0, Qt.CheckState.Checked)
                continue

            # Se não há referência, preserva exatamente 1 arquivo conforme a regra
            preserved_child = None
            if rule == "except_biggest_size":
                preserved_child = max(non_ref_children, key=lambda x: x[1].size)[0]
            elif rule == "except_smallest_size":
                preserved_child = min(non_ref_children, key=lambda x: x[1].size)[0]
            elif rule == "except_newest":
                preserved_child = max(non_ref_children, key=lambda x: x[1].mtime)[0]
            elif rule == "except_oldest":
                preserved_child = min(non_ref_children, key=lambda x: x[1].mtime)[0]
            elif rule == "except_longest_path":
                preserved_child = max(non_ref_children, key=lambda x: len(str(x[1].path)))[0]
            elif rule == "except_shortest_path":
                preserved_child = min(non_ref_children, key=lambda x: len(str(x[1].path)))[0]

            for child, _ in non_ref_children:
                if child is preserved_child:
                    child.setCheckState(0, Qt.CheckState.Unchecked)
                else:
                    child.setCheckState(0, Qt.CheckState.Checked)

    def invert_all(self):
        """Inverte a seleção de todos os arquivos não-referência da árvore inteira."""
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            parent = root.child(i)
            for j in range(parent.childCount()):
                child = parent.child(j)
                file_rec: FileRecord = child.data(0, Qt.ItemDataRole.UserRole)
                if file_rec and not file_rec.is_reference:
                    curr = child.checkState(0)
                    new_st = Qt.CheckState.Unchecked if curr == Qt.CheckState.Checked else Qt.CheckState.Checked
                    child.setCheckState(0, new_st)

    def select_all(self):
        """Marca todos os arquivos (exceto os de referência)."""
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            parent = root.child(i)
            for j in range(parent.childCount()):
                child = parent.child(j)
                file_rec: FileRecord = child.data(0, Qt.ItemDataRole.UserRole)
                if file_rec and not file_rec.is_reference:
                    child.setCheckState(0, Qt.CheckState.Checked)

    def unselect_all(self):
        """Desmarca todos os arquivos."""
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            parent = root.child(i)
            for j in range(parent.childCount()):
                child = parent.child(j)
                file_rec: FileRecord = child.data(0, Qt.ItemDataRole.UserRole)
                if file_rec and not file_rec.is_reference:
                    child.setCheckState(0, Qt.CheckState.Unchecked)

    def get_selected_files(self) -> List[Path]:
        """Retorna os caminhos de todos os arquivos selecionados para exclusão."""
        selected: List[Path] = []
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            parent = root.child(i)
            for j in range(parent.childCount()):
                child = parent.child(j)
                file_rec: FileRecord = child.data(0, Qt.ItemDataRole.UserRole)
                if file_rec and not file_rec.is_reference and child.checkState(0) == Qt.CheckState.Checked:
                    selected.append(file_rec.path)
        return selected

    def _on_item_changed(self, item: QTreeWidgetItem, column: int):
        """Garante que nenhum item de referência possa ter checkState marcado."""
        if column == 0 and item.childCount() == 0:
            file_rec: Optional[FileRecord] = item.data(0, Qt.ItemDataRole.UserRole)
            if file_rec and file_rec.is_reference and item.checkState(0) != Qt.CheckState.Unchecked:
                item.setCheckState(0, Qt.CheckState.Unchecked)

    def remove_deleted_files(self, deleted_paths: Set[Path]):
        """Remove da árvore os arquivos que foram excluídos com sucesso."""
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount() - 1, -1, -1):
            parent = root.child(i)
            for j in range(parent.childCount() - 1, -1, -1):
                child = parent.child(j)
                file_rec: FileRecord = child.data(0, Qt.ItemDataRole.UserRole)
                if file_rec.path in deleted_paths:
                    parent.removeChild(child)

            # Se o grupo ficou com 1 ou 0 arquivos, remove o grupo inteiro
            if parent.childCount() <= 1:
                root.removeChild(parent)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, column: int):
        """Ao clicar duas vezes em um arquivo, abre no player ou visualizador padrão do Windows."""
        # Se for cabeçalho de grupo (tem filhos), preserva o comportamento de expandir/recolher
        if item.childCount() > 0:
            return

        file_rec: FileRecord = item.data(0, Qt.ItemDataRole.UserRole)
        if file_rec and file_rec.path.exists():
            try:
                os.startfile(str(file_rec.path))
            except Exception:
                pass

    def _show_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item or item.childCount() > 0:
            return  # Clicou no grupo ou no vazio

        file_rec: FileRecord = item.data(0, Qt.ItemDataRole.UserRole)
        if not file_rec:
            return

        menu = QMenu(self)

        act_open_folder = QAction("Open Containing Folder", self)
        act_open_folder.triggered.connect(lambda: self._open_in_explorer(file_rec.path))
        menu.addAction(act_open_folder)

        act_open_file = QAction("Open File", self)
        act_open_file.triggered.connect(lambda: os.startfile(str(file_rec.path)))
        menu.addAction(act_open_file)

        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _open_in_explorer(self, path: Path):
        """Abre o Windows Explorer com o arquivo selecionado."""
        if path.exists():
            subprocess.run(["explorer", f"/select,{path}"])
