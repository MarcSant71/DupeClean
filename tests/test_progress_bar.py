import sys
import unittest
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage
from dupeclean.ui.progress_bar import SmoothProgressBar


class TestSmoothProgressBar(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def test_initial_state(self):
        bar = SmoothProgressBar()
        self.assertEqual(bar.height(), 8)
        self.assertFalse(bar.isTextVisible())
        self.assertFalse(bar._timer.isActive())

    def test_indeterminate_mode_timer(self):
        bar = SmoothProgressBar()
        bar.show()
        bar.setRange(0, 0)
        self.assertTrue(bar._timer.isActive())

        # Animação deve avançar da esquerda para a direita
        initial_pos = bar._anim_pos
        bar._on_animation_tick()
        self.assertGreater(bar._anim_pos, initial_pos)

        # Switch to determinate
        bar.setRange(0, 100)
        self.assertFalse(bar._timer.isActive())

    def test_rendering_modes(self):
        bar = SmoothProgressBar()
        bar.resize(300, 8)

        # 1. Indeterminate render
        bar.setRange(0, 0)
        bar._anim_pos = 0.5
        img = QImage(300, 8, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(0)
        bar.render(img)
        self.assertEqual(img.width(), 300)
        self.assertEqual(img.height(), 8)

        # 2. Determinate render (50%)
        bar.setRange(0, 100)
        bar.setValue(50)
        img.fill(0)
        bar.render(img)
        self.assertEqual(img.width(), 300)

        # 3. Determinate render (0%)
        bar.setValue(0)
        img.fill(0)
        bar.render(img)
        self.assertEqual(img.width(), 300)


if __name__ == "__main__":
    unittest.main()
