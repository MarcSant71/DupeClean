"""
DupeClean Main Window (PySide6).
Combines tool sidebar, results tree, action controls,
similarity slider, and folder panel with Reference protection and Drag & Drop.
"""

import os
import shutil
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
import send2trash

from PySide6.QtCore import Qt, Slot, QTimer, QSize, QUrl, QRectF
from PySide6.QtGui import QIcon, QPixmap, QPainter, QFont, QPen, QColor, QDesktopServices, QBrush
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QGridLayout, QListWidget,
    QListWidgetItem, QPushButton, QLabel, QSlider, QProgressBar,
    QMessageBox, QFileDialog, QSplitter, QFrame, QSizePolicy, QMenu,
    QApplication, QStyledItemDelegate, QStyle, QStyleOptionViewItem,
    QStackedWidget
)

from dupeclean import __version__
from dupeclean.core.models import ScanMode, DuplicateGroup, FileRecord
from dupeclean.core.scanner import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from dupeclean.core.cache import CacheManager
from dupeclean.ui.folder_panel import FolderPanel
from dupeclean.ui.results_table import ResultsWidget, format_size
from dupeclean.ui.preview_panel import PreviewPanel
from dupeclean.ui.progress_bar import SmoothProgressBar
from dupeclean.ui.help_dialog import HelpDialog
from dupeclean.worker import ScanWorker, FileActionWorker, CacheClearWorker


class ModeItemDelegate(QStyledItemDelegate):
    """Render the subtle gray vertical active-selection pill on the left edge."""

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        # Suppress the native Windows 11 indicator to draw the clean pill
        opt.state &= ~QStyle.StateFlag.State_HasFocus
        super().paint(painter, opt, index)

        # If the item is selected, draw a subtle gray vertical pill on the left edge
        if option.state & QStyle.StateFlag.State_Selected:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            indicator_color = QColor("#71717a") if (option.widget and option.widget.isEnabled()) else QColor("#3f3f46")
            painter.setBrush(QBrush(indicator_color))
            pill_width = 3.0
            pill_height = max(14.0, float(option.rect.height() - 14.0))
            pill_x = float(option.rect.left() + 2)
            pill_y = float(option.rect.top() + (option.rect.height() - pill_height) / 2.0)
            painter.drawRoundedRect(QRectF(pill_x, pill_y, pill_width, pill_height), 1.5, 1.5)
            painter.restore()


class MainWindow(QMainWindow):
    """Janela principal do aplicativo DupeClean."""

    @staticmethod
    def _create_app_icon() -> QIcon:
        """Return the official application icon for the title bar and taskbar."""
        from dupeclean.ui.theme import create_app_icon
        return create_app_icon()

    def __init__(self, gpu_info: Dict[str, Any]):
        super().__init__()
        self.gpu_info = gpu_info
        self.worker: ScanWorker = None
        self.file_worker: Optional[FileActionWorker] = None
        self.current_mode = ScanMode.EXACT
        self._was_cancelled: bool = False
        self._is_busy: bool = False

        self.setWindowIcon(self._create_app_icon())
        self.setWindowTitle(f"DupeClean v{__version__} - GPU-Accelerated Duplicate File Finder")
        self.resize(1280, 820)
        self.setMinimumSize(900, 560)
        self._init_ui()
        self._restore_app_state()
        self._sync_controls_state(busy=False)

    def _restore_app_state(self):
        """Restore saved folders and previous preferences from DupeClean.db."""
        try:
            cache = CacheManager()
            saved_folders = cache.get_setting("saved_folders")
            if saved_folders:
                self.folder_panel.import_state(saved_folders)
        except Exception as e:
            print(f"[WARN] Could not restore saved folders: {e}")
        finally:
            self._sync_controls_state(busy=False)

    def _init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_h_layout = QHBoxLayout(central_widget)
        main_h_layout.setContentsMargins(0, 0, 0, 0)
        main_h_layout.setSpacing(0)

        # -------------------------------------------------------------
        # 1. Sidebar - Scan Modes & Tools
        # -------------------------------------------------------------
        sidebar_widget = QWidget()
        sidebar_widget.setObjectName("sidebarContainer")
        sidebar_widget.setFixedWidth(220)
        sidebar_layout = QVBoxLayout(sidebar_widget)
        sidebar_layout.setContentsMargins(10, 15, 10, 15)
        sidebar_layout.setSpacing(10)

        # Logo / Title
        title_layout = QHBoxLayout()
        title_layout.setSpacing(8)
        title_layout.setContentsMargins(0, 0, 0, 0)

        lbl_icon = QLabel("⚡")
        lbl_icon.setStyleSheet("font-size: 22px;")

        lbl_title = QLabel("DupeClean")
        lbl_title.setStyleSheet("font-size: 20px; font-weight: bold; color: #38bdf8; letter-spacing: 0.5px;")

        title_layout.addWidget(lbl_icon)
        title_layout.addWidget(lbl_title)
        title_layout.addStretch()

        sidebar_layout.addLayout(title_layout)

        lbl_subtitle = QLabel("GPU-Accelerated Duplicate File Finder")
        lbl_subtitle.setStyleSheet("font-size: 11px; color: #71717a;")
        sidebar_layout.addWidget(lbl_subtitle)

        # GPU Badge (KISS: Minimalist, single outer frame, unified centered layout)
        gpu_box = QFrame()
        gpu_box.setObjectName("gpuBox")
        gpu_box.setStyleSheet(
            "QFrame#gpuBox { background-color: #202024; border: 1px solid #2e2e33; border-radius: 6px; padding: 4px 6px; } "
            "QLabel { border: none; background: transparent; padding: 0px; }"
        )
        has_gpu = bool(self.gpu_info.get("has_cuda", False))
        if has_gpu:
            badge_color = "#4ade80"
            badge_text = self.gpu_info.get("name", "NVIDIA GPU")
            badge_tooltip = f"{self.gpu_info.get('arch', 'RTX')} • {self.gpu_info.get('vram_gb', 0)} GB VRAM"
        else:
            badge_color = "#a1a1aa"
            badge_text = "NO GPU"
            badge_tooltip = (
                "Running in CPU Mode (no compatible NVIDIA RTX GPU detected). "
                "An NVIDIA RTX GPU is recommended for superior performance."
            )

        gpu_box.setToolTip(badge_tooltip)
        gpu_layout = QHBoxLayout(gpu_box)
        gpu_layout.setContentsMargins(4, 4, 4, 4)
        gpu_layout.setSpacing(6)

        lbl_dot = QLabel()
        lbl_dot.setFixedSize(12, 12)
        lbl_dot.setPixmap(self._create_status_dot_pixmap(badge_color, size=12))
        lbl_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)

        lbl_gpu_name = QLabel(badge_text)
        lbl_gpu_name.setStyleSheet(f"font-weight: 600; font-size: 11px; color: {badge_color};")

        gpu_layout.addStretch()
        gpu_layout.addWidget(lbl_dot)
        gpu_layout.addWidget(lbl_gpu_name)
        gpu_layout.addStretch()

        self.gpu_box = gpu_box
        self.gpu_dot_label = lbl_dot
        self.gpu_badge_label = lbl_gpu_name
        sidebar_layout.addWidget(gpu_box)

        sidebar_layout.addSpacing(10)

        # Mode List (Single box holding the 3 modes)
        self.sidebar_modes = QListWidget()
        self.sidebar_modes.setObjectName("sidebarModes")
        self.sidebar_modes.setItemDelegate(ModeItemDelegate(self.sidebar_modes))
        self.sidebar_modes.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.sidebar_modes.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_modes.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar_modes.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        item_exact = QListWidgetItem("📁 Duplicate Files")
        item_exact.setData(Qt.ItemDataRole.UserRole, ScanMode.EXACT)
        item_exact.setToolTip("Finds 100% identical files using accelerated xxHash.")
        item_exact.setSizeHint(QSize(230, 40))

        item_images = QListWidgetItem("🖼️ Similar Images")
        item_images.setData(Qt.ItemDataRole.UserRole, ScanMode.IMAGES)
        item_images.setToolTip("Finds identical or similar images using GPU Tensor Cores.")
        item_images.setSizeHint(QSize(230, 40))

        item_videos = QListWidgetItem("🎬 Similar Videos")
        item_videos.setData(Qt.ItemDataRole.UserRole, ScanMode.VIDEOS)
        item_videos.setToolTip("Finds similar videos using dedicated NVDEC hardware decoding.")
        item_videos.setSizeHint(QSize(230, 40))

        self.sidebar_modes.addItem(item_exact)
        self.sidebar_modes.addItem(item_images)
        self.sidebar_modes.addItem(item_videos)
        self.sidebar_modes.setCurrentRow(0)
        self.sidebar_modes.currentRowChanged.connect(self._on_mode_changed)
        self.sidebar_modes.setFixedHeight(128)

        sidebar_layout.addWidget(self.sidebar_modes)

        # Similarity Parameter Box (for Images & Videos)
        self.container_slider = QFrame()
        self.container_slider.setObjectName("sliderContainer")
        self.container_slider.setStyleSheet(
            "QFrame#sliderContainer { background-color: #202024; border: 1px solid #2e2e33; border-radius: 6px; padding: 6px; }"
            "QFrame#sliderContainer:disabled { background-color: #18181b; border: 1px solid #222226; }"
        )
        slider_box_layout = QVBoxLayout(self.container_slider)
        slider_box_layout.setContentsMargins(6, 6, 6, 6)
        slider_box_layout.setSpacing(6)

        slider_header = QHBoxLayout()
        lbl_sim = QLabel("Similarity Threshold:")
        lbl_sim.setStyleSheet(
            "QLabel { font-size: 11px; color: #a1a1aa; font-weight: 500; border: none; background: transparent; padding: 0; }"
            "QLabel:disabled { color: #52525b; }"
        )
        self.lbl_sim_val = QLabel("85%")
        self.lbl_sim_val.setStyleSheet(
            "QLabel { font-size: 12px; font-weight: bold; color: #38bdf8; border: none; background: transparent; padding: 0; }"
            "QLabel:disabled { color: #52525b; }"
        )
        slider_header.addWidget(lbl_sim)
        slider_header.addStretch()
        slider_header.addWidget(self.lbl_sim_val)
        slider_box_layout.addLayout(slider_header)

        self.slider_sim = QSlider(Qt.Orientation.Horizontal)
        self.slider_sim.setStyleSheet("QSlider { background: transparent; }")
        self.slider_sim.setRange(70, 100)
        self.slider_sim.setValue(85)
        self.slider_sim.valueChanged.connect(lambda v: self.lbl_sim_val.setText(f"{v}%"))
        slider_box_layout.addWidget(self.slider_sim)

        self.container_slider.setVisible(False)
        sidebar_layout.addWidget(self.container_slider)

        sidebar_layout.addStretch()

        # Cache Management (Inline Swap Card - zero modal popups)
        self.cache_stack = QStackedWidget()
        self.cache_stack.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        self.btn_clear_cache = QPushButton("🧹 Clear Cache")
        self.btn_clear_cache.setStyleSheet(
            "QPushButton { background-color: #202024; border: 1px solid #2e2e33; color: #a1a1aa; font-size: 13px; padding: 7px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #27272a; color: #f43f5e; border-color: #f43f5e; }"
            "QPushButton:disabled { background-color: #18181b; border: 1px solid #27272a; color: #52525b; }"
        )
        self.btn_clear_cache.setToolTip("Clears the persistent hash and embedding cache.")
        self.btn_clear_cache.clicked.connect(self._clear_cache_prompt)

        cache_confirm_card = QFrame()
        cache_confirm_card.setObjectName("cacheConfirmCard")
        cache_confirm_card.setStyleSheet(
            "#cacheConfirmCard { background-color: #202024; border: 1px solid #3f3f46; border-radius: 4px; }"
        )
        confirm_layout = QVBoxLayout(cache_confirm_card)
        confirm_layout.setContentsMargins(8, 7, 8, 7)
        confirm_layout.setSpacing(6)

        lbl_confirm = QLabel(
            "<span style='font-size:13px; font-weight:600; color:#e4e4e7;'>Clear Cache</span>"
            "<br><span style='font-size:11px; color:#a1a1aa;'>Are You Sure?</span>"
        )
        lbl_confirm.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl_confirm.setStyleSheet("background: transparent; border: none;")
        confirm_layout.addWidget(lbl_confirm)

        confirm_btns = QHBoxLayout()
        confirm_btns.setContentsMargins(0, 0, 0, 0)
        confirm_btns.setSpacing(6)

        self.btn_cache_yes = QPushButton("Yes")
        self.btn_cache_yes.setStyleSheet(
            "QPushButton { background-color: #27272a; border: 1px solid #3f3f46; color: #a1a1aa; font-size: 11px; padding: 4px 8px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #f43f5e; border-color: #f43f5e; color: #ffffff; }"
            "QPushButton:focus { border-color: #f43f5e; color: #f43f5e; }"
            "QPushButton:disabled { background-color: #18181b; border-color: #27272a; color: #52525b; }"
        )
        self.btn_cache_yes.clicked.connect(self._execute_inline_cache_clear)

        self.btn_cache_no = QPushButton("No")
        self.btn_cache_no.setStyleSheet(
            "QPushButton { background-color: #27272a; border: 1px solid #38bdf8; color: #ffffff; font-size: 11px; font-weight: 600; padding: 4px 8px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #3f3f46; border-color: #38bdf8; color: #ffffff; }"
            "QPushButton:disabled { background-color: #18181b; border-color: #27272a; color: #52525b; }"
        )
        self.btn_cache_no.clicked.connect(lambda: self.cache_stack.setCurrentIndex(0))

        confirm_btns.addWidget(self.btn_cache_yes)
        confirm_btns.addWidget(self.btn_cache_no)
        confirm_layout.addLayout(confirm_btns)

        self.cache_stack.addWidget(self.btn_clear_cache)
        self.cache_stack.addWidget(cache_confirm_card)
        sidebar_layout.addWidget(self.cache_stack)

        main_h_layout.addWidget(sidebar_widget)

        # -------------------------------------------------------------
        # 2. Main Panel (Controls, Results Table and Folders)
        # -------------------------------------------------------------
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(15, 15, 15, 15)
        right_layout.setSpacing(10)

        # 2.1 Action Bar (Compacto e Responsivo)
        top_bar_layout = QHBoxLayout()
        top_bar_layout.setContentsMargins(0, 0, 0, 0)
        top_bar_layout.setSpacing(8)

        # Unified Action Container (Segmented Card style matching Side Menu)
        actions_container = QFrame()
        actions_container.setObjectName("topActionBarContainer")
        actions_layout = QHBoxLayout(actions_container)
        actions_layout.setContentsMargins(3, 3, 3, 3)
        actions_layout.setSpacing(2)

        self.btn_scan = QPushButton("🔍 Scan")
        self.btn_scan.clicked.connect(self._start_scan)

        self.btn_stop = QPushButton("🛑 Stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_scan)

        actions_layout.addWidget(self.btn_scan)
        actions_layout.addWidget(self.btn_stop)

        # Selection Dropdown Menu
        self.btn_select_menu = QPushButton("📋 Select")
        menu_select = QMenu(self)
        menu_select.addAction("Select Duplicates (Smart)", lambda: self.results_widget.select_smart_duplicates())
        menu_select.addSeparator()
        menu_select.addAction("Select Smaller Files", lambda: self.results_widget.select_custom("except_biggest_size"))
        menu_select.addAction("Select Larger Files", lambda: self.results_widget.select_custom("except_smallest_size"))
        menu_select.addSeparator()
        menu_select.addAction("Select Oldest", lambda: self.results_widget.select_custom("except_newest"))
        menu_select.addAction("Select Newest", lambda: self.results_widget.select_custom("except_oldest"))
        menu_select.addSeparator()
        menu_select.addAction("Select Longest Path", lambda: self.results_widget.select_custom("except_shortest_path"))
        menu_select.addAction("Select Shortest Path", lambda: self.results_widget.select_custom("except_longest_path"))
        menu_select.addSeparator()
        menu_select.addAction("Invert Selection in Group", lambda: self.results_widget.select_custom("invert_in_group"))
        menu_select.addAction("Invert All", lambda: self.results_widget.invert_all())
        menu_select.addSeparator()
        menu_select.addAction("Unselect All", lambda: self.results_widget.unselect_all())
        menu_select.addAction("Select All", lambda: self.results_widget.select_all())
        menu_select.addSeparator()
        menu_select.addAction("Expand All Groups", lambda: self.results_widget.expand_all())
        menu_select.addAction("Collapse All Groups", lambda: self.results_widget.collapse_all())
        self.btn_select_menu.setMenu(menu_select)

        # Action Buttons (Start disabled until the user checks at least 1 file)
        self.btn_move = QPushButton("📁 Move")
        self.btn_move.setToolTip("Moves selected files to another folder.")
        self.btn_move.setEnabled(False)
        self.btn_move.clicked.connect(self._move_selected_files)

        self.btn_delete_trash = QPushButton("🗑️ Trash")
        self.btn_delete_trash.setToolTip("Sends selected files to the Windows Recycle Bin.")
        self.btn_delete_trash.setEnabled(False)
        self.btn_delete_trash.clicked.connect(self._delete_selected_to_trash)

        self.btn_delete_perm = QPushButton("❌ Delete")
        self.btn_delete_perm.setToolTip("Permanently deletes selected files from disk.")
        self.btn_delete_perm.setEnabled(False)
        self.btn_delete_perm.clicked.connect(self._delete_selected_permanently)

        self.btn_save = QPushButton("💾 Save")
        self.btn_save.setToolTip("Exports duplicate list to a text file.")
        self.btn_save.clicked.connect(self._export_results)

        actions_layout.addWidget(self.btn_select_menu)
        actions_layout.addWidget(self.btn_move)
        actions_layout.addWidget(self.btn_delete_trash)
        actions_layout.addWidget(self.btn_delete_perm)
        actions_layout.addWidget(self.btn_save)

        top_bar_layout.addWidget(actions_container)
        top_bar_layout.addStretch()

        # Circular Help Button (?) - Aligned on the far right
        self.btn_help = QPushButton("?")
        self.btn_help.setObjectName("helpButton")
        self.btn_help.setFixedSize(28, 28)
        self.btn_help.setToolTip("Help & Technical Documentation")
        self.btn_help.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_help.clicked.connect(self._open_help_dialog)
        top_bar_layout.addWidget(self.btn_help)

        right_layout.addLayout(top_bar_layout)

        # 2.2 Splitter vertical: Resultados+Preview (cima) e Pastas (baixo)
        self.v_splitter = QSplitter(Qt.Orientation.Vertical)

        # Splitter horizontal no topo: Tabela de Resultados (esquerda) e Preview (direita)
        self.h_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.h_splitter.setChildrenCollapsible(True)

        # Tabela de Resultados
        self.results_widget = ResultsWidget()
        self.results_widget.selection_changed.connect(self._on_selection_changed)
        self.results_widget.selection_progress.connect(self._on_operation_progress)
        self.results_widget.status_message.connect(self._on_status_message)
        self.results_widget.file_selected.connect(self._on_file_selected)
        self.h_splitter.addWidget(self.results_widget)

        # Side Preview Panel (Starts hidden until a photo/video file is clicked)
        self.preview_panel = PreviewPanel()
        self.preview_panel.setVisible(False)
        self.h_splitter.addWidget(self.preview_panel)
        self.h_splitter.setSizes([750, 320])

        self.v_splitter.addWidget(self.h_splitter)

        # Folders Panel (Included with Ref and Excluded) - BOTTOM
        self.folder_panel = FolderPanel()
        self.folder_panel.folders_cleared.connect(self._on_folders_cleared)
        self.folder_panel.folders_changed.connect(lambda _cnt: self._sync_controls_state())
        self.v_splitter.addWidget(self.folder_panel)

        self.v_splitter.setSizes([520, 240])
        right_layout.addWidget(self.v_splitter)

        # 2.3 Status Bar, Telemetry and Progress (Two Stages)
        status_container = QVBoxLayout()
        status_container.setSpacing(6)
        status_container.setContentsMargins(0, 4, 0, 0)

        # Stages widget with 2-row grid (Current Stage and All Stages)
        self.stage_widget = QWidget()
        stage_layout = QGridLayout(self.stage_widget)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setSpacing(6)

        # Linha 1: Current Stage
        self.lbl_stage_current = QLabel("Current Stage:")
        self.lbl_stage_current.setStyleSheet("color: #a1a1aa; font-size: 11px; font-weight: 500;")
        self.lbl_stage_current.setMinimumWidth(84)

        self.progress_current = SmoothProgressBar()
        self.progress_current.setObjectName("progCurrent")
        self.progress_current.setFixedHeight(8)
        self.progress_current.setTextVisible(False)
        self.progress_current.setRange(0, 100)
        self.progress_current.setValue(0)

        self.lbl_pct_current = QLabel("0%")
        self.lbl_pct_current.setStyleSheet("color: #38bdf8; font-size: 11px; font-family: 'Consolas', monospace;")
        self.lbl_pct_current.setFixedWidth(40)
        self.lbl_pct_current.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        stage_layout.addWidget(self.lbl_stage_current, 0, 0)
        stage_layout.addWidget(self.progress_current, 0, 1)
        stage_layout.addWidget(self.lbl_pct_current, 0, 2)

        # Linha 2: All Stages
        self.lbl_stage_all = QLabel("All Stages:")
        self.lbl_stage_all.setStyleSheet("color: #a1a1aa; font-size: 11px; font-weight: 500;")
        self.lbl_stage_all.setMinimumWidth(84)

        self.progress_all = SmoothProgressBar()
        self.progress_all.setObjectName("progAll")
        self.progress_all.setFixedHeight(8)
        self.progress_all.setTextVisible(False)
        self.progress_all.setRange(0, 100)
        self.progress_all.setValue(0)

        self.lbl_pct_all = QLabel("0%")
        self.lbl_pct_all.setStyleSheet("color: #38bdf8; font-size: 11px; font-family: 'Consolas', monospace;")
        self.lbl_pct_all.setFixedWidth(40)
        self.lbl_pct_all.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        stage_layout.addWidget(self.lbl_stage_all, 1, 0)
        stage_layout.addWidget(self.progress_all, 1, 1)
        stage_layout.addWidget(self.lbl_pct_all, 1, 2)

        # Alias progress_bar aponta para progress_all para retrocompatibilidade
        self.progress_bar = self.progress_all

        sp_stage = self.stage_widget.sizePolicy()
        sp_stage.setRetainSizeWhenHidden(True)
        self.stage_widget.setSizePolicy(sp_stage)
        self.stage_widget.setVisible(False)

        # Row 3: Status info
        self.lbl_status = QLabel("Ready to scan. Add folders below.")
        self.lbl_status.setStyleSheet("color: #a1a1aa;")
        self.lbl_status.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.lbl_status.setMinimumWidth(0)

        status_container.addWidget(self.stage_widget)
        status_container.addWidget(self.lbl_status)

        right_layout.addLayout(status_container)

        main_h_layout.addWidget(right_panel)

    @Slot(int)
    def _on_mode_changed(self, row: int):
        item = self.sidebar_modes.item(row)
        if not item:
            return

        new_mode = item.data(Qt.ItemDataRole.UserRole)
        if new_mode == self.current_mode:
            return

        self.current_mode = new_mode

        # Exibe o controle de sensibilidade (slider) apenas para modos visuais
        self.container_slider.setVisible(self.current_mode in (ScanMode.IMAGES, ScanMode.VIDEOS))
        # Keeps the grid and status bar 100% intact, with zero response time (0 ms).

    @Slot(object)
    def _on_file_selected(self, file_rec: Optional[FileRecord]):
        """Update the preview panel when the user single-clicks a photo or video file."""
        if file_rec is not None and file_rec.path.exists():
            ext = file_rec.ext.lower()
            if ext in IMAGE_EXTENSIONS or ext in VIDEO_EXTENSIONS:
                if not self.preview_panel.isVisible():
                    self.preview_panel.setVisible(True)
                    sizes = self.h_splitter.sizes()
                    if len(sizes) == 2 and sizes[1] < 120:
                        total_w = sum(sizes) if sum(sizes) > 0 else 1000
                        self.h_splitter.setSizes([int(total_w * 0.68), int(total_w * 0.32)])
                self.preview_panel.load_file(file_rec)
                return

        # If a header or non-multimedia file was clicked, collapse the panel
        self.preview_panel.clear()
        self.preview_panel.setVisible(False)

    def _sync_controls_state(self, busy: Optional[bool] = None):
        """Synchronize all button enabled states with execution, folders, and results state (KISS)."""
        if busy is not None:
            self._is_busy = busy
        is_busy = bool(
            getattr(self, "_is_busy", False)
            or (self.worker and self.worker.isRunning())
            or (getattr(self, "file_worker", None) and self.file_worker.isRunning())
            or (getattr(self, "cache_worker", None) and self.cache_worker.isRunning())
        )
        has_folders = self.folder_panel.table_included.rowCount() > 0
        has_results = bool(getattr(self.results_widget, "groups", None))
        has_sel = self.results_widget.has_selected_files()

        self.btn_scan.setEnabled(has_folders and not is_busy)
        self.btn_stop.setEnabled(is_busy)
        self.btn_clear_cache.setEnabled(not is_busy)
        if is_busy and hasattr(self, "cache_stack"):
            self.cache_stack.setCurrentIndex(0)
        self.sidebar_modes.setEnabled(not is_busy)
        self.slider_sim.setEnabled(not is_busy)
        self.folder_panel.setEnabled(not is_busy)
        self.btn_select_menu.setEnabled(has_results and not is_busy)
        self.btn_save.setEnabled(has_results and not is_busy)
        self.btn_move.setEnabled(has_sel and not is_busy)
        self.btn_delete_trash.setEnabled(has_sel and not is_busy)
        self.btn_delete_perm.setEnabled(has_sel and not is_busy)

    def _set_scanning_state(self, scanning: bool):
        """Enables/disables UI controls based on scan execution state (KISS)."""
        self._sync_controls_state(busy=scanning)

    def _set_action_busy_state(self, busy: bool):
        """Enables/disables UI controls while a background file action (trash/delete/move) is running."""
        self._sync_controls_state(busy=busy)

    @Slot(str, int, int)
    def _on_operation_progress(self, title: str, current: int, total: int):
        """Update stage bars and percentage for batch operations while preserving visual consistency."""
        if total > 0 and current <= total:
            self.stage_widget.setVisible(True)
            fraction = min(1.0, max(0.0, current / total))
            pct = 100 if current >= total else min(99, int(fraction * 100))

            is_scan_mount = title == "Loading results" and getattr(self, "_is_scan_mounting", False)
            display_title = title
            display_total_stages = getattr(self, "total_stages", 3) + 1
            if is_scan_mount:
                # Preserve strict scan visual consistency:
                # Top bar: 'Current Stage:' advances from 0% to 100% of assembly
                # Bottom bar: 'All Stages:' advances proportionally from previous stages to 100%
                # Linha de baixo: 'Stage N/N: Loading results (X/Y)...'
                display_title = "Loading results"
                self.lbl_stage_current.setText("Current Stage:")
                self.lbl_stage_all.setVisible(True)
                self.progress_all.setVisible(True)
                self.lbl_pct_all.setVisible(True)

                total_stages = getattr(self, "total_stages", 3)
                base_pct = (total_stages / display_total_stages) * 100.0
                stage_weight = (1.0 / display_total_stages) * 100.0
                if current >= total:
                    overall_pct = 100
                else:
                    overall_pct = min(99, int(base_pct + fraction * stage_weight))

                if self.progress_all.maximum() != 100 or self.progress_all.minimum() != 0:
                    self.progress_all.setRange(0, 100)
                self.progress_all.setValue(overall_pct)
                self.lbl_pct_all.setText(f"{overall_pct}%")
            else:
                self.lbl_stage_all.setVisible(False)
                self.progress_all.setVisible(False)
                self.lbl_pct_all.setVisible(False)

                # Map to concise titles that never truncate in the top bar
                stage_label_map = {
                    "delete": "Deleting... ",
                    "Permanently deleting": "Deleting... ",
                    "Deleting": "Deleting... ",
                    "trash": "Recycling... ",
                    "Moving to Recycle Bin": "Recycling... ",
                    "Recycling": "Recycling... ",
                    "move": "Moving... ",
                    "Moving files": "Moving... ",
                    "Moving": "Moving... ",
                    "Selecting smart duplicates": "Selecting... ",
                    "Applying selection rule": "Selecting... ",
                    "Inverting selection": "Inverting... ",
                    "Selecting all duplicates": "Selecting... ",
                    "Unselecting all": "Unselecting... ",
                }
                display_title = stage_label_map.get(title, title)
                if not (display_title.endswith("...") or display_title.endswith("... ") or display_title.endswith(":")):
                    display_title = f"{display_title}:"
                self.lbl_stage_current.setText(display_title)

            if self.progress_current.maximum() != 100 or self.progress_current.minimum() != 0:
                self.progress_current.setRange(0, 100)
            self.progress_current.setValue(pct)
            self.lbl_pct_current.setText(f"{pct}%")
            if is_scan_mount:
                self.lbl_status.setText(
                    f"Stage {display_total_stages}/{display_total_stages}: Loading results ({current:,}/{total:,})..."
                )
            else:
                clean_title = display_title.rstrip(". :")
                self.lbl_status.setText(f"{clean_title} ({current:,}/{total:,})...")
        else:
            self.stage_widget.setVisible(False)
            self.lbl_stage_all.setVisible(True)
            self.progress_all.setVisible(True)
            self.lbl_pct_all.setVisible(True)
            self.lbl_stage_current.setText("Current Stage:")
            self.lbl_pct_current.setText("0%")
            self.progress_current.setRange(0, 100)
            self.progress_current.setValue(0)
            self.lbl_pct_all.setText("0%")
            self.progress_all.setRange(0, 100)
            self.progress_all.setValue(0)

    @Slot(int, "qint64")
    def _on_selection_changed(self, count: int, total_bytes: int):
        """Dynamically update action buttons and status as files are checked/unchecked."""
        is_busy = bool((self.worker and self.worker.isRunning()) or (getattr(self, 'file_worker', None) and self.file_worker.isRunning()))
        has_sel = (count > 0) and not is_busy
        self.btn_move.setEnabled(has_sel)
        self.btn_delete_trash.setEnabled(has_sel)
        self.btn_delete_perm.setEnabled(has_sel)

        if count > 0:
            size_str = format_size(total_bytes)
            self.btn_move.setText(f"📁 Move ({count})")
            self.btn_delete_trash.setText(f"🗑️ Trash ({count})")
            self.btn_delete_perm.setText(f"❌ Delete ({count})")
            self.lbl_status.setText(f"Selected: {count} duplicate file(s) ({size_str}) ready for action.")
        else:
            self.btn_move.setText("📁 Move")
            self.btn_delete_trash.setText("🗑️ Trash")
            self.btn_delete_perm.setText("❌ Delete")

    @Slot(str)
    def _on_status_message(self, msg: str):
        """Show temporary messages in the status bar during synchronous operations (e.g., column sorting)."""
        if msg:
            if not getattr(self, "_saved_status_text", None):
                self._saved_status_text = self.lbl_status.text()
            self.lbl_status.setText(msg)
        else:
            if getattr(self, "_saved_status_text", None):
                self.lbl_status.setText(self._saved_status_text)
                self._saved_status_text = ""

    @Slot()
    def _on_folders_cleared(self):
        """When all scan sources are removed, clear the duplicate grid, close preview, and reset status."""
        if self.worker and self.worker.isRunning():
            return

        self.results_widget.clear()
        if hasattr(self, "preview_panel"):
            self.preview_panel.clear()
            self.preview_panel.setVisible(False)
        self._sync_controls_state()

        self.lbl_status.setText("Ready to scan. Add folders below.")

        # Persiste o estado limpo das pastas no banco local
        try:
            cache = CacheManager()
            cache.save_setting("saved_folders", self.folder_panel.export_state())
        except Exception:
            pass

    def _clear_cache_prompt(self):
        if (
            (self.worker and self.worker.isRunning())
            or (getattr(self, "file_worker", None) and self.file_worker.isRunning())
            or (getattr(self, "cache_worker", None) and self.cache_worker.isRunning())
        ):
            return

        self.btn_cache_yes.setEnabled(True)
        self.btn_cache_no.setEnabled(True)
        self.cache_stack.setCurrentIndex(1)
        self.btn_cache_no.setFocus()

    def _execute_inline_cache_clear(self):
        if getattr(self, "cache_worker", None) and self.cache_worker.isRunning():
            return
        self.btn_cache_yes.setEnabled(False)
        self.btn_cache_no.setEnabled(False)

        self.cache_worker = CacheClearWorker(parent=self)

        def _on_finished(success: bool, error_msg: str):
            self.cache_worker = None
            self.btn_cache_yes.setEnabled(True)
            self.btn_cache_no.setEnabled(True)
            self.cache_stack.setCurrentIndex(0)
            if success:
                self.btn_clear_cache.setText("✓ Cache Cleared")
                QTimer.singleShot(1500, lambda: self.btn_clear_cache.setText("🧹 Clear Cache"))
            else:
                QMessageBox.critical(self, "Error Clearing Cache", f"Could not clear cache:\n{error_msg}")

        self.cache_worker.finished_clear.connect(_on_finished)
        self.cache_worker.start()

    def _start_scan(self):
        scan_paths = self.folder_panel.get_scan_paths()
        if not scan_paths:
            QMessageBox.warning(
                self,
                "No Folders Selected",
                "Please add at least one folder to the scan list before starting."
            )
            return

        excluded_paths = self.folder_panel.get_excluded_paths()
        threshold = self.slider_sim.value() / 100.0

        self._set_scanning_state(True)
        self.results_widget.clear()
        self.preview_panel.clear()
        self.preview_panel.setVisible(False)
        self._was_cancelled = False
        self._is_scan_mounting = False
        self.current_stage_num = 1
        self.total_stages = 3 if self.current_mode == ScanMode.EXACT else 4
        display_total_stages = self.total_stages + 1
        self.stage_widget.setVisible(True)
        self.progress_current.setRange(0, 0)
        self.lbl_pct_current.setText("...")
        self.progress_all.setRange(0, 100)
        self.progress_all.setValue(0)
        self.lbl_pct_all.setText("0%")
        self.lbl_status.setText(f"Stage 1/{display_total_stages}: Scanning folders...")

        self.worker = ScanWorker(
            scan_paths=scan_paths,
            excluded_paths=excluded_paths,
            mode=self.current_mode,
            similarity_threshold=threshold
        )
        self.worker.progress_stage.connect(self._on_stage_progress)
        self.worker.scan_finished.connect(self._on_scan_finished)
        self.worker.scan_error.connect(self._on_scan_error)
        self.worker.finished.connect(self._finalize_scan_ui)
        self.worker.start()

    def _stop_scan(self):
        if self.worker and self.worker.isRunning():
            self._was_cancelled = True
            self.lbl_status.setText("Stopping scan...")
            self.btn_stop.setEnabled(False)
            self.worker.requestInterruption()
        elif getattr(self, "file_worker", None) and self.file_worker.isRunning():
            self.lbl_status.setText("Stopping file action...")
            self.btn_stop.setEnabled(False)
            self.file_worker.requestInterruption()

    def closeEvent(self, event):
        """Ensures immediate termination of all background processes and threads upon closing the window."""
        if hasattr(self, "preview_panel"):
            self.preview_panel._worker.stop()

        if getattr(self, "cache_worker", None) and self.cache_worker.isRunning():
            self.cache_worker.wait(1000)

        # Persist included and excluded folders in DupeClean.db before closing
        try:
            cache = CacheManager()
            state = self.folder_panel.export_state()
            cache.save_setting("saved_folders", state)
        except Exception as e:
            print(f"[WARN] Failed to persist folders on close: {e}")

        if getattr(self, "file_worker", None) and self.file_worker.isRunning():
            self.file_worker.requestInterruption()
            self.file_worker.wait(400)

        if self.worker and self.worker.isRunning():
            self.lbl_status.setText("Shutting down...")
            self.worker.requestInterruption()
            if not self.worker.wait(400):
                self.worker.terminate()
                self.worker.wait(200)

        event.accept()
        app = QApplication.instance()
        if app:
            app.quit()

    @Slot(str, "qint64", "qint64", int, int)
    def _on_stage_progress(self, msg: str, current: int, total: int, stage_num: int, total_stages: int):
        self.current_stage_num = stage_num
        self.total_stages = total_stages

        # Include the final results assembly stage for full consistency with the All Stages bar
        display_total_stages = total_stages + 1 if total_stages > 0 else total_stages
        self.lbl_status.setText(f"Stage {stage_num}/{display_total_stages}: {msg}")

        # 1. Current Stage Progress
        if total > 0:
            pct_curr = int(min(1.0, current / total) * 100)
            if self.progress_current.maximum() != 100 or self.progress_current.minimum() != 0:
                self.progress_current.setRange(0, 100)
            self.progress_current.setValue(pct_curr)
            self.lbl_pct_current.setText(f"{pct_curr}%")
        else:
            # Smooth indeterminate animation in Stage 1 (no resets or jumps)
            if self.progress_current.maximum() != 0 or self.progress_current.minimum() != 0:
                self.progress_current.setRange(0, 0)
            self.lbl_pct_current.setText("...")

        # 2. Progresso Geral Acumulado (All Stages)
        if total_stages > 0:
            fraction = min(1.0, max(0.0, current / total)) if total > 0 else 0.0
            overall_pct = int(min(99, max(0, ((stage_num - 1 + fraction) / display_total_stages) * 100)))
            if self.progress_all.maximum() != 100 or self.progress_all.minimum() != 0:
                self.progress_all.setRange(0, 100)
            self.progress_all.setValue(overall_pct)
            self.lbl_pct_all.setText(f"{overall_pct}%")

    @Slot(list)
    def _on_scan_finished(self, groups: List[DuplicateGroup]):
        self._was_cancelled = False

        if groups:
            display_total_stages = getattr(self, "total_stages", 3) + 1
            self.lbl_status.setText(
                f"Stage {display_total_stages}/{display_total_stages}: Loading results ({len(groups):,} duplicate groups)..."
            )
            app = QApplication.instance()
            if app:
                app.processEvents()

        self._is_scan_mounting = bool(groups)
        try:
            self.results_widget.set_results(groups)
        finally:
            self._is_scan_mounting = False

        # Finish scan state only after data is fully assembled
        self._set_scanning_state(False)

        # Ensure 100% on both bars upon scan completion for clean visual closure
        self.progress_current.setRange(0, 100)
        self.progress_current.setValue(100)
        self.lbl_pct_current.setText("100%")
        self.progress_all.setRange(0, 100)
        self.progress_all.setValue(100)
        self.lbl_pct_all.setText("100%")
        self.stage_widget.setVisible(True)

        total_files = sum(len(g.files) for g in groups)
        total_reclaim = sum(g.reclaimable_bytes for g in groups)
        reclaim_mb = round(total_reclaim / (1024 * 1024), 2)

        if groups:
            group_word = "group" if len(groups) == 1 else "groups"
            file_word = "file" if total_files == 1 else "files"
            self.lbl_status.setText(
                f"Scan completed! Found {len(groups)} duplicate {group_word} "
                f"({total_files} {file_word}). Estimated space to free: {reclaim_mb} MB."
            )
        else:
            self.lbl_status.setText("Scan completed. No duplicates found with current filters.")

    @Slot(str)
    def _on_scan_error(self, err_msg: str):
        self._finalize_scan_ui()
        self.lbl_status.setText("Scan error.")
        from dupeclean.core.crash_logger import sanitize_text
        sanitized_err = sanitize_text(err_msg)
        msg_box = QMessageBox(self)
        msg_box.setIcon(QMessageBox.Icon.Critical)
        msg_box.setWindowTitle("Execution Error - DupeClean")
        msg_box.setText(
            f"A critical error occurred during the scan:\n\n"
            f"{sanitized_err}\n\n"
            f"An error report has been saved in data/logs."
        )
        msg_box.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg_box.exec()

    def _finalize_scan_ui(self):
        if getattr(self, "_is_scan_mounting", False):
            return
        self._set_scanning_state(False)
        if getattr(self, "_was_cancelled", False):
            self.stage_widget.setVisible(False)
            self.lbl_status.setText("Scan cancelled by user.")
            self._was_cancelled = False

    def _delete_selected_to_trash(self):
        selected = self.results_widget.get_selected_files()
        if not selected:
            QMessageBox.information(
                self,
                "No Files Selected",
                "Please check at least one file in the list to move to the Recycle Bin."
            )
            return

        total_bytes = sum(p.stat().st_size for p in selected if p.exists())
        size_str = format_size(total_bytes)

        confirm = QMessageBox.question(
            self,
            "Confirm Move to Recycle Bin",
            f"Move {len(selected):,} selected file(s) ({size_str}) to the Windows Recycle Bin?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if confirm == QMessageBox.StandardButton.Yes:
            self._start_file_action(selected, "trash")

    def _move_selected_files(self):
        """Moves selected files to another destination folder chosen by the user."""
        selected = self.results_widget.get_selected_files()
        if not selected:
            QMessageBox.information(
                self,
                "No Files Selected",
                "Please check at least one file in the list to move."
            )
            return

        dest_dir = QFileDialog.getExistingDirectory(self, "Select Destination Folder to Move Files")
        if not dest_dir:
            return

        dest_path = Path(dest_dir)
        total_bytes = sum(p.stat().st_size for p in selected if p.exists())
        size_str = format_size(total_bytes)

        confirm = QMessageBox.question(
            self,
            "Confirm Move Files",
            f"Move {len(selected):,} selected file(s) ({size_str}) to:\n{dest_path}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self._start_file_action(selected, "move", dest_dir=dest_path)

    def _delete_selected_permanently(self):
        """Permanently deletes selected files from disk with strict user confirmation."""
        selected = self.results_widget.get_selected_files()
        if not selected:
            QMessageBox.information(
                self,
                "No Files Selected",
                "Please check at least one file in the list to delete."
            )
            return

        total_bytes = sum(p.stat().st_size for p in selected if p.exists())
        size_str = format_size(total_bytes)

        confirm = QMessageBox.warning(
            self,
            "Confirm Permanent Deletion",
            f"WARNING: Permanently delete {len(selected):,} selected file(s) ({size_str}) from disk?\n\n"
            "This action CANNOT be undone and will NOT use the Recycle Bin!",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if confirm == QMessageBox.StandardButton.Yes:
            self._start_file_action(selected, "delete")

    def _start_file_action(self, paths: List[Path], action: str, dest_dir: Optional[Path] = None):
        """Start executing the file action in the background with non-blocking visual feedback."""
        self._action_type = action
        self._action_dest_dir = dest_dir
        self._action_total = len(paths)
        self._set_action_busy_state(True)

        stage_verb_map = {
            "trash": "Recycling... ",
            "delete": "Deleting... ",
            "move": "Moving... ",
        }
        self._action_stage_label = stage_verb_map.get(action, "Processing... ")
        self._on_operation_progress(self._action_stage_label, 0, len(paths))

        self.file_worker = FileActionWorker(paths, action, dest_dir=dest_dir, parent=self)
        self.file_worker.progress.connect(self._on_file_action_progress)
        self.file_worker.finished_action.connect(self._on_file_action_finished)
        self.file_worker.start()

    @Slot(str, "qint64", "qint64")
    def _on_file_action_progress(self, msg: str, current: int, total: int):
        stage_label = getattr(self, "_action_stage_label", "Processing... ")
        self._on_operation_progress(stage_label, current, total)
        self.lbl_status.setText(msg)

    @Slot(set, list)
    def _on_file_action_finished(self, deleted_set: set, errors: list):
        self._on_operation_progress("", 0, 0)

        action = getattr(self, "_action_type", "delete")
        dest_dir = getattr(self, "_action_dest_dir", None)
        self.file_worker = None

        self.results_widget.remove_deleted_files(deleted_set)
        self._set_action_busy_state(False)

        if action == "trash":
            if errors:
                QMessageBox.warning(
                    self,
                    "Completed with Warnings",
                    f"{len(deleted_set):,} files moved to the Recycle Bin.\n"
                    f"{len(errors):,} errors:\n" + "\n".join(errors[:5])
                )
            else:
                QMessageBox.information(
                    self,
                    "Success",
                    f"{len(deleted_set):,} file(s) safely moved to the Windows Recycle Bin!"
                )
                self.lbl_status.setText(f"{len(deleted_set):,} file(s) moved to the Recycle Bin.")

        elif action == "delete":
            if errors:
                QMessageBox.warning(
                    self,
                    "Completed with Warnings",
                    f"{len(deleted_set):,} files permanently deleted.\n"
                    f"{len(errors):,} errors:\n" + "\n".join(errors[:5])
                )
            else:
                QMessageBox.information(
                    self,
                    "Success",
                    f"{len(deleted_set):,} file(s) permanently deleted from disk."
                )
                self.lbl_status.setText(f"{len(deleted_set):,} file(s) permanently deleted.")

        elif action == "move":
            dest_name = dest_dir.name if dest_dir else "destination"
            if errors:
                QMessageBox.warning(
                    self,
                    "Completed with Warnings",
                    f"{len(deleted_set):,} file(s) moved to '{dest_name}'.\n"
                    f"{len(errors):,} errors:\n" + "\n".join(errors[:5])
                )
            else:
                QMessageBox.information(
                    self,
                    "Success",
                    f"{len(deleted_set):,} file(s) safely moved to:\n{dest_dir}"
                )
                self.lbl_status.setText(f"{len(deleted_set):,} file(s) moved to '{dest_name}'.")

    def _export_results(self):
        """Exports duplicate results to a text report file."""
        if not self.results_widget.groups:
            QMessageBox.information(
                self,
                "No Results",
                "There are no scan results to save."
            )
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Duplicate List",
            str(Path.home() / "Desktop" / "dupeclean_duplicates.txt"),
            "Text Files (*.txt);;All Files (*.*)"
        )
        if not file_path:
            return

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("=== DUPECLEAN - DUPLICATE REPORT ===\n")
                f.write(f"Generated on: {time.strftime('%m/%d/%Y %H:%M:%S')}\n")
                f.write(f"Mode: {self.current_mode.value.upper()}\n")
                f.write(f"Total Groups: {len(self.results_widget.groups)}\n\n")

                for grp in self.results_widget.groups:
                    f.write("--------------------------------------------------\n")
                    f.write(f"Group #{grp.group_id} | Similarity: {grp.similarity * 100:.1f}%\n")
                    for fl in grp.files:
                        ref_tag = " [REFERENCE]" if fl.is_reference else ""
                        size_mb = fl.size / (1024 * 1024)
                        f.write(f"  • {fl.name} ({size_mb:.2f} MB){ref_tag}\n    {fl.path}\n")
                    f.write("\n")

            QMessageBox.information(
                self,
                "Success",
                f"Report successfully exported to:\n{file_path}"
            )
            self.lbl_status.setText(f"Report saved to: {Path(file_path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Save Error", f"Could not save file:\n{str(e)}")

    def _open_help_dialog(self):
        """Open the Help and Technical Documentation window directly with its tabs."""
        dlg = HelpDialog(self.gpu_info, parent=self)
        dlg.exec()

    def _open_help_tab(self, tab_idx: int = 0):
        """Open the Help and Technical Architecture window on the corresponding tab."""
        dlg = HelpDialog(self.gpu_info, parent=self)
        dlg.open_tab(tab_idx)

    def _show_about_dialog(self):
        """Display the compact About DupeClean dialog."""
        has_gpu = bool(self.gpu_info.get("has_cuda", False))
        if has_gpu:
            gpu_name = self.gpu_info.get("name", "NVIDIA GPU")
            arch = self.gpu_info.get("arch", "RTX")
            vram = self.gpu_info.get("vram_gb", 0)
            hw_line = f"<b>GPU Hardware:</b> {gpu_name} ({arch}, {vram} GB VRAM)<br>"
        else:
            hw_line = "<b>Compute Engine:</b> CPU Mode (FP32 Fallback &bull; RTX GPU recommended for superior speed)<br>"
        text = (
            f"<h3 style='color: #38bdf8; margin-top: 0px;'>⚡ DupeClean v{__version__}</h3>"
            f"<p><b>GPU-Accelerated Duplicate File, Image & Video Finder</b></p>"
            f"<p>Compares continuous 2D DCT perceptual embeddings (768-D images / 3840-D videos) via "
            f"dense all-against-all (N &times; N) matrix multiplication on NVIDIA RTX GPUs (CUDA FP16) "
            f"or universal CPU fallback. An NVIDIA RTX GPU is recommended for superior performance.</p>"
            f"<hr style='border: none; border-top: 1px solid #3f3f46;'>"
            f"<p>{hw_line}"
            f"<b>Developer:</b> Marcelo Santos &bull; Dominio Digital<br>"
            f"<b>Contact:</b> <a style='color: #38bdf8;' href='mailto:contato@dominiodigital.dev.br'>contato@dominiodigital.dev.br</a><br>"
            f"<b>Platform:</b> Windows 10 / 11 x64 (Standalone)<br>"
            f"<b>License:</b> GNU General Public License v3.0 (GPLv3)</p>"
        )
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("About DupeClean")
        msg_box.setTextFormat(Qt.TextFormat.RichText)
        msg_box.setText(text)
        msg_box.setIcon(QMessageBox.Icon.Information)
        msg_box.exec()

    @staticmethod
    def _create_status_dot_pixmap(color_hex: str, size: int = 12) -> QPixmap:
        """Generate a crisp circular icon with antialiasing and DPR for the hardware badge."""
        dpr = 2
        pixmap = QPixmap(size * dpr, size * dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        pixmap.setDevicePixelRatio(dpr)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color_hex))
        painter.drawEllipse(QRectF(0.5, 0.5, size - 1.0, size - 1.0))
        painter.end()
        return pixmap

    def keyPressEvent(self, event) -> None:
        """Redireciona atalhos globais de zoom (Ctrl +/-/0) para a grid quando outro widget estiver em foco."""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
                self.results_widget.tree.zoom_in()
                event.accept()
                return
            if event.key() == Qt.Key.Key_Minus:
                self.results_widget.tree.zoom_out()
                event.accept()
                return
            if event.key() == Qt.Key.Key_0:
                self.results_widget.tree.reset_zoom()
                event.accept()
                return
        super().keyPressEvent(event)
