import ctypes
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication
from dupeclean.ui.splash import SplashScreen
from dupeclean.ui.theme import create_app_icon


class TestStartupIcon(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def test_create_app_icon_loads_ico_and_fallback(self):
        icon = create_app_icon()
        self.assertFalse(icon.isNull())
        sizes = {(s.width(), s.height()) for s in icon.availableSizes()}
        self.assertIn((32, 32), sizes)
        self.assertIn((256, 256), sizes)

        with tempfile.TemporaryDirectory() as tmpdir:
            assets_dir = Path(tmpdir) / "assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(__file__).resolve().parent.parent / "assets" / "icon.ico", assets_dir / "icon.ico")
            with patch.object(sys, "_MEIPASS", tmpdir, create=True):
                meipass_icon = create_app_icon()
                self.assertFalse(meipass_icon.isNull())

        with patch.object(Path, "is_file", return_value=False):
            fallback_icon = create_app_icon()
            self.assertFalse(fallback_icon.isNull())
            fallback_sizes = {(s.width(), s.height()) for s in fallback_icon.availableSizes()}
            self.assertIn((32, 32), fallback_sizes)

    def test_splash_screen_has_native_icon_on_startup(self):
        self.app.setWindowIcon(create_app_icon())
        splash = SplashScreen()
        self.addCleanup(splash.close)
        self.assertFalse(splash.windowIcon().isNull())

        splash.show()
        self.app.processEvents()

        if sys.platform == "win32":
            hwnd = int(splash.winId())
            WM_GETICON = 0x007F
            icon_small = ctypes.windll.user32.SendMessageW(hwnd, WM_GETICON, 0, 0)
            icon_big = ctypes.windll.user32.SendMessageW(hwnd, WM_GETICON, 1, 0)
            self.assertNotEqual(icon_small, 0)
            self.assertNotEqual(icon_big, 0)


if __name__ == "__main__":
    unittest.main()
