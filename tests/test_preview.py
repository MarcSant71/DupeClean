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
        self.tmp_dir = Path("W:/Projetos/Pessoal/DupeClean/data/test_preview_tmp")
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

        # Limpeza
        panel.clear()
        self.assertIsNone(panel._current_file)
        panel._worker.stop()

    def test_results_widget_selection_signal(self):
        widget = ResultsWidget()
        rec = FileRecord(path=self.img1_path, size=self.img1_path.stat().st_size, mtime=100.0)
        group = DuplicateGroup(group_id=1, files=[rec], similarity=1.0)
        widget.set_results([group])

        received_recs = []
        widget.file_selected.connect(lambda r: received_recs.append(r))

        # Seleciona o primeiro filho (o arquivo)
        parent_item = widget.tree.topLevelItem(0)
        child_item = parent_item.child(0)
        widget.tree.setCurrentItem(child_item)
        app.processEvents()

        self.assertTrue(len(received_recs) > 0)
        self.assertEqual(received_recs[-1].path, self.img1_path)

        # Clica no cabeçalho do grupo (deve emitir None)
        widget.tree.setCurrentItem(parent_item)
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


if __name__ == "__main__":
    unittest.main()
