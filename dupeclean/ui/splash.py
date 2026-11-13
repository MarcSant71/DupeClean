"""
Instant splash screen for DupeClean startup.
Displays immediately (< 50ms) while PyTorch and GPU run hardware initialization.
"""

from typing import Optional
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QProgressBar,
    QGraphicsDropShadowEffect, QApplication
)
from PySide6.QtGui import QColor, QFont

from dupeclean.ui.theme import create_app_icon


class SplashScreen(QWidget):
    """Splash Screen com design Dark moderno e feedback de carregamento."""

    def __init__(self):
        super().__init__()
        # Usar Window + FramelessWindowHint para vincular a splash ao Virtual Desktop ativo do Windows.
        # Evita flags como WindowStaysOnTopHint e SplashScreen (WS_EX_TOOLWINDOW) que fazem o Windows
        # propagar a janela como overlay global em todos os workspaces/desktops virtuais.
        self.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.FramelessWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        app_icon = QApplication.windowIcon()
        self.setWindowIcon(app_icon if not app_icon.isNull() else create_app_icon())
        self.setFixedSize(490, 250)
        self._init_ui()

    def _init_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(20, 20, 20, 20)

        # Container Principal
        container = QWidget()
        container.setObjectName("splashContainer")
        container.setStyleSheet("""
            QWidget#splashContainer {
                background-color: #18181b;
                border: 1px solid #27272a;
                border-radius: 16px;
            }
        """)

        # Sombra suave
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(40)
        shadow.setColor(QColor(0, 0, 0, 180))
        shadow.setOffset(0, 10)
        container.setGraphicsEffect(shadow)

        layout = QVBoxLayout(container)
        # Margens calculadas para simetria exata (35px topo ao título, 35px barra à base)
        layout.setContentsMargins(34, 14, 34, 28)
        layout.setSpacing(6)

        # 1. Título Centralizado (Escalado em 50%: 36px font, altura 56px, background transparente)
        lbl_title = QLabel("DupeClean")
        lbl_title.setStyleSheet("font-size: 36px; font-weight: 800; color: #38bdf8; background: transparent;")
        lbl_title.setFixedHeight(56)
        lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl_title)

        # 2. Subtítulo Conciso Centralizado (Escalado em 50%: 18px font, altura 28px)
        lbl_sub = QLabel("GPU-Accelerated Duplicate File Finder")
        lbl_sub.setStyleSheet("font-size: 18px; color: #a1a1aa; font-weight: 500; background: transparent;")
        lbl_sub.setFixedHeight(28)
        lbl_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl_sub)

        # 3. Status de Carregamento Centralizado (Escalado em 50%: 18px font, altura 28px)
        self.lbl_status = QLabel("Initializing...")
        self.lbl_status.setStyleSheet("color: #e4e4e7; font-size: 18px; background: transparent;")
        self.lbl_status.setFixedHeight(28)
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_status)

        # 4. Barra de Carregamento Ativa (Escalada em 50%: altura 9px)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(9)
        self.progress.setStyleSheet("""
            QProgressBar {
                border: 1px solid #27272a;
                border-radius: 4px;
                background-color: #202024;
                height: 9px;
                min-height: 9px;
                max-height: 9px;
            }
            QProgressBar::chunk {
                background-color: #38bdf8;
                border-radius: 4px;
            }
        """)
        layout.addWidget(self.progress)

        root_layout.addWidget(container)

        # Centraliza na tela primária
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            x = (geo.width() - self.width()) // 2
            y = (geo.height() - self.height()) // 2
            self.move(x, y)

    def start_animation(self):
        """Inicia animação fluida e contínua da barra de progresso durante a carga."""
        self._current_float_val = 5.0
        self._target_val = 20.0
        self.progress.setValue(5)
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._step_animation)
        self._anim_timer.start(25)  # 40 fps suaves

    def set_target_progress(self, target: int, message: Optional[str] = None):
        """Define um novo patamar de progresso para a interpolação suave da barra."""
        self._target_val = max(self._target_val, float(target))
        if message:
            self.lbl_status.setText(message)

    def _step_animation(self):
        if self._current_float_val < self._target_val:
            diff = self._target_val - self._current_float_val
            step = max(0.25, diff * 0.15)
            self._current_float_val = min(self._target_val, self._current_float_val + step)
            self.progress.setValue(int(self._current_float_val))
        elif self._current_float_val < 98.0 and self._target_val < 100.0:
            # Creep suave e sutil enquanto aguarda a próxima etapa real (elimina qualquer congelamento visual)
            self._current_float_val += 0.05
            self.progress.setValue(int(self._current_float_val))

    def set_progress(self, value: int, message: Optional[str] = None):
        """Define o valor do progresso e repinta imediatamente."""
        self._target_val = max(getattr(self, "_target_val", 0.0), float(value))
        self._current_float_val = float(value)
        self.progress.setValue(value)
        if message:
            self.lbl_status.setText(message)
        QApplication.processEvents()

    def set_status(self, message: str):
        """Atualiza o texto de carregamento e processa eventos do Qt imediatamente."""
        self.lbl_status.setText(message)
        QApplication.processEvents()

    def finish(self, main_window=None):
        """Conclui a barra em 100%, transiciona para a janela principal e fecha a splash."""
        if hasattr(self, "_anim_timer") and self._anim_timer.isActive():
            self._anim_timer.stop()
        self.progress.setValue(100)
        QApplication.processEvents()
        self.close()
