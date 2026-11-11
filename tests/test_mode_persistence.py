import sys
import time
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication

from dupeclean.core.models import DuplicateGroup, FileRecord, ScanMode
from dupeclean.ui.main_window import MainWindow

def test_mode_persistence():
    app = QApplication.instance() or QApplication(sys.argv)

    gpu_info = {"available": False, "device_name": "CPU", "vram_gb": 0.0}
    window = MainWindow(gpu_info)
    window.show()

    # 1. Simula scan finalizado no modo EXACT
    file1 = FileRecord(path=Path("/tmp/a.txt"), size=100, mtime=1000.0)
    file2 = FileRecord(path=Path("/tmp/b.txt"), size=100, mtime=1000.0)
    exact_group = DuplicateGroup(group_id=1, files=[file1, file2], similarity=1.0)

    window.current_mode = ScanMode.EXACT
    window._on_scan_finished([exact_group])

    assert len(window.results_widget.groups) == 1, "Grid deve conter 1 grupo após scan"
    initial_status = window.lbl_status.text()

    # 2. Troca de modo para IMAGES (linha 1 no sidebar_modes)
    t0 = time.time()
    window._on_mode_changed(1)
    duration = time.time() - t0
    assert duration < 0.05, f"Troca de modo deve ser instantânea (< 50ms), levou {duration:.4f}s"
    assert window.current_mode == ScanMode.IMAGES
    assert window.container_slider.isVisible() is True
    # Grade e status NÃO devem ser destruídos nem alterados!
    assert len(window.results_widget.groups) == 1, "Grade deve ser preservada intacta"
    assert window.lbl_status.text() == initial_status, "Status bar deve ser preservada sem mensagens desnecessárias"

    # 3. Troca de modo para VIDEOS (linha 2)
    window._on_mode_changed(2)
    assert window.current_mode == ScanMode.VIDEOS
    assert window.container_slider.isVisible() is True
    assert len(window.results_widget.groups) == 1

    # 4. Retorna para EXACT (linha 0)
    window._on_mode_changed(0)
    assert window.current_mode == ScanMode.EXACT
    assert window.container_slider.isVisible() is False
    assert len(window.results_widget.groups) == 1
    assert window.results_widget.groups[0].files[0].path == Path("/tmp/a.txt")

    # 5. Limpeza de pastas limpa tudo
    window._on_folders_cleared()
    assert len(window.results_widget.groups) == 0

    print("[OK] test_mode_persistence passou com sucesso!")

if __name__ == "__main__":
    test_mode_persistence()
