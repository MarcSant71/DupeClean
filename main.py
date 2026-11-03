"""
Main entry point for DupeClean.
Initializes the Hardware Gatekeeper, validates the NVIDIA RTX GPU, and launches the UI.
"""

import os
import sys
import traceback
from pathlib import Path
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import Qt, QThread, Signal

from dupeclean.core.hardware import enforce_high_performance_gpu
from dupeclean.ui.theme import DARK_THEME_QSS
from dupeclean.ui.splash import SplashScreen


class HardwareInitWorker(QThread):
    """Valida o hardware e inicializa o PyTorch sem congelar a animação da Splash Screen."""
    finished_init = Signal(bool, str, dict)

    def run(self):
        from dupeclean.core.hardware import check_gpu_compatibility
        is_compatible, error_msg, gpu_info = check_gpu_compatibility()
        self.finished_init.emit(is_compatible, error_msg, gpu_info)


def excepthook(exc_type, exc_value, exc_tb):
    """Captura exceções globais e grava em crash.log para diagnóstico fácil."""
    error_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    try:
        log_file = Path(__file__).resolve().parent / "crash.log"
        log_file.write_text(error_msg, encoding="utf-8")
    except Exception:
        pass
    print(error_msg, file=sys.stderr)
    try:
        QMessageBox.critical(
            None,
            "Unexpected Error - DupeClean",
            f"An error occurred while running the application:\n\n{exc_value}\n\nDetails saved to crash.log"
        )
    except Exception:
        pass


sys.excepthook = excepthook


def main():
    # 0. Força a GPU dedicada de alta performance (NVIDIA RTX)
    enforce_high_performance_gpu()

    # Suporte a High DPI no Windows
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_THEME_QSS)

    # 1. Exibe a Splash Screen imediatamente (< 50ms) com animação fluida ativa
    splash = SplashScreen()
    splash.show()
    splash.start_animation()
    app.processEvents()

    # 2. Hardware Gatekeeper: Validação em background sem congelar o loop de eventos
    worker = HardwareInitWorker()
    main_window_holder = []

    def on_hardware_ready(is_compatible, error_msg, gpu_info):
        if not is_compatible:
            splash.close()
            QMessageBox.critical(
                None,
                "Incompatible Hardware - DupeClean",
                error_msg
            )
            app.exit(1)
            return

        # 3. Inicialização da Interface Principal
        from dupeclean.ui.main_window import MainWindow
        window = MainWindow(gpu_info=gpu_info)
        main_window_holder.append(window)
        window.show()

        # Fecha a splash screen após a janela principal carregar
        splash.finish(window)

    worker.finished_init.connect(on_hardware_ready)
    worker.start()

    exit_code = app.exec()
    os._exit(exit_code)


if __name__ == "__main__":
    main()
