"""
Smooth, pixel-perfect Progress Bar with strict geometric clipping and rounded pill ends.
Eliminates Qt Stylesheet square-corner artifacts and overflow in indeterminate mode.
"""

from PySide6.QtCore import Qt, QRectF, QTimer
from PySide6.QtGui import QPainter, QPainterPath, QColor, QPen, QBrush
from PySide6.QtWidgets import QProgressBar


class SmoothProgressBar(QProgressBar):
    """
    Barra de progresso personalizada com acabamento arredondado contínuo (pill-shape).
    Garante que tanto no modo determinado (com contagem) quanto no modo indeterminado
    (busca de arquivos) o preenchimento respeite rigorosamente os limites e as curvas
    da geometria externa, sem vazamentos ou cantos retos.
    No modo indeterminado, a animação corre continuamente da esquerda para a direita.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(8)
        self.setTextVisible(False)
        self._anim_pos: float = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(20)  # 50 FPS para animação suave
        self._timer.timeout.connect(self._on_animation_tick)

    def _on_animation_tick(self):
        """Avança a posição do bloco deslizante continuamente da esquerda para a direita."""
        if self.minimum() == 0 and self.maximum() == 0 and self.isVisible():
            step = 0.014  # ~1.4s por ciclo contínuo
            self._anim_pos = (self._anim_pos + step) % 1.0
            self.update()

    def _update_timer_state(self):
        """Atualiza o estado do timer de acordo com a visibilidade e o modo de operação."""
        is_indeterminate = (self.minimum() == 0 and self.maximum() == 0)
        if is_indeterminate and self.isVisible():
            if not self._timer.isActive():
                self._anim_pos = 0.0
                self._timer.start()
        else:
            if self._timer.isActive():
                self._timer.stop()
        self.update()

    def setRange(self, minimum: int, maximum: int):
        super().setRange(minimum, maximum)
        self._update_timer_state()

    def setMinimum(self, minimum: int):
        super().setMinimum(minimum)
        self._update_timer_state()

    def setMaximum(self, maximum: int):
        super().setMaximum(maximum)
        self._update_timer_state()

    def reset(self):
        super().reset()
        self._update_timer_state()

    def setValue(self, val: int):
        super().setValue(val)
        self.update()

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._timer.isActive():
            self._timer.stop()

    def showEvent(self, event):
        super().showEvent(event)
        self._update_timer_state()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = float(self.width())
        h = float(self.height())
        if w <= 2.0 or h <= 2.0:
            return

        # Geometria do invólucro externo (groove)
        rect = QRectF(0.5, 0.5, w - 1.0, h - 1.0)
        radius = 4.0

        groove_path = QPainterPath()
        groove_path.addRoundedRect(rect, radius, radius)

        # 1. Fundo escuro do trilho
        painter.fillPath(groove_path, QBrush(QColor("#18181b")))

        # 2. Máscara interna de recorte (impede estritamente qualquer transbordo do preenchimento)
        inner_rect = QRectF(1.0, 1.0, w - 2.0, h - 2.0)
        inner_radius = 3.0
        inner_path = QPainterPath()
        inner_path.addRoundedRect(inner_rect, inner_radius, inner_radius)

        painter.save()
        painter.setClipPath(inner_path)

        # 3. Renderização do preenchimento ciano
        if self.minimum() == 0 and self.maximum() == 0:
            # Modo Indeterminado: bloco pill correndo continuamente da esquerda para a direita
            chunk_w = max(36.0, inner_rect.width() * 0.30)
            cycle = inner_rect.width()
            offset = (self._anim_pos % 1.0) * cycle

            for base in [offset - cycle, offset, offset + cycle]:
                x = inner_rect.x() + base
                chunk_rect = QRectF(x, inner_rect.y(), chunk_w, inner_rect.height())
                if chunk_rect.right() > inner_rect.left() and chunk_rect.left() < inner_rect.right():
                    chunk_path = QPainterPath()
                    chunk_path.addRoundedRect(chunk_rect, inner_radius, inner_radius)
                    painter.fillPath(chunk_path, QBrush(QColor("#38bdf8")))
        else:
            # Modo Determinado: preenchimento proporcional contínuo
            val = self.value()
            min_v = self.minimum()
            max_v = self.maximum()
            if max_v > min_v:
                pct = max(0.0, min(1.0, (val - min_v) / (max_v - min_v)))
                if pct > 0:
                    chunk_w = inner_rect.width() * pct
                    chunk_rect = QRectF(inner_rect.x(), inner_rect.y(), chunk_w, inner_rect.height())
                    chunk_path = QPainterPath()
                    chunk_path.addRoundedRect(chunk_rect, inner_radius, inner_radius)
                    painter.fillPath(chunk_path, QBrush(QColor("#38bdf8")))

        painter.restore()

        # 4. Borda externa suave
        pen = QPen(QColor("#3f3f46"), 1.0)
        painter.strokePath(groove_path, pen)
