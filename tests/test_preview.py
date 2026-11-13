"""
Testes automatizados do Painel de Preview de Imagens e Vídeos.
Valida renderização assíncrona, cache LRU, sinais de seleção da árvore e dupla-clique.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from dupeclean.core.models import FileRecord, DuplicateGroup, ScanMode
from dupeclean.ui.preview_panel import PreviewPanel, AspectRatioPixmapLabel
from dupeclean.ui.results_table import ResultsWidget
from dupeclean.ui.main_window import MainWindow

app = QApplication.instance()
if not app:
    app = QApplication(sys.argv)


class TestPreviewIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(__file__).resolve().parent.parent / "data" / "test_preview_tmp"
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

        # Cria imagens sintéticas para teste
        self.img1_path = self.tmp_dir / "test_sample1.jpg"
        img1 = Image.new("RGB", (640, 480), color="blue")
        img1.save(self.img1_path)

        self.img2_path = self.tmp_dir / "test_sample2.png"
        img2 = Image.new("RGB", (1920, 1080), color="green")
        img2.save(self.img2_path)

    def tearDown(self):
        try:
            if self.img1_path.exists():
                self.img1_path.unlink()
            if self.img2_path.exists():
                self.img2_path.unlink()
            if self.tmp_dir.exists():
                self.tmp_dir.rmdir()
        except Exception:
            pass

    def test_preview_panel_loading_and_lru_cache(self):
        panel = PreviewPanel()
        rec1 = FileRecord(path=self.img1_path, size=self.img1_path.stat().st_size, mtime=100.0)

        # 1. Carregamento inicial (via worker)
        panel.load_file(rec1)
        # Espera o worker processar
        panel._worker.wait(1000)
        app.processEvents()

        # Verifica se carregou no cache LRU
        self.assertIn(str(self.img1_path), panel._lru_cache)
        pixmap, meta = panel._lru_cache[str(self.img1_path)]
        self.assertIsNotNone(pixmap)
        self.assertEqual(meta["width"], 640)
        self.assertEqual(meta["height"], 480)

        # 2. Carregamento do cache LRU (0 ms)
        rec2 = FileRecord(path=self.img2_path, size=self.img2_path.stat().st_size, mtime=100.0)
        panel.load_file(rec2)
        panel._worker.wait(1000)
        app.processEvents()

        self.assertIn(str(self.img2_path), panel._lru_cache)

        # Alterna de volta para rec1 (deve vir instantâneo do cache)
        panel.load_file(rec1)
        self.assertEqual(panel._current_file.path, self.img1_path)

        # Valida estabilidade geométrica no swap entre fotos (zero encolhimento vertical por lbl_status)
        panel.resize(360, 520)
        panel.show()
        app.processEvents()
        size_before = panel.lbl_image.size()

        rec2_uncached = FileRecord(path=self.img2_path, size=self.img2_path.stat().st_size, mtime=100.0)
        panel._lru_cache.pop(str(self.img2_path), None)
        panel.load_file(rec2_uncached)
        app.processEvents()
        self.assertEqual(panel.lbl_image.size(), size_before)
        self.assertIs(panel.image_stack.currentWidget(), panel.lbl_image)

        panel._worker.wait(1000)
        app.processEvents()
        self.assertEqual(panel.lbl_image.size(), size_before)

        # Limpeza
        panel.clear()
        self.assertIsNone(panel._current_file)
        panel.close()
        panel._worker.stop()

    def test_results_widget_selection_signal(self):
        widget = ResultsWidget()
        rec = FileRecord(path=self.img1_path, size=self.img1_path.stat().st_size, mtime=100.0)
        group = DuplicateGroup(group_id=1, files=[rec], similarity=1.0)
        widget.set_results([group])

        received_recs = []
        widget.file_selected.connect(lambda r: received_recs.append(r))

        # Seleciona o primeiro filho (linha 1: arquivo abaixo do cabeçalho na linha 0)
        child_index = widget.model.index(1, 1)
        widget.tree.setCurrentIndex(child_index)
        app.processEvents()

        self.assertTrue(len(received_recs) > 0)
        self.assertEqual(received_recs[-1].path, self.img1_path)

        # Seleciona o cabeçalho do grupo (linha 0: deve emitir None)
        group_index = widget.model.index(0, 1)
        widget.tree.setCurrentIndex(group_index)
        app.processEvents()
        self.assertIsNone(received_recs[-1])

    def test_main_window_preview_visibility(self):
        gpu_info = {"model": "NVIDIA GeForce RTX 5090", "vram_mb": 32768, "available": True}
        win = MainWindow(gpu_info=gpu_info)
        win.show()
        app.processEvents()

        # Inicia invisível
        self.assertTrue(win.preview_panel.isHidden())

        # Simula seleção de foto
        rec = FileRecord(path=self.img1_path, size=self.img1_path.stat().st_size, mtime=100.0)
        win._on_file_selected(rec)
        app.processEvents()
        self.assertFalse(win.preview_panel.isHidden())

        # Simula deseleção
        win._on_file_selected(None)
        app.processEvents()
        self.assertTrue(win.preview_panel.isHidden())

        win.close()

    def test_preview_image_zoom_pan_and_reset(self):
        from PySide6.QtCore import QPoint, QPointF
        from PySide6.QtGui import QPixmap, QWheelEvent
        from PySide6.QtTest import QTest

        panel = PreviewPanel()
        panel.resize(320, 320)
        panel.show()
        app.processEvents()

        pm = QPixmap(800, 600)
        pm.fill(Qt.GlobalColor.blue)
        panel.image_stack.setCurrentWidget(panel.lbl_image)
        panel.lbl_image.set_pixmap(pm)

        self.assertEqual(panel.lbl_image._zoom_factor, 1.0)
        we_in = QWheelEvent(
            QPointF(100, 100),
            panel.lbl_image.mapToGlobal(QPoint(100, 100)).toPointF(),
            QPoint(0, 0),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.ControlModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        app.sendEvent(panel.lbl_image, we_in)
        self.assertGreater(panel.lbl_image._zoom_factor, 1.0)

        panel.lbl_image.setFocus()
        app.processEvents()
        QTest.keyClick(panel.lbl_image, Qt.Key.Key_0, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(panel.lbl_image._zoom_factor, 1.0)

        panel.close()
        panel._worker.stop()

    def test_results_grid_zoom_scales_font_and_row_height(self):
        from PySide6.QtCore import QPoint, QPointF
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtTest import QTest

        win = MainWindow(gpu_info={"name": "RTX 5090", "has_cuda": True})
        self.addCleanup(win.close)
        win.show()
        app.processEvents()

        tree = win.results_widget.tree
        base_row_h = tree.verticalHeader().defaultSectionSize()
        base_font_px = tree._group_font.pixelSize()

        # Ctrl+Wheel on results grid viewport
        we_in = QWheelEvent(
            QPointF(50, 50),
            tree.viewport().mapToGlobal(QPoint(50, 50)).toPointF(),
            QPoint(0, 0),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.ControlModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        app.sendEvent(tree.viewport(), we_in)
        self.assertEqual(tree._zoom_level, 1)
        self.assertGreater(tree.verticalHeader().defaultSectionSize(), base_row_h)

        # Ctrl+= while focus is on MainWindow/tree and Ctrl+0 reset
        tree.setFocus()
        app.processEvents()
        QTest.keyClick(tree, Qt.Key.Key_Equal, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(tree._zoom_level, 2)

        QTest.keyClick(tree, Qt.Key.Key_0, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(tree._zoom_level, 0)
        self.assertEqual(tree.verticalHeader().defaultSectionSize(), base_row_h)
        if base_font_px > 0:
            self.assertEqual(tree._group_font.pixelSize(), base_font_px)


if __name__ == "__main__":
    unittest.main()
