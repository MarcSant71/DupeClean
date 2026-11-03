"""
Instant splash screen for DupeClean startup.
Displays immediately (< 50ms) while PyTorch and GPU run hardware initialization.
"""

from typing import Optional
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar,
    QGraphicsDropShadowEffect, QApplication
)
from PySide6.QtGui import QColor, QFont


class SplashScreen(QWidget):
    """Splash Screen com design Krokiet/Dark moderno e feedback de carregamento."""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.SplashScreen |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(460, 215)
        self._init_ui()

    def _init_ui(self):
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(15, 15, 15, 15)

        # Container Principal
        container = QWidget()
        container.setObjectName("splashContainer")
        container.setStyleSheet("""
            QWidget#splashContainer {
                background-color: #18181b;
                border: 1px solid #27272a;
                border-radius: 12px;
            }
        """)

        # Sombra suave
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(30)
        shadow.setColor(QColor(0, 0, 0, 180))
        shadow.setOffset(0, 8)
        container.setGraphicsEffect(shadow)

        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(8)

        # 1. Título e Ícone (Altura fixa de 42px garante que a haste do 'p' nunca seja comprimida pelo layout)
        title_layout = QHBoxLayout()
        title_layout.setSpacing(10)

        lbl_icon = QLabel("⚡")
        lbl_icon.setStyleSheet("font-size: 26px;")
        lbl_icon.setFixedHeight(42)

        lbl_title = QLabel("DupeClean")
        lbl_title.setStyleSheet("font-size: 24px; font-weight: 800; color: #38bdf8;")
        lbl_title.setFixedHeight(42)

        title_layout.addWidget(lbl_icon)
        title_layout.addWidget(lbl_title)
        title_layout.addStretch()
        layout.addLayout(title_layout)

        # 2. Subtítulo Conciso
        lbl_sub = QLabel("GPU-Accelerated Duplicate File Finder")
        lbl_sub.setStyleSheet("font-size: 12px; color: #a1a1aa; font-weight: 500;")
        lbl_sub.setFixedHeight(22)
        layout.addWidget(lbl_sub)

        layout.addSpacing(10)

        # 3. Status de Carregamento
        self.lbl_status = QLabel("Initializing...")
        self.lbl_status.setStyleSheet("color: #e4e4e7; font-size: 12px;")
        self.lbl_status.setFixedHeight(20)
        layout.addWidget(self.lbl_status)

        # 4. Barra de Carregamento Ativa (Fluida e Responsiva)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        self.progress.setStyleSheet("""
            QProgressBar {
                border: 1px solid #27272a;
                border-radius: 3px;
                background-color: #202024;
                height: 6px;
                max-height: 6px;
            }
            QProgressBar::chunk {
                background-color: #38bdf8;
                border-radius: 3px;
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
        self._current_val = 5
        self.progress.setValue(self._current_val)
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._step_animation)
        self._anim_timer.start(25)  # 40 fps suaves

    def _step_animation(self):
        if self._current_val < 92:
            step = 3 if self._current_val < 45 else (2 if self._current_val < 75 else 1)
            self._current_val += step
            self.progress.setValue(self._current_val)

    def set_progress(self, value: int, message: Optional[str] = None):
        """Define o valor exato da barra de progresso e repinta imediatamente."""
        if hasattr(self, "_anim_timer") and self._anim_timer.isActive():
            self._anim_timer.stop()
        self.progress.setValue(value)
        if message:
            self.lbl_status.setText(message)
        QApplication.processEvents()

    def set_status(self, message: str):
        """Atualiza o texto de carregamento e processa eventos do Qt imediatamente."""
        self.lbl_status.setText(message)
        QApplication.processEvents()

    def finish(self, main_window):
        """Conclui a barra em 100%, transiciona para a janela principal e fecha a splash."""
        if hasattr(self, "_anim_timer") and self._anim_timer.isActive():
            self._anim_timer.stop()
        self.progress.setValue(100)
        QApplication.processEvents()
        self.close()
