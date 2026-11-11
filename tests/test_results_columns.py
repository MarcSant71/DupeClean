"""
Testes de verificação da nova estrutura da grade de resultados (6 colunas):
- Remoção da coluna Origin
- Ausência de ícone de cadeado na coluna Selection
- Linha de referência inteiramente pintada em cinza desabilitado (#202024 / #71717a)
- Coluna 5 é Full Path e ordena corretamente
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from dupeclean.core.models import FileRecord, DuplicateGroup, ScanMode
from dupeclean.ui.results_table import ResultsWidget

app = QApplication.instance() or QApplication(sys.argv)

def test_results_columns():
    widget = ResultsWidget()

    # 1. Verifica contagem e rótulos das colunas
    assert widget.tree.columnCount() == 6, f"Esperado 6 colunas, obtido {widget.tree.columnCount()}"
    labels = [widget.tree.headerItem().text(i) for i in range(6)]
    expected_labels = ["Selection", "File Name", "Size", "Date Modified", "Similarity", "Full Path"]
    assert labels == expected_labels, f"Rótulos incorretos: {labels} != {expected_labels}"

    # 2. Cria grupo com 1 arquivo de referência e 1 duplicata normal
    ref_file = FileRecord(
        path=Path("W:/Temp/SET1/photo.jpg"),
        size=1024,
        mtime=1700000000.0,
        is_reference=True,
        name="photo.jpg",
        ext=".jpg"
    )
    dup_file = FileRecord(
        path=Path("W:/Temp/SET2/photo.jpg"),
        size=1024,
        mtime=1700000000.0,
        is_reference=False,
        name="photo.jpg",
        ext=".jpg"
    )
    group = DuplicateGroup(group_id=1, files=[ref_file, dup_file], similarity=1.0, kind=ScanMode.EXACT)
    widget.set_results([group])

    # 3. Inspeciona os itens da árvore
    parent_item = widget.tree.topLevelItem(0)
    assert parent_item.childCount() == 2

    child_ref = parent_item.child(0)
    child_dup = parent_item.child(1)

    # 4. Linha de referência
    # Sem cadeado e sem checkbox
    assert child_ref.text(0) == "", f"Texto da coluna 0 da referência deve ser vazio, obtido '{child_ref.text(0)}'"
    assert not (child_ref.flags() & Qt.ItemFlag.ItemIsUserCheckable), "Item de referência não pode ser checkable"
    assert child_ref.text(5) == str(ref_file.path), f"Coluna 5 deve ser Full Path: {child_ref.text(5)}"

    # Linha inteira pintada de cinza (#202024 / #71717a)
    expected_bg = QColor("#202024")
    expected_fg = QColor("#71717a")
    for col in range(6):
        bg = child_ref.background(col).color()
        fg = child_ref.foreground(col).color()
        assert bg == expected_bg, f"Col {col} bg esperado {expected_bg.name()}, obtido {bg.name()}"
        assert fg == expected_fg, f"Col {col} fg esperado {expected_fg.name()}, obtido {fg.name()}"
        assert "Protected file" in child_ref.toolTip(col)

    # 5. Linha de duplicata normal
    assert (child_dup.flags() & Qt.ItemFlag.ItemIsUserCheckable), "Item duplicata deve ser checkable"
    assert child_dup.checkState(0) == Qt.CheckState.Unchecked
    assert child_dup.text(5) == str(dup_file.path)

    # 6. Ordenação por coluna 5 (Full Path)
    widget._on_header_clicked(5)
    app.processEvents()
    assert widget.tree.columnCount() == 6

    # 7. Seleção inteligente
    widget.select_smart_duplicates()
    app.processEvents()
    new_parent = widget.tree.topLevelItem(0)
    for j in range(new_parent.childCount()):
        child = new_parent.child(j)
        rec = child.data(0, Qt.ItemDataRole.UserRole)
        if rec.is_reference:
            assert child.checkState(0) == Qt.CheckState.Unchecked, "Referência nunca pode ser selecionada"
        else:
            assert child.checkState(0) == Qt.CheckState.Checked, "Duplicata deve ser selecionada pelo smart select"

    print("[OK] test_results_columns passou com sucesso!")

if __name__ == "__main__":
    test_results_columns()
