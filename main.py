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
from dupeclean.ui.theme import DARK_THEME_QSS, create_app_icon
from dupeclean.ui.splash import SplashScreen


class HardwareInitWorker(QThread):
    """Valida o hardware, inicializa PyTorch e adapta o schema do banco sem congelar a animação da Splash Screen."""
    progress_update = Signal(int, str)
    finished_init = Signal(bool, str, dict, object)

    def run(self):
        try:
            self.progress_update.emit(25, "Warming up...")
            from dupeclean.core.hardware import check_gpu_compatibility
            is_compatible, error_msg, gpu_info = check_gpu_compatibility()

            self.progress_update.emit(55, "Warming up...")
            if gpu_info.get("has_cuda", False):
                import torch
                if torch.cuda.is_available():
                    torch.cuda.init()

            if not is_compatible:
                self.finished_init.emit(False, error_msg, gpu_info, ("skipped", None))
                return

            self.progress_update.emit(80, "Warming up...")
            from dupeclean.core.cache import CacheManager
            from dupeclean.core.db_schema import FutureSchemaError
            try:
                cache = CacheManager()
                db_outcome = ("ok", cache.schema_result)
            except FutureSchemaError as fse:
                db_outcome = ("future", fse)
            except Exception as db_exc:
                db_outcome = ("error", db_exc)

            self.progress_update.emit(90, "Warming up...")
            self.finished_init.emit(True, "", gpu_info, db_outcome)
        except Exception as e:
            self.finished_init.emit(False, str(e), {}, ("skipped", None))


def excepthook(exc_type, exc_value, exc_tb):
    """Captura exceções globais não tratadas e grava log sanitizado (LGPD) com retenção de 7 dias."""
    log_file_str = "data/logs/"
    try:
        from dupeclean.core.crash_logger import log_critical_crash
        log_path = log_critical_crash((exc_type, exc_value, exc_tb))
        if log_path:
            log_file_str = log_path.name
    except Exception:
        pass

    try:
        QMessageBox.critical(
            None,
            "Unexpected Error - DupeClean",
            f"An unexpected critical error occurred:\n\n{exc_value}\n\n"
            f"An error report has been recorded in {log_file_str}."
        )
    except Exception:
        pass


sys.excepthook = excepthook


def main():
    # 0. Força a GPU dedicada de alta performance (NVIDIA RTX)
    enforce_high_performance_gpu()

    # Identidade explícita no Windows para agrupar Splash, diálogos e janela principal sob o mesmo ícone na taskbar
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ActionSys.DupeClean")
        except Exception:
            pass

    # Suporte a High DPI no Windows
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setWindowIcon(create_app_icon())
    app.setStyleSheet(DARK_THEME_QSS)

    # 1. Exibe a Splash Screen imediatamente (< 50ms) com animação fluida ativa
    splash = SplashScreen()
    splash.show()
    splash.start_animation()
    app.processEvents()

    # 2. Hardware Gatekeeper: Validação em background sem congelar o loop de eventos
    worker = HardwareInitWorker()
    worker.progress_update.connect(splash.set_target_progress)
    main_window_holder = []

    def on_hardware_ready(is_compatible, error_msg, gpu_info, db_outcome):
        if not is_compatible:
            splash.close()
            QMessageBox.critical(
                None,
                "Initialization Error - DupeClean",
                error_msg
            )
            app.exit(1)
            return

        db_kind, db_payload = db_outcome
        if db_kind == "future":
            splash.hide()
            reply = QMessageBox.warning(
                None,
                "Cache Database Version Mismatch - DupeClean",
                f"The cache database on disk was created by a newer version of DupeClean "
                f"(schema v{db_payload.db_version}, expected v{db_payload.expected_version}).\n\n"
                f"Do you want to reset the cache so this version can run? "
                f"(Saved folder lists will be preserved if readable.)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if reply != QMessageBox.StandardButton.Yes:
                splash.close()
                app.exit(0)
                return

            from dupeclean.core.db_schema import reset_database_preserving_settings
            try:
                db_payload = reset_database_preserving_settings(
                    db_payload.db_path, reason="future_schema_reset"
                )
            except Exception as db_exc:
                splash.close()
                QMessageBox.critical(
                    None,
                    "Database Error - DupeClean",
                    f"Could not reset the cache database:\n\n{db_exc}"
                )
                app.exit(1)
                return
        elif db_kind == "error":
            splash.close()
            QMessageBox.critical(
                None,
                "Database Error - DupeClean",
                f"Could not initialize the cache database:\n\n{db_payload}"
            )
            app.exit(1)
            return

        splash.set_target_progress(95, "Warming up...")
        app.processEvents()

        # 3. Inicialização da Interface Principal
        from dupeclean.ui.main_window import MainWindow
        window = MainWindow(gpu_info=gpu_info)
        main_window_holder.append(window)
        window.show()

        # Fecha a splash screen após a janela principal carregar
        splash.finish(window)

        if getattr(db_payload, "status", None) == "reset":
            msg = (
                "An incompatible or corrupted cache database was found and had to be reset "
                "for this version of DupeClean. Your cache will be rebuilt on the next scan."
            )
            if getattr(db_payload, "preserved_settings", False):
                msg += "\n\nSaved folder selections were preserved."
            QMessageBox.information(window, "Cache Database Reset - DupeClean", msg)

    worker.finished_init.connect(on_hardware_ready)
    worker.start()

    exit_code = app.exec()
    os._exit(exit_code)


if __name__ == "__main__":
    main()
