"""
Duplicate results tree widget with group headers,
selection rules, and visual Reference Folder protection.
"""

from pathlib import Path
from datetime import datetime
from typing import List, Set, Optional
import os
import subprocess

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QColor, QBrush, QFont, QAction
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem,
    QHeaderView, QMenu, QApplication
)

from dupeclean.core.models import DuplicateGroup, FileRecord
from dupeclean.core.matcher import apply_reference_rules


def format_size(size_bytes: int) -> str:
    """Formata bytes em KB, MB ou GB legíveis."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


_format_size = format_size


class SortableGroupItem(QTreeWidgetItem):
    """Item de cabeçalho do grupo com ordenação inteligente por coluna e chaves pré-cacheadas para alta performance."""
    _sort_col: int = 1
    _sort_normal: bool = False

    def __init__(self, group: DuplicateGroup, original_index: int = 0, parent=None):
        super().__init__(parent)
        self.group = group
        self.original_index: int = original_index
        self.k_orig: int = original_index
        self.k_chk: int = 0

        # Pré-cache de chaves de comparação para eliminar chamadas e alocações repetitivas no __lt__
        if group.files:
            f0 = group.files[0]
            self.k_name: str = (f0.name or f0.path.name or "").lower()
            self.k_size: int = f0.size
            self.k_path: str = str(f0.path).lower()
            self.k_mtime: float = max((f.mtime for f in group.files), default=0.0)
        else:
            self.k_name = ""
            self.k_size = 0
            self.k_path = ""
            self.k_mtime = 0.0

        self.k_reclaim: int = group.reclaimable_bytes
        self.k_sim: float = group.similarity

        self.k_name_tuple = (self.k_name, self.k_orig)
        self.k_size_tuple = (self.k_size, self.k_reclaim, self.k_orig)
        self.k_mtime_tuple = (self.k_mtime, self.k_orig)
        self.k_sim_tuple = (self.k_sim, self.k_reclaim, self.k_orig)
        self.k_path_tuple = (self.k_path, self.k_orig)

        self.setFlags(self.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)

    def __lt__(self, other: 'QTreeWidgetItem') -> bool:
        if not isinstance(other, SortableGroupItem):
            return super().__lt__(other)
        if SortableGroupItem._sort_normal:
            return self.k_orig < other.k_orig

        col = SortableGroupItem._sort_col
        if col == 0:
            return (self.k_chk, self.k_orig) < (other.k_chk, other.k_orig)
        elif col == 1:
            return self.k_name_tuple < other.k_name_tuple
        elif col == 2:
            return self.k_size_tuple < other.k_size_tuple
        elif col == 3:
            return self.k_mtime_tuple < other.k_mtime_tuple
        elif col == 4:
            return self.k_sim_tuple < other.k_sim_tuple
        elif col == 5:
            return self.k_path_tuple < other.k_path_tuple

        return self.k_orig < other.k_orig


class SortableFileItem(QTreeWidgetItem):
    """Item de arquivo filho com ordenação exata por tipo de dado (bytes, timestamp, texto) e chaves cacheadas."""
    _sort_col: int = 1
    _sort_normal: bool = False

    def __init__(self, file_rec: FileRecord, similarity: float, original_index: int = 0, parent=None):
        super().__init__(parent)
        self.file_rec = file_rec
        self.similarity = similarity
        self.original_index: int = original_index
        self.k_orig: int = original_index
        self.k_name: str = (file_rec.name or file_rec.path.name or "").lower()
        self.k_size: int = file_rec.size
        self.k_mtime: float = file_rec.mtime
        self.k_sim: float = similarity
        self.k_path: str = str(file_rec.path).lower()

        self.k_name_tuple = (self.k_name, self.k_orig)
        self.k_size_tuple = (self.k_size, self.k_orig)
        self.k_mtime_tuple = (self.k_mtime, self.k_orig)
        self.k_sim_tuple = (self.k_sim, self.k_orig)
        self.k_path_tuple = (self.k_path, self.k_orig)

    def __lt__(self, other: 'QTreeWidgetItem') -> bool:
        if not isinstance(other, SortableFileItem):
            return super().__lt__(other)
        if SortableFileItem._sort_normal:
            return self.k_orig < other.k_orig

        col = SortableFileItem._sort_col
        if col == 0:
            chk_self = (self.checkState(0) == Qt.CheckState.Checked) if not self.file_rec.is_reference else False
            chk_other = (other.checkState(0) == Qt.CheckState.Checked) if not other.file_rec.is_reference else False
            if self.file_rec.is_reference != other.file_rec.is_reference:
                return self.file_rec.is_reference < other.file_rec.is_reference
            return (chk_self, self.k_orig) < (chk_other, other.k_orig)
        elif col == 1:
            return self.k_name_tuple < other.k_name_tuple
        elif col == 2:
            return self.k_size_tuple < other.k_size_tuple
        elif col == 3:
            return self.k_mtime_tuple < other.k_mtime_tuple
        elif col == 4:
            return self.k_sim_tuple < other.k_sim_tuple
        elif col == 5:
            return self.k_path_tuple < other.k_path_tuple

        return self.k_orig < other.k_orig


class ResultsWidget(QWidget):
    """Visualizador em árvore das duplicatas encontradas."""
    COLUMN_LABELS = [
        "Selection", "File Name", "Size", "Date Modified",
        "Similarity", "Full Path"
    ]
    selection_changed = Signal(int, "qint64")  # (count, total_bytes) - qint64 suporta até 9 Exabytes sem overflow
    selection_progress = Signal(str, int, int) # (title, current, total) para barra de progresso não-bloqueante
    status_message = Signal(str)               # Mensagem amigável de status para o rodapé
    file_selected = Signal(object)             # Optional[FileRecord]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.groups: List[DuplicateGroup] = []
        self._selected_count: int = 0
        self._selected_bytes: int = 0
        self._init_ui()

    def _create_tree(self) -> QTreeWidget:
        """Cria e configura uma instância limpa do QTreeWidget com todas as conexões e colunas calibradas."""
        tree = QTreeWidget()
        tree.setColumnCount(6)
        tree.setHeaderLabels([
            "Selection", "File Name", "Size", "Date Modified",
            "Similarity", "Full Path"
        ])

        header = tree.header()
        # Colunas interativas com stretch na última seção (Full Path) para preencher a largura sem quebrar os cantos
        header.setStretchLastSection(True)
        header.setMinimumSectionSize(50)
        for col_idx in range(6):
            header.setSectionResizeMode(col_idx, QHeaderView.ResizeMode.Interactive)

        # Calibragem de indentação e larguras iniciais
        tree.setIndentation(12)
        tree.setColumnWidth(0, 75)   # Selection
        tree.setColumnWidth(1, 260)  # File Name
        tree.setColumnWidth(2, 90)   # Size
        tree.setColumnWidth(3, 135)  # Date Modified
        tree.setColumnWidth(4, 80)   # Similarity
        tree.setColumnWidth(5, 200)  # Full Path (expande para preencher o espaço restante)

        # Permite rolagem horizontal suave se as colunas excederem o espaço disponível
        tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        # Habilita duplo clique interativo nos cabeçalhos para alternar entre 3 estados: Crescente / Decrescente / Normal
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(False)
        header.setToolTip("Double-click column header to cycle sort: Ascending ▲ / Descending ▼ / Normal")
        header.sectionDoubleClicked.connect(self._on_header_double_clicked)

        tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tree.customContextMenuRequested.connect(self._show_context_menu)
        tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        tree.itemClicked.connect(self._on_item_clicked)
        tree.currentItemChanged.connect(self._on_current_item_changed)
        tree.itemChanged.connect(self._on_item_changed)
        return tree

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tree = self._create_tree()
        layout.addWidget(self.tree)

    def set_results(self, groups: List[DuplicateGroup]):
        """Popula a árvore com os grupos de duplicatas encontrados em lote de alta performance."""
        self.tree.viewport().setUpdatesEnabled(False)
        self.tree.blockSignals(True)
        if self.tree.topLevelItemCount() > 0:
            self.tree.invisibleRootItem().takeChildren()

        self.tree.header().setSectionsClickable(True)
        self.tree.header().setSortIndicatorShown(False)
        self.groups = groups
        self._selected_count = 0
        self._selected_bytes = 0
        self._current_sort_col = None
        self._current_sort_order = None
        self._reset_header_labels()
        self.file_selected.emit(None)

        bold_font = QFont()
        bold_font.setBold(True)
        bg_brush = QBrush(QColor("#27272a"))
        fg_brush = QBrush(QColor("#38bdf8"))
        ref_row_bg = QBrush(QColor("#202024"))
        ref_row_fg = QBrush(QColor("#71717a"))

        top_level_items: List[QTreeWidgetItem] = []
        batch_chunk_size = 2000
        app = QApplication.instance()

        date_cache: Dict[int, str] = {}
        def _get_mtime_str(ts: float) -> str:
            sec = int(ts)
            val = date_cache.get(sec)
            if val is None:
                val = datetime.fromtimestamp(ts).strftime("%m/%d/%Y %H:%M:%S")
                if len(date_cache) < 50000:
                    date_cache[sec] = val
            return val

        total_groups = len(groups)

        for idx, group in enumerate(groups, 1):
            # Item Pai: Cabeçalho do Grupo
            reclaim_str = _format_size(group.reclaimable_bytes)
            sim_pct = "100.0%" if group.similarity >= 0.9999 else f"{group.similarity * 100:.1f}%"
            group_header = (
                f"Group #{group.group_id}  •  {len(group.files)} Files  •  "
                f"Similarity: {sim_pct}  •  Reclaimable: {reclaim_str}"
            )

            parent_item = SortableGroupItem(group, original_index=idx)
            parent_item.setText(1, group_header)
            parent_item.setFirstColumnSpanned(True)
            parent_item.setFont(1, bold_font)
            parent_item.setBackground(0, bg_brush)
            parent_item.setForeground(1, fg_brush)

            # Child items: Files in group
            child_items: List[QTreeWidgetItem] = []
            for file_idx, file_rec in enumerate(group.files):
                child_item = SortableFileItem(file_rec, group.similarity, original_index=file_idx)

                # Name
                child_item.setText(1, file_rec.name)

                # Size
                child_item.setText(2, _format_size(file_rec.size))

                # Date Modified
                child_item.setText(3, _get_mtime_str(file_rec.mtime))

                # Similarity
                child_item.setText(4, sim_pct)

                # Caminho Completo (coluna 5)
                child_item.setText(5, str(file_rec.path))
                child_item.setData(0, Qt.ItemDataRole.UserRole, file_rec)

                # Selection control & Visual styling:
                # Reference files are 100% protected, uncheckable, and painted in muted disabled gray across all columns.
                if file_rec.is_reference:
                    child_item.setFlags(child_item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                    child_item.setText(0, "")
                    for c in range(6):
                        child_item.setBackground(c, ref_row_bg)
                        child_item.setForeground(c, ref_row_fg)
                        child_item.setToolTip(c, "Protected file (Reference Folder). Cannot be selected or deleted.")
                else:
                    child_item.setFlags(child_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    child_item.setCheckState(0, Qt.CheckState.Unchecked)

                child_items.append(child_item)

            parent_item.addChildren(child_items)
            top_level_items.append(parent_item)

            if idx % batch_chunk_size == 0:
                self.tree.addTopLevelItems(top_level_items)
                top_level_items = []
                self.selection_progress.emit("Loading results", idx, total_groups)
                if app:
                    app.processEvents()

        if top_level_items:
            self.tree.addTopLevelItems(top_level_items)

        if total_groups > 0:
            self.selection_progress.emit("Loading results", total_groups, total_groups)
            if app:
                app.processEvents()

        # Expande automaticamente todos os grupos recém-carregados (0.4s) sem reordenação destrutiva síncrona
        self.tree.expandAll()
        self.tree.header().setSectionsClickable(True)
        self.tree.blockSignals(False)
        self.tree.viewport().setUpdatesEnabled(True)
        self.tree.viewport().update()
        if app:
            app.processEvents()
        self.selection_changed.emit(0, 0)

    def _reset_header_labels(self):
        """Restaura os títulos limpos dos cabeçalhos sem setas de ordenação."""
        header_item = self.tree.headerItem()
        if header_item:
            for i, title in enumerate(self.COLUMN_LABELS):
                header_item.setText(i, title)
            if self.tree.header():
                self.tree.header().viewport().update()

    def _on_header_double_clicked(self, col: int):
        """Alterna a ordenação com duplo clique em 3 estados: Crescente -> Decrescente -> Normal (sem ordenação)."""
        if not self.groups:
            return

        root = self.tree.invisibleRootItem()
        total_items = root.childCount()
        if total_items == 0:
            return

        header = self.tree.header()
        current_col = getattr(self, "_current_sort_col", None)
        current_order = getattr(self, "_current_sort_order", None)

        # Ciclo de 3 estados:
        # Se coluna diferente ou estado Normal -> 1º clique duplo: Crescente
        # Se mesma coluna e Crescente -> 2º clique duplo: Decrescente
        # Se mesma coluna e Decrescente -> 3º clique duplo: Normal (Sem ordenação)
        if current_col != col or current_order is None:
            new_order = Qt.SortOrder.AscendingOrder
        elif current_order == Qt.SortOrder.AscendingOrder:
            new_order = Qt.SortOrder.DescendingOrder
        else:
            new_order = None

        self._current_sort_col = col if new_order is not None else None
        self._current_sort_order = new_order

        # Feedback visual instantâneo: Cursor de espera + mensagem amigável no rodapé
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

        self.tree.viewport().setUpdatesEnabled(False)
        self.tree.blockSignals(True)
        try:
            # Estado 3: Normal / Sem ordenação (restaura a ordem original do scan)
            if new_order is None:
                header.setSortIndicatorShown(False)
                self._reset_header_labels()

                SortableGroupItem._sort_normal = True
                SortableFileItem._sort_normal = True
                self.tree.sortItems(0, Qt.SortOrder.AscendingOrder)
                SortableGroupItem._sort_normal = False
                SortableFileItem._sort_normal = False
            else:
                # Estados 1 e 2: Crescente ou Decrescente (o indicador nativo da grid exibe o sinal de ordenação)
                header.setSortIndicatorShown(True)
                header.setSortIndicator(col, new_order)
                self._reset_header_labels()

                # Se a coluna for Seleção (0), faz uma contagem rápida O(N) dos itens marcados por grupo
                if col == 0:
                    for i in range(total_items):
                        g_it = root.child(i)
                        g_it.k_chk = sum(
                            1 for j in range(g_it.childCount())
                            if g_it.child(j).checkState(0) == Qt.CheckState.Checked
                        )

                SortableGroupItem._sort_col = col
                SortableFileItem._sort_col = col
                SortableGroupItem._sort_normal = False
                SortableFileItem._sort_normal = False
                self.tree.sortItems(col, new_order)

            # Sincroniza a lista interna self.groups com a nova ordem da árvore
            self.groups = [root.child(i).group for i in range(total_items)]
        finally:
            self.tree.blockSignals(False)
            self.tree.viewport().setUpdatesEnabled(True)
            self.tree.viewport().update()
            if header:
                header.viewport().update()
            self.status_message.emit("")
            if app:
                app.restoreOverrideCursor()

    # Alias para compatibilidade e chamadas diretas
    _on_header_clicked = _on_header_double_clicked

    def clear(self):
        """Limpa os resultados da árvore e reseta a seleção mantendo os títulos e colunas permanentemente visíveis."""
        self.groups = []
        self._selected_count = 0
        self._selected_bytes = 0
        self._current_sort_col = None
        self._current_sort_order = None
        self._reset_header_labels()
        self.tree.viewport().setUpdatesEnabled(False)
        self.tree.blockSignals(True)
        try:
            if self.tree.topLevelItemCount() > 0:
                self.tree.invisibleRootItem().takeChildren()
        finally:
            self.tree.blockSignals(False)
            self.tree.viewport().setUpdatesEnabled(True)
        self.tree.viewport().update()
        self.file_selected.emit(None)
        self.selection_changed.emit(0, 0)

    def has_selected_files(self) -> bool:
        """Retorna True se há pelo menos um arquivo marcado, em tempo O(1) sem varrer a árvore."""
        return self._selected_count > 0

    def get_selected_count(self) -> int:
        return self._selected_count

    def get_selected_bytes(self) -> int:
        return self._selected_bytes

    def _emit_selection_stats(self):
        """Calcula em tempo real quantos arquivos estão marcados e emite sinal para a janela principal."""
        count = 0
        total_bytes = 0
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            parent = root.child(i)
            for j in range(parent.childCount()):
                child = parent.child(j)
                file_rec: Optional[FileRecord] = child.data(0, Qt.ItemDataRole.UserRole)
                if file_rec and not file_rec.is_reference and child.checkState(0) == Qt.CheckState.Checked:
                    count += 1
                    total_bytes += file_rec.size
        self._selected_count = count
        self._selected_bytes = total_bytes
        self.selection_changed.emit(count, total_bytes)

    def expand_all(self):
        """Expande todos os grupos de duplicatas da árvore."""
        self.tree.expandAll()

    def collapse_all(self):
        """Recolhe todos os grupos de duplicatas da árvore."""
        self.tree.collapseAll()

    def select_smart_duplicates(self):
        """
        Aplica a seleção inteligente em lote sem congelar a interface:
        - Protege 100% dos arquivos de referência.
        - Se houver arquivos de referência no grupo, marca os demais.
        - Se não houver referência, mantém o maior/mais recente e marca os outros.
        - Atualiza a barra de progresso em lotes e emite processEvents para garantir 60fps.
        """
        root = self.tree.invisibleRootItem()
        total_groups = root.childCount()
        if total_groups == 0:
            return

        self.tree.blockSignals(True)
        self.tree.viewport().setUpdatesEnabled(False)

        selected_count = 0
        selected_bytes = 0

        try:
            # 1. Pré-calcula os caminhos a remover diretamente dos metadados em memória (< 5ms)
            paths_to_remove = set()
            for i in range(total_groups):
                parent_item = root.child(i)
                if isinstance(parent_item, SortableGroupItem) and getattr(parent_item, "group", None):
                    paths_to_remove.update(apply_reference_rules(parent_item.group))
                else:
                    group_files = []
                    for j in range(parent_item.childCount()):
                        rec = parent_item.child(j).data(0, Qt.ItemDataRole.UserRole)
                        if rec:
                            group_files.append(rec)
                    if group_files:
                        paths_to_remove.update(apply_reference_rules(DuplicateGroup(group_id=0, files=group_files)))

            # 2. Atualiza os nós da árvore em lotes com telemetria fluida
            CHUNK_SIZE = 150
            for i in range(total_groups):
                parent_item = root.child(i)
                for j in range(parent_item.childCount()):
                    child = parent_item.child(j)
                    file_rec: Optional[FileRecord] = child.data(0, Qt.ItemDataRole.UserRole)
                    if file_rec and not file_rec.is_reference:
                        if file_rec.path in paths_to_remove:
                            child.setCheckState(0, Qt.CheckState.Checked)
                            selected_count += 1
                            selected_bytes += file_rec.size
                        else:
                            child.setCheckState(0, Qt.CheckState.Unchecked)

                if (i + 1) % CHUNK_SIZE == 0 or (i + 1) == total_groups:
                    self.selection_progress.emit("Selecting smart duplicates", i + 1, total_groups)
                    QApplication.processEvents()

        finally:
            self.tree.viewport().setUpdatesEnabled(True)
            self.tree.blockSignals(False)
            self.selection_progress.emit("", 0, 0)
            self._selected_count = selected_count
            self._selected_bytes = selected_bytes
            self.selection_changed.emit(selected_count, selected_bytes)

    def select_custom(self, rule: str):
        """
        Aplica regras de seleção avançadas por grupo (estilo Krokiet).
        Arquivos de referência são 100% protegidos e nunca marcados.
        """
        root = self.tree.invisibleRootItem()
        total_groups = root.childCount()
        if total_groups == 0:
            return

        self.tree.blockSignals(True)
        self.tree.viewport().setUpdatesEnabled(False)

        selected_count = 0
        selected_bytes = 0
        CHUNK_SIZE = 150

        try:
            for i in range(total_groups):
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
                    for child, rec in non_ref_children:
                        curr = child.checkState(0)
                        new_st = Qt.CheckState.Unchecked if curr == Qt.CheckState.Checked else Qt.CheckState.Checked
                        child.setCheckState(0, new_st)
                        if new_st == Qt.CheckState.Checked:
                            selected_count += 1
                            selected_bytes += rec.size
                    continue

                has_reference = any(rec.is_reference for _, rec in child_items)
                if has_reference:
                    for child, rec in non_ref_children:
                        child.setCheckState(0, Qt.CheckState.Checked)
                        selected_count += 1
                        selected_bytes += rec.size
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

                for child, rec in non_ref_children:
                    if child is preserved_child:
                        child.setCheckState(0, Qt.CheckState.Unchecked)
                    else:
                        child.setCheckState(0, Qt.CheckState.Checked)
                        selected_count += 1
                        selected_bytes += rec.size

                if (i + 1) % CHUNK_SIZE == 0 or (i + 1) == total_groups:
                    self.selection_progress.emit("Applying selection rule", i + 1, total_groups)
                    QApplication.processEvents()

        finally:
            self.tree.viewport().setUpdatesEnabled(True)
            self.tree.blockSignals(False)
            self.selection_progress.emit("", 0, 0)
            self._selected_count = selected_count
            self._selected_bytes = selected_bytes
            self.selection_changed.emit(selected_count, selected_bytes)

    def invert_all(self):
        """Inverte a seleção de todos os arquivos não-referência da árvore inteira."""
        root = self.tree.invisibleRootItem()
        total_groups = root.childCount()
        if total_groups == 0:
            return

        self.tree.blockSignals(True)
        self.tree.viewport().setUpdatesEnabled(False)

        selected_count = 0
        selected_bytes = 0
        CHUNK_SIZE = 150

        try:
            for i in range(total_groups):
                parent = root.child(i)
                for j in range(parent.childCount()):
                    child = parent.child(j)
                    file_rec: FileRecord = child.data(0, Qt.ItemDataRole.UserRole)
                    if file_rec and not file_rec.is_reference:
                        curr = child.checkState(0)
                        new_st = Qt.CheckState.Unchecked if curr == Qt.CheckState.Checked else Qt.CheckState.Checked
                        child.setCheckState(0, new_st)
                        if new_st == Qt.CheckState.Checked:
                            selected_count += 1
                            selected_bytes += file_rec.size

                if (i + 1) % CHUNK_SIZE == 0 or (i + 1) == total_groups:
                    self.selection_progress.emit("Inverting selection", i + 1, total_groups)
                    QApplication.processEvents()
        finally:
            self.tree.viewport().setUpdatesEnabled(True)
            self.tree.blockSignals(False)
            self.selection_progress.emit("", 0, 0)
            self._selected_count = selected_count
            self._selected_bytes = selected_bytes
            self.selection_changed.emit(selected_count, selected_bytes)

    def select_all(self):
        """Marca todos os arquivos (exceto os de referência)."""
        root = self.tree.invisibleRootItem()
        total_groups = root.childCount()
        if total_groups == 0:
            return

        self.tree.blockSignals(True)
        self.tree.viewport().setUpdatesEnabled(False)

        selected_count = 0
        selected_bytes = 0
        CHUNK_SIZE = 150

        try:
            for i in range(total_groups):
                parent = root.child(i)
                for j in range(parent.childCount()):
                    child = parent.child(j)
                    file_rec: FileRecord = child.data(0, Qt.ItemDataRole.UserRole)
                    if file_rec and not file_rec.is_reference:
                        child.setCheckState(0, Qt.CheckState.Checked)
                        selected_count += 1
                        selected_bytes += file_rec.size

                if (i + 1) % CHUNK_SIZE == 0 or (i + 1) == total_groups:
                    self.selection_progress.emit("Selecting all duplicates", i + 1, total_groups)
                    QApplication.processEvents()
        finally:
            self.tree.viewport().setUpdatesEnabled(True)
            self.tree.blockSignals(False)
            self.selection_progress.emit("", 0, 0)
            self._selected_count = selected_count
            self._selected_bytes = selected_bytes
            self.selection_changed.emit(selected_count, selected_bytes)

    def unselect_all(self):
        """Desmarca todos os arquivos."""
        root = self.tree.invisibleRootItem()
        total_groups = root.childCount()
        if total_groups == 0:
            return

        self.tree.blockSignals(True)
        self.tree.viewport().setUpdatesEnabled(False)
        CHUNK_SIZE = 150

        try:
            for i in range(total_groups):
                parent = root.child(i)
                for j in range(parent.childCount()):
                    child = parent.child(j)
                    file_rec: FileRecord = child.data(0, Qt.ItemDataRole.UserRole)
                    if file_rec and not file_rec.is_reference:
                        child.setCheckState(0, Qt.CheckState.Unchecked)

                if (i + 1) % CHUNK_SIZE == 0 or (i + 1) == total_groups:
                    self.selection_progress.emit("Unselecting all", i + 1, total_groups)
                    QApplication.processEvents()
        finally:
            self.tree.viewport().setUpdatesEnabled(True)
            self.tree.blockSignals(False)
            self.selection_progress.emit("", 0, 0)
            self._selected_count = 0
            self._selected_bytes = 0
            self.selection_changed.emit(0, 0)

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

    def get_selected_records(self) -> List[FileRecord]:
        """Retorna os registros de todos os arquivos selecionados para exclusão."""
        selected: List[FileRecord] = []
        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            parent = root.child(i)
            for j in range(parent.childCount()):
                child = parent.child(j)
                file_rec: Optional[FileRecord] = child.data(0, Qt.ItemDataRole.UserRole)
                if file_rec and not file_rec.is_reference and child.checkState(0) == Qt.CheckState.Checked:
                    selected.append(file_rec)
        return selected

    def _on_item_changed(self, item: QTreeWidgetItem, column: int):
        """Garante que nenhum item de referência possa ter checkState marcado e atualiza telemetria de seleção em O(1)."""
        if column == 0 and item.childCount() == 0:
            file_rec: Optional[FileRecord] = item.data(0, Qt.ItemDataRole.UserRole)
            if file_rec:
                if file_rec.is_reference:
                    if item.checkState(0) != Qt.CheckState.Unchecked:
                        item.setCheckState(0, Qt.CheckState.Unchecked)
                    return

                is_checked = (item.checkState(0) == Qt.CheckState.Checked)
                prev_state = getattr(item, "_last_checked", False)
                if is_checked != prev_state:
                    item._last_checked = is_checked
                    if is_checked:
                        self._selected_count += 1
                        self._selected_bytes += file_rec.size
                    else:
                        self._selected_count = max(0, self._selected_count - 1)
                        self._selected_bytes = max(0, self._selected_bytes - file_rec.size)
                    self.selection_changed.emit(self._selected_count, self._selected_bytes)

    def remove_deleted_files(self, deleted_paths: Set[Path]):
        """Remove da árvore os arquivos que foram excluídos com sucesso sem travar a interface."""
        if not deleted_paths:
            return

        self.tree.blockSignals(True)
        self.tree.viewport().setUpdatesEnabled(False)

        try:
            cur_item = self.tree.currentItem()
            if cur_item and cur_item.childCount() == 0:
                cur_rec: Optional[FileRecord] = cur_item.data(0, Qt.ItemDataRole.UserRole)
                if cur_rec and cur_rec.path in deleted_paths:
                    self.file_selected.emit(None)

            root = self.tree.invisibleRootItem()
            for i in range(root.childCount() - 1, -1, -1):
                parent = root.child(i)
                for j in range(parent.childCount() - 1, -1, -1):
                    child = parent.child(j)
                    file_rec: Optional[FileRecord] = child.data(0, Qt.ItemDataRole.UserRole)
                    if file_rec and file_rec.path in deleted_paths:
                        parent.removeChild(child)

                # Se o grupo ficou com 1 ou 0 arquivos, remove o grupo inteiro
                if parent.childCount() <= 1:
                    root.removeChild(parent)
        finally:
            # Mantém a lista interna self.groups sincronizada com as remoções
            updated_groups = []
            for g in self.groups:
                rem = [f for f in g.files if f.path not in deleted_paths]
                if len(rem) > 1:
                    g.files = rem
                    updated_groups.append(g)
            self.groups = updated_groups

            self.tree.viewport().setUpdatesEnabled(True)
            self.tree.blockSignals(False)
            self._emit_selection_stats()

    def _on_item_clicked(self, item: QTreeWidgetItem, column: int):
        """Ao clicar com 1 clique em um arquivo, emite sinal para o preview."""
        if item and item.childCount() == 0:
            file_rec: Optional[FileRecord] = item.data(0, Qt.ItemDataRole.UserRole)
            self.file_selected.emit(file_rec)
        else:
            self.file_selected.emit(None)

    def _on_current_item_changed(self, current: Optional[QTreeWidgetItem], previous: Optional[QTreeWidgetItem]):
        """Ao navegar com o teclado ou mudar a seleção na árvore, atualiza o preview."""
        if current and current.childCount() == 0:
            file_rec: Optional[FileRecord] = current.data(0, Qt.ItemDataRole.UserRole)
            self.file_selected.emit(file_rec)
        else:
            self.file_selected.emit(None)

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
        menu = QMenu(self)

        # Se clicou em um arquivo específico, oferece opções de arquivo
        if item and item.childCount() == 0:
            file_rec: Optional[FileRecord] = item.data(0, Qt.ItemDataRole.UserRole)
            if file_rec:
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
        """Abre o Windows Explorer com o arquivo selecionado."""
        if path.exists():
            subprocess.run(["explorer", f"/select,{path}"])
