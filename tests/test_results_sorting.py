"""
Testes automatizados da ordenação de colunas em alta performance:
- Ordenação por Tamanho (1º clique Decrescente, 2º Crescente)
- Ordenação por Data (1º clique Decrescente)
- Ordenação por Nome e Caminho (1º clique Crescente A-Z)
- Ordenação por Seleção (1º clique Decrescente, grupos com marcações no topo)
- Preservação estrita dos checkboxes marcados durante qualquer ordenação
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

        # Cria 3 grupos com diferentes tamanhos, datas e nomes
        self.g_small = DuplicateGroup(
            group_id=1,
            files=[
                FileRecord(path=Path("C:/demo/small_a.txt"), size=100, mtime=1000.0, name="small_a.txt", hash_value="h1"),
                FileRecord(path=Path("C:/demo/small_b.txt"), size=100, mtime=1001.0, name="small_b.txt", hash_value="h1"),
            ],
            similarity=1.0,
            kind=ScanMode.EXACT
        )
        self.g_huge = DuplicateGroup(
            group_id=2,
            files=[
                FileRecord(path=Path("C:/demo/huge_a.iso"), size=5000000, mtime=500.0, name="huge_a.iso", hash_value="h2"),
                FileRecord(path=Path("C:/demo/huge_b.iso"), size=5000000, mtime=501.0, name="huge_b.iso", hash_value="h2"),
            ],
            similarity=1.0,
            kind=ScanMode.EXACT
        )
        self.g_medium = DuplicateGroup(
            group_id=3,
            files=[
                FileRecord(path=Path("C:/demo/medium_a.mp4"), size=250000, mtime=2000.0, name="medium_a.mp4", hash_value="h3"),
                FileRecord(path=Path("C:/demo/medium_b.mp4"), size=250000, mtime=2001.0, name="medium_b.mp4", hash_value="h3"),
            ],
            similarity=0.95,
            kind=ScanMode.EXACT
        )

        self.widget.set_results([self.g_small, self.g_huge, self.g_medium])

    def test_size_sort_three_state_cycle(self):
        # 1º duplo clique na coluna 2 (Size): Crescente (menor arquivo no topo)
        self.widget._on_header_double_clicked(2)
        self.assertEqual(self.widget._current_sort_order, Qt.SortOrder.AscendingOrder)
        self.assertEqual(self.widget.tree.topLevelItem(0).group.group_id, 1)  # small (100 B)
        self.assertEqual(self.widget.tree.headerItem().text(2), "Size")
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())
        self.assertEqual(self.widget.tree.header().sortIndicatorOrder(), Qt.SortOrder.AscendingOrder)

        # 2º duplo clique na coluna 2 (Size): Decrescente (maior arquivo no topo)
        self.widget._on_header_double_clicked(2)
        self.assertEqual(self.widget._current_sort_order, Qt.SortOrder.DescendingOrder)
        self.assertEqual(self.widget.tree.topLevelItem(0).group.group_id, 2)  # huge (5 MB)
        self.assertEqual(self.widget.tree.headerItem().text(2), "Size")
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())
        self.assertEqual(self.widget.tree.header().sortIndicatorOrder(), Qt.SortOrder.DescendingOrder)

        # 3º duplo clique na coluna 2 (Size): Normal (sem ordenação, restaura ordem original do scan)
        self.widget._on_header_double_clicked(2)
        self.assertIsNone(self.widget._current_sort_order)
        self.assertEqual(self.widget.tree.topLevelItem(0).group.group_id, 1)  # g_small
        self.assertEqual(self.widget.tree.topLevelItem(1).group.group_id, 2)  # g_huge
        self.assertEqual(self.widget.tree.topLevelItem(2).group.group_id, 3)  # g_medium
        self.assertEqual(self.widget.tree.headerItem().text(2), "Size")
        self.assertFalse(self.widget.tree.header().isSortIndicatorShown())

    def test_checkmark_preservation_during_sort(self):
        # Marca um arquivo no grupo 1 (small)
        g1_item = None
        for i in range(self.widget.tree.topLevelItemCount()):
            if self.widget.tree.topLevelItem(i).group.group_id == 1:
                g1_item = self.widget.tree.topLevelItem(i)
                break
        self.assertIsNotNone(g1_item)
        g1_item.child(0).setCheckState(0, Qt.CheckState.Checked)

        # Alterna pelo ciclo completo de 3 estados em colunas diferentes
        self.widget._on_header_double_clicked(2)  # Size asc
        self.widget._on_header_double_clicked(2)  # Size desc
        self.widget._on_header_double_clicked(2)  # Size normal
        self.widget._on_header_double_clicked(1)  # Name asc
        self.widget._on_header_double_clicked(1)  # Name desc
        self.widget._on_header_double_clicked(1)  # Name normal

        # Localiza o grupo 1 novamente e verifica se o checkbox continua marcado no arquivo correto (small_a.txt)
        found_g1 = False
        for i in range(self.widget.tree.topLevelItemCount()):
            item = self.widget.tree.topLevelItem(i)
            if item.group.group_id == 1:
                found_g1 = True
                states = {item.child(j).file_rec.name: item.child(j).checkState(0) for j in range(item.childCount())}
                self.assertEqual(states.get("small_a.txt"), Qt.CheckState.Checked, "A seleção de small_a.txt foi perdida!")
                self.assertEqual(states.get("small_b.txt"), Qt.CheckState.Unchecked, "small_b.txt não deveria estar marcado!")
        self.assertTrue(found_g1)

    def test_file_name_sort_three_state_cycle(self):
        # 1º duplo clique na coluna 1 (File Name): Crescente A-Z
        self.widget._on_header_double_clicked(1)
        self.assertEqual(self.widget.tree.topLevelItem(0).group.files[0].name, "huge_a.iso")  # h < m < s
        self.assertEqual(self.widget.tree.headerItem().text(1), "File Name")
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())

        # 2º duplo clique na coluna 1: Decrescente Z-A
        self.widget._on_header_double_clicked(1)
        self.assertEqual(self.widget.tree.topLevelItem(0).group.files[0].name, "small_a.txt")
        self.assertEqual(self.widget.tree.headerItem().text(1), "File Name")
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())

        # 3º duplo clique na coluna 1: Normal (volta à ordem original do scan)
        self.widget._on_header_double_clicked(1)
        self.assertIsNone(self.widget._current_sort_order)
        self.assertEqual(self.widget.tree.topLevelItem(0).group.files[0].name, "small_a.txt")  # g1
        self.assertEqual(self.widget.tree.topLevelItem(1).group.files[0].name, "huge_a.iso")   # g2
        self.assertEqual(self.widget.tree.headerItem().text(1), "File Name")
        self.assertFalse(self.widget.tree.header().isSortIndicatorShown())

    def test_date_sort_three_state_cycle(self):
        # 1º duplo clique na coluna 3 (Date): Crescente (mais antigo no topo)
        self.widget._on_header_double_clicked(3)
        self.assertEqual(self.widget.tree.topLevelItem(0).group.group_id, 2)  # mtime 500.0
        self.assertEqual(self.widget.tree.headerItem().text(3), "Date Modified")
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())

        # 2º duplo clique na coluna 3: Decrescente (mais recente no topo)
        self.widget._on_header_double_clicked(3)
        self.assertEqual(self.widget.tree.topLevelItem(0).group.group_id, 3)  # mtime 2000.0
        self.assertEqual(self.widget.tree.headerItem().text(3), "Date Modified")
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())

        # 3º duplo clique na coluna 3: Normal
        self.widget._on_header_double_clicked(3)
        self.assertIsNone(self.widget._current_sort_order)
        self.assertEqual(self.widget.tree.topLevelItem(0).group.group_id, 1)  # ordem original g1
        self.assertEqual(self.widget.tree.headerItem().text(3), "Date Modified")
        self.assertFalse(self.widget.tree.header().isSortIndicatorShown())

    def test_selection_column_sort_three_state_cycle(self):
        # Marca um arquivo no grupo 3
        for i in range(self.widget.tree.topLevelItemCount()):
            if self.widget.tree.topLevelItem(i).group.group_id == 3:
                self.widget.tree.topLevelItem(i).child(0).setCheckState(0, Qt.CheckState.Checked)
                break

        # 1º duplo clique: Crescente (desmarcados primeiro)
        self.widget._on_header_double_clicked(0)
        self.assertNotEqual(self.widget.tree.topLevelItem(0).group.group_id, 3)
        self.assertEqual(self.widget.tree.headerItem().text(0), "Selection")
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())

        # 2º duplo clique: Decrescente (grupos com itens marcados no topo)
        self.widget._on_header_double_clicked(0)
        self.assertEqual(self.widget.tree.topLevelItem(0).group.group_id, 3)
        self.assertEqual(self.widget.tree.headerItem().text(0), "Selection")
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())

        # 3º duplo clique: Normal (ordem original do scan)
        self.widget._on_header_double_clicked(0)
        self.assertIsNone(self.widget._current_sort_order)
        self.assertEqual(self.widget.tree.topLevelItem(0).group.group_id, 1)
        self.assertEqual(self.widget.tree.headerItem().text(0), "Selection")
        self.assertFalse(self.widget.tree.header().isSortIndicatorShown())

    def test_similarity_and_path_sort_three_state_cycle(self):
        # Coluna 4: Similarity (g_medium tem 0.95, g_small e g_huge tem 1.0)
        # 1º duplo clique: Crescente (0.95 no topo)
        self.widget._on_header_double_clicked(4)
        self.assertEqual(self.widget.tree.topLevelItem(0).group.group_id, 3)
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())

        # 2º duplo clique: Decrescente (1.0 no topo)
        self.widget._on_header_double_clicked(4)
        self.assertNotEqual(self.widget.tree.topLevelItem(0).group.group_id, 3)
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())

        # 3º duplo clique: Normal (ordem original g1 no topo)
        self.widget._on_header_double_clicked(4)
        self.assertIsNone(self.widget._current_sort_order)
        self.assertEqual(self.widget.tree.topLevelItem(0).group.group_id, 1)
        self.assertFalse(self.widget.tree.header().isSortIndicatorShown())

        # Coluna 5: Full Path
        # 1º duplo clique: Crescente
        self.widget._on_header_double_clicked(5)
        self.assertEqual(self.widget.tree.topLevelItem(0).group.files[0].name, "huge_a.iso")
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())

        # 2º duplo clique: Decrescente
        self.widget._on_header_double_clicked(5)
        self.assertEqual(self.widget.tree.topLevelItem(0).group.files[0].name, "small_a.txt")
        self.assertTrue(self.widget.tree.header().isSortIndicatorShown())

        # 3º duplo clique: Normal
        self.widget._on_header_double_clicked(5)
        self.assertIsNone(self.widget._current_sort_order)
        self.assertEqual(self.widget.tree.topLevelItem(0).group.group_id, 1)
        self.assertFalse(self.widget.tree.header().isSortIndicatorShown())

    def test_double_click_signal_integration(self):
        header = self.widget.tree.header()

        # Clique simples não deve alterar a ordenação
        header.sectionClicked.emit(1)
        self.assertIsNone(self.widget._current_sort_order)
        self.assertEqual(self.widget.tree.headerItem().text(1), "File Name")

        # 1º duplo clique: Crescente
        header.sectionDoubleClicked.emit(1)
        self.assertEqual(self.widget._current_sort_order, Qt.SortOrder.AscendingOrder)
        self.assertEqual(self.widget.tree.headerItem().text(1), "File Name")
        self.assertTrue(header.isSortIndicatorShown())

        # 2º duplo clique: Decrescente
        header.sectionDoubleClicked.emit(1)
        self.assertEqual(self.widget._current_sort_order, Qt.SortOrder.DescendingOrder)
        self.assertEqual(self.widget.tree.headerItem().text(1), "File Name")
        self.assertTrue(header.isSortIndicatorShown())

        # 3º duplo clique: Normal (sem ordenação)
        header.sectionDoubleClicked.emit(1)
        self.assertIsNone(self.widget._current_sort_order)
        self.assertEqual(self.widget.tree.headerItem().text(1), "File Name")
        self.assertFalse(header.isSortIndicatorShown())
        self.assertEqual(self.widget.tree.headerItem().text(1), "File Name")

    def test_header_sections_always_clickable(self):
        # O cabeçalho deve permanecer estritamente clicável em todas as fases
        header = self.widget.tree.header()
        self.assertTrue(header.sectionsClickable(), "Cabeçalho deve ser clicável após set_results")

        self.widget.select_smart_duplicates()
        self.assertTrue(header.sectionsClickable(), "Cabeçalho deve ser clicável após select_smart_duplicates")

        self.widget.select_custom("except_newest")
        self.assertTrue(header.sectionsClickable(), "Cabeçalho deve ser clicável após select_custom")

        self.widget.select_all()
        self.assertTrue(header.sectionsClickable(), "Cabeçalho deve ser clicável após select_all")

        self.widget.unselect_all()
        self.assertTrue(header.sectionsClickable(), "Cabeçalho deve ser clicável após unselect_all")

        self.widget.invert_all()
        self.assertTrue(header.sectionsClickable(), "Cabeçalho deve ser clicável após invert_all")

        self.widget.remove_deleted_files({Path("C:/demo/small_b.txt")})
        self.assertTrue(header.sectionsClickable(), "Cabeçalho deve ser clicável após remove_deleted_files")

        self.widget.clear()
        self.assertTrue(self.widget.tree.header().sectionsClickable(), "Cabeçalho deve ser clicável após clear")


    def test_sorting_status_messages(self):
        emitted_messages = []
        self.widget.status_message.connect(lambda msg: emitted_messages.append(msg))

        # Duplo clique em Size (coluna 2): Crescente
        self.widget._on_header_double_clicked(2)
        # Deve ter emitido a mensagem de início e a de restauração ("")
        self.assertIn("Sorting results by Size (Ascending)...", emitted_messages)
        self.assertEqual(emitted_messages[-1], "")

        # Duplo clique em Size (coluna 2): Decrescente
        emitted_messages.clear()
        self.widget._on_header_double_clicked(2)
        self.assertIn("Sorting results by Size (Descending)...", emitted_messages)
        self.assertEqual(emitted_messages[-1], "")

        # 3º duplo clique em Size (coluna 2): Normal
        emitted_messages.clear()
        self.widget._on_header_double_clicked(2)
        self.assertIn("Restoring original scan order...", emitted_messages)
        self.assertEqual(emitted_messages[-1], "")

    def test_group_header_omits_similarity_while_column_preserves_it(self):
        # O cabeçalho do grupo não deve duplicar "Similarity:", mantendo a coluna 4 intacta
        group_item = self.widget.tree.topLevelItem(2)  # g_medium (similarity=0.95)
        header_text = group_item.text(1)
        self.assertIn("Group #3", header_text)
        self.assertIn("2 Files", header_text)
        self.assertIn("Reclaimable:", header_text)
        self.assertNotIn("Similarity:", header_text)
        self.assertEqual(group_item.child(0).text(4), "95.0%")


if __name__ == "__main__":
    unittest.main()
