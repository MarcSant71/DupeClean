"""
Painel de visualização lateral (Preview Panel) para imagens e vídeos
com renderização assíncrona, cache LRU em memória e preservação estrita de proporção (aspect ratio).
"""

import io
import os
import sys
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from collections import OrderedDict

from PIL import Image, ImageOps
from PySide6.QtCore import Qt, Signal, Slot, QThread, QObject, QSize
from PySide6.QtGui import QPixmap, QImage, QColor, QFont, QPainter, QIcon
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QSizePolicy, QApplication
)

from dupeclean.core.models import FileRecord
from dupeclean.core.scanner import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from dupeclean.ui.results_table import format_size


class AspectRatioPixmapLabel(QLabel):
    """
    QLabel que redimensiona dinamicamente a imagem mantendo o aspect ratio
    conforme o usuário redimensiona o painel ou o splitter.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(120, 120)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pixmap: Optional[QPixmap] = None

    def set_pixmap(self, pixmap: Optional[QPixmap]):
        self._pixmap = pixmap
        self._update_scaled_pixmap()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_scaled_pixmap()

    def _update_scaled_pixmap(self):
        if self._pixmap and not self._pixmap.isNull():
            w = max(self.width() - 8, 32)
            h = max(self.height() - 8, 32)
            scaled = self._pixmap.scaled(
                w, h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )
            super().setPixmap(scaled)
        else:
            super().setPixmap(QPixmap())


class PreviewWorker(QThread):
    """Worker assíncrono para carregar imagens e extrair frames de vídeo sem travar a interface."""
    preview_ready = Signal(int, object, object, dict)  # (token, file_rec, QPixmap, metadata)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pending_request: Optional[Tuple[int, FileRecord]] = None
        self._lock = False
        self._stop_requested = False

    def request_preview(self, token: int, file_rec: FileRecord):
        self._pending_request = (token, file_rec)
        if not self.isRunning():
            self.start()

    def run(self):
        while self._pending_request is not None and not self._stop_requested:
            token, file_rec = self._pending_request
            self._pending_request = None

            pixmap, meta = self._process_file(file_rec)
            if not self._stop_requested:
                self.preview_ready.emit(token, file_rec, pixmap, meta)

    def stop(self):
        self._stop_requested = True
        self.wait(500)

    def _process_file(self, file_rec: FileRecord) -> Tuple[Optional[QPixmap], Dict[str, Any]]:
        path = file_rec.path
        if not path.exists():
            return None, {"error": "File not found"}

        ext = file_rec.ext.lower()
        try:
            if ext in IMAGE_EXTENSIONS:
                return self._load_image(file_rec)
            elif ext in VIDEO_EXTENSIONS:
                return self._load_video_frame(file_rec)
            else:
                return None, {"error": "Unsupported media format"}
        except Exception as e:
            return None, {"error": str(e)}

    def _load_image(self, file_rec: FileRecord) -> Tuple[Optional[QPixmap], Dict[str, Any]]:
        path = file_rec.path
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)
            orig_w, orig_h = img.size
            img_format = img.format or file_rec.ext.upper().lstrip(".")

            # Redimensiona para preview limitando a 1200px para economizar memória
            max_bound = 1200
            if orig_w > max_bound or orig_h > max_bound:
                img.thumbnail((max_bound, max_bound), Image.Resampling.BILINEAR)

            img_rgba = img.convert("RGBA")
            data = img_rgba.tobytes("raw", "RGBA")
            qimg = QImage(data, img_rgba.width, img_rgba.height, img_rgba.width * 4, QImage.Format.Format_RGBA8888)
            pixmap = QPixmap.fromImage(qimg.copy())

            meta = {
                "kind": "image",
                "width": orig_w,
                "height": orig_h,
                "format": img_format,
                "size": file_rec.size,
                "path": str(path)
            }
            return pixmap, meta

    def _load_video_frame(self, file_rec: FileRecord) -> Tuple[Optional[QPixmap], Dict[str, Any]]:
        path = file_rec.path
        try:
            import av
            is_dav = path.suffix.lower() == ".dav"
            options = {"probesize": "65536", "analyzeduration": "0"} if is_dav else None
            container = av.open(str(path), options=options)
            if not container.streams.video:
                container.close()
                return None, {"error": "No video stream found"}

            stream = container.streams.video[0]
            duration = file_rec.duration
            if duration is None:
                duration = float(container.duration) / 1000000.0 if container.duration else None
                if duration is None and stream.duration and stream.time_base:
                    duration = float(stream.duration * stream.time_base)

            seek_sec = max(0.5, (duration * 0.2)) if (duration and duration > 2.0) else 1.0
            frame_img = None

            # 1. Tenta seek rápido para momento representativo (somente se não for .dav)
            if not is_dav:
                try:
                    target_pts = int(seek_sec / stream.time_base) if stream.time_base else int(seek_sec * 1000000)
                    container.seek(target_pts, any_frame=False, backward=True, stream=stream)
                    for f in container.decode(video=0):
                        frame_img = f.to_image()
                        break
                except Exception:
                    frame_img = None

            # 2. Fallback / .dav: decodifica o primeiro keyframe disponível via demux rápido
            if frame_img is None:
                for pkt in container.demux(stream):
                    if pkt.is_keyframe:
                        decoded = pkt.decode()
                        if decoded:
                            frame_img = decoded[0].to_image()
                            break

            container.close()

            if frame_img:
                orig_w, orig_h = frame_img.size
                max_bound = 1200
                if orig_w > max_bound or orig_h > max_bound:
                    frame_img.thumbnail((max_bound, max_bound), Image.Resampling.BILINEAR)

                img_rgba = frame_img.convert("RGBA")
                data = img_rgba.tobytes("raw", "RGBA")
                qimg = QImage(data, img_rgba.width, img_rgba.height, img_rgba.width * 4, QImage.Format.Format_RGBA8888)
                pixmap = QPixmap.fromImage(qimg.copy())

                meta = {
                    "kind": "video",
                    "width": orig_w,
                    "height": orig_h,
                    "duration": duration,
                    "format": file_rec.ext.upper().lstrip("."),
                    "size": file_rec.size,
                    "path": str(path)
                }
                return pixmap, meta
        except Exception as e:
            return None, {"error": str(e)}

        return None, {"error": "Could not decode video frame"}


class PreviewPanel(QFrame):
    """
    Painel lateral elegante com preview da imagem/vídeo,
    metadados em tempo real e atalhos rápidos.
    """
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("previewPanel")
        self.setMinimumWidth(260)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

        # Cache LRU em memória (máximo 60 itens para latência instantânea de 0ms)
        self._lru_cache: OrderedDict[str, Tuple[QPixmap, Dict[str, Any]]] = OrderedDict()
        self._max_cache_size = 60

        self._current_token = 0
        self._current_file: Optional[FileRecord] = None

        self._worker = PreviewWorker(self)
        self._worker.preview_ready.connect(self._on_preview_ready)

        self._init_ui()

    def _init_ui(self):
        self.setStyleSheet(
            "QFrame#previewPanel {"
            "  background-color: #1a1a1e;"
            "  border: 1px solid #2e2e33;"
            "  border-radius: 6px;"
            "}"
        )

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(8)

        # 1. Barra de Cabeçalho Superior
        header_layout = QHBoxLayout()
        header_layout.setSpacing(6)

        self.lbl_title = QLabel("🖼️ Media Preview")
        self.lbl_title.setStyleSheet("font-size: 12px; font-weight: bold; color: #38bdf8;")
        header_layout.addWidget(self.lbl_title)

        header_layout.addStretch()

        self.btn_close = QPushButton("✕")
        self.btn_close.setFixedSize(22, 22)
        self.btn_close.setToolTip("Close preview panel")
        self.btn_close.setStyleSheet(
            "QPushButton { background: transparent; border: none; color: #71717a; font-size: 13px; font-weight: bold; border-radius: 3px; }"
            "QPushButton:hover { background-color: #27272a; color: #f43f5e; }"
        )
        self.btn_close.clicked.connect(self._on_close_clicked)
        header_layout.addWidget(self.btn_close)

        main_layout.addLayout(header_layout)

        # 2. Área Central de Exibição da Imagem (Aspect Ratio Preserved)
        self.image_container = QFrame()
        self.image_container.setStyleSheet(
            "QFrame { background-color: #141416; border: 1px solid #26262a; border-radius: 4px; }"
        )
        img_box_layout = QVBoxLayout(self.image_container)
        img_box_layout.setContentsMargins(4, 4, 4, 4)

        self.lbl_image = AspectRatioPixmapLabel()
        img_box_layout.addWidget(self.lbl_image)

        self.lbl_status = QLabel("Select an image or video to preview")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_status.setStyleSheet("color: #71717a; font-size: 11px;")
        img_box_layout.addWidget(self.lbl_status)

        main_layout.addWidget(self.image_container, stretch=1)

        # 3. Card Inferior de Metadados
        self.meta_card = QFrame()
        self.meta_card.setStyleSheet(
            "QFrame {"
            "  background-color: #202024;"
            "  border: 1px solid #2e2e33;"
            "  border-radius: 5px;"
            "  padding: 6px;"
            "}"
        )
        meta_layout = QVBoxLayout(self.meta_card)
        meta_layout.setContentsMargins(8, 6, 8, 6)
        meta_layout.setSpacing(4)

        self.lbl_filename = QLabel("")
        self.lbl_filename.setStyleSheet("font-size: 11px; font-weight: bold; color: #f4f4f5;")
        self.lbl_filename.setWordWrap(True)
        meta_layout.addWidget(self.lbl_filename)

        info_row = QHBoxLayout()
        info_row.setSpacing(8)

        self.lbl_resolution = QLabel("")
        self.lbl_resolution.setStyleSheet("font-size: 11px; color: #38bdf8; font-family: 'Consolas', monospace;")
        info_row.addWidget(self.lbl_resolution)

        info_row.addStretch()

        self.lbl_filesize = QLabel("")
        self.lbl_filesize.setStyleSheet("font-size: 11px; color: #a1a1aa;")
        info_row.addWidget(self.lbl_filesize)

        meta_layout.addLayout(info_row)

        main_layout.addWidget(self.meta_card)

        # Inicializa em estado limpo
        self.clear()

    def _on_close_clicked(self):
        self.setVisible(False)
        self.closed.emit()

    def load_file(self, file_rec: Optional[FileRecord]):
        """Solicita o carregamento do preview do arquivo."""
        if not file_rec or not file_rec.path.exists():
            self.clear()
            return

        self._current_file = file_rec
        cache_key = str(file_rec.path)

        # 1. Verifica cache LRU em memória (0 ms)
        if cache_key in self._lru_cache:
            pixmap, meta = self._lru_cache[cache_key]
            # Move para o final (mais recentemente usado)
            self._lru_cache.move_to_end(cache_key)
            self._display_preview(file_rec, pixmap, meta)
            return

        # 2. Não está no cache: prepara interface e dispara worker
        self._current_token += 1
        token = self._current_token

        # Feedback visual sutil enquanto decodifica
        ext = file_rec.ext.lower()
        is_video = ext in VIDEO_EXTENSIONS
        icon = "🎬" if is_video else "🖼️"
        self.lbl_title.setText(f"{icon} {'Video' if is_video else 'Image'} Preview")
        self.lbl_filename.setText(file_rec.name)
        self.lbl_filesize.setText(format_size(file_rec.size))
        self.lbl_resolution.setText("Loading...")
        self.lbl_status.setText("Decoding preview...")
        self.lbl_status.setVisible(True)

        self._worker.request_preview(token, file_rec)

    @Slot(int, object, object, dict)
    def _on_preview_ready(self, token: int, file_rec: FileRecord, pixmap: Optional[QPixmap], meta: dict):
        if token != self._current_token:
            # Requisição obsoleta descartada (usuário já clicou em outro arquivo)
            return

        cache_key = str(file_rec.path)
        if pixmap and not pixmap.isNull():
            self._lru_cache[cache_key] = (pixmap, meta)
            if len(self._lru_cache) > self._max_cache_size:
                self._lru_cache.popitem(last=False)

        self._display_preview(file_rec, pixmap, meta)

    def _display_preview(self, file_rec: FileRecord, pixmap: Optional[QPixmap], meta: dict):
        self._current_file = file_rec
        ext = file_rec.ext.lower()
        is_video = ext in VIDEO_EXTENSIONS
        icon = "🎬" if is_video else "🖼️"
        self.lbl_title.setText(f"{icon} {'Video' if is_video else 'Image'} Preview")

        self.lbl_filename.setText(file_rec.name)
        self.lbl_filename.setToolTip(str(file_rec.path))
        self.lbl_filesize.setText(format_size(file_rec.size))

        if pixmap and not pixmap.isNull():
            self.lbl_image.set_pixmap(pixmap)
            self.lbl_status.setVisible(False)

            w = meta.get("width", 0)
            h = meta.get("height", 0)
            fmt = meta.get("format", "")

            if is_video:
                dur = meta.get("duration")
                if dur:
                    mins = int(dur // 60)
                    secs = int(dur % 60)
                    dur_str = f"{mins:02d}:{secs:02d}"
                    self.lbl_resolution.setText(f"{w}×{h} • {dur_str} • {fmt}")
                else:
                    self.lbl_resolution.setText(f"{w}×{h} • {fmt}")
            else:
                self.lbl_resolution.setText(f"{w}×{h} • {fmt}")

            self.meta_card.setVisible(True)
        else:
            err = meta.get("error", "No preview available")
            self.lbl_image.set_pixmap(None)
            self.lbl_status.setText(f"Preview unavailable ({err})")
            self.lbl_status.setVisible(True)
            self.lbl_resolution.setText("-")
            self.meta_card.setVisible(True)

    def clear(self):
        """Limpa o preview atual e retorna para o estado inicial."""
        self._current_file = None
        self._current_token += 1
        self.lbl_image.set_pixmap(None)
        self.lbl_status.setText("Select an image or video to preview")
        self.lbl_status.setVisible(True)
        self.lbl_filename.setText("")
        self.lbl_resolution.setText("")
        self.lbl_filesize.setText("")
        self.meta_card.setVisible(False)
