"""
Janela Principal do DupeClean (PySide6).
Reúne o painel lateral de ferramentas, tabela de resultados, controles de ação,
slider de similaridade e o painel de pastas com suporte a Referência e Drag & Drop.
"""

import os
import shutil
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
import send2trash

from PySide6.QtCore import Qt, Slot, QTimer
from PySide6.QtGui import QIcon, QPixmap, QPainter, QFont
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QListWidget,
    QListWidgetItem, QPushButton, QLabel, QSlider, QProgressBar,
    QMessageBox, QFileDialog, QSplitter, QFrame, QSizePolicy, QMenu,
    QApplication
)

from dupeclean.core.models import ScanMode, DuplicateGroup
from dupeclean.core.cache import CacheManager
from dupeclean.ui.folder_panel import FolderPanel
from dupeclean.ui.results_table import ResultsWidget
from dupeclean.worker import ScanWorker


class MainWindow(QMainWindow):
    """Janela principal do aplicativo DupeClean."""

    @staticmethod
    def _create_app_icon() -> QIcon:
        """Gera o ícone com o raio de energia para a barra de títulos e barra de tarefas do Windows."""
        icon = QIcon()
        for size in (16, 24, 32, 48, 64, 128):
            pixmap = QPixmap(size, size)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            font_size = int(size * 0.72)
            font = QFont("Segoe UI Emoji", max(10, font_size))
            painter.setFont(font)
            painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "⚡")
            painter.end()
            icon.addPixmap(pixmap)
        return icon

    def __init__(self, gpu_info: Dict[str, Any]):
        super().__init__()
        self.gpu_info = gpu_info
        self.worker: ScanWorker = None
        self.current_mode = ScanMode.EXACT
        self.start_time: Optional[float] = None
        self.latest_current: int = 0
        self.latest_total: int = 0

        # Temporizador independente da UI para contagem contínua e cálculo de ETA
        self.ui_timer = QTimer(self)
        self.ui_timer.setInterval(250)
        self.ui_timer.timeout.connect(self._update_telemetry_tick)

        self.setWindowIcon(self._create_app_icon())
        self.setWindowTitle(f"DupeClean - Deduplicação de Arquivos por GPU ({gpu_info.get('name', 'NVIDIA RTX')})")
        self.resize(1280, 820)
        self._init_ui()

    def _init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_h_layout = QHBoxLayout(central_widget)
        main_h_layout.setContentsMargins(0, 0, 0, 0)
        main_h_layout.setSpacing(0)

        # -------------------------------------------------------------
        # 1. Barra Lateral de Modos de Escaneamento
        # -------------------------------------------------------------
        sidebar_widget = QWidget()
        sidebar_widget.setObjectName("sidebarContainer")
        sidebar_widget.setFixedWidth(260)
        sidebar_layout = QVBoxLayout(sidebar_widget)
        sidebar_layout.setContentsMargins(10, 15, 10, 15)
        sidebar_layout.setSpacing(10)

        # Logo / Título
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

        lbl_subtitle = QLabel("Deduplicação de Arquivos por GPU")
        lbl_subtitle.setStyleSheet("font-size: 11px; color: #71717a;")
        sidebar_layout.addWidget(lbl_subtitle)

        # Badge de GPU (KISS: Minimalista, linha única)
        gpu_box = QFrame()
        gpu_box.setStyleSheet("background-color: #202024; border: 1px solid #2e2e33; border-radius: 6px; padding: 4px 6px;")
        gpu_box.setToolTip(f"{self.gpu_info.get('arch', 'RTX')} • {self.gpu_info.get('vram_gb', 0)} GB VRAM")
        gpu_layout = QHBoxLayout(gpu_box)
        gpu_layout.setContentsMargins(4, 4, 4, 4)
        lbl_gpu_name = QLabel(f"🟢 {self.gpu_info.get('name', 'NVIDIA GPU')}")
        lbl_gpu_name.setStyleSheet("font-weight: 600; font-size: 11px; color: #4ade80;")
        gpu_layout.addWidget(lbl_gpu_name)
        sidebar_layout.addWidget(gpu_box)

        sidebar_layout.addSpacing(10)

        # Lista de Ferramentas
        self.sidebar_modes = QListWidget()
        self.sidebar_modes.setObjectName("sidebarModes")
        self.sidebar_modes.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        item_exact = QListWidgetItem("📁 Arquivos Duplicados")
        item_exact.setData(Qt.ItemDataRole.UserRole, ScanMode.EXACT)
        item_exact.setToolTip("Encontra arquivos 100% idênticos usando xxHash acelerado.")

        item_images = QListWidgetItem("🖼️ Imagens Similares")
        item_images.setData(Qt.ItemDataRole.UserRole, ScanMode.IMAGES)
        item_images.setToolTip("Encontra fotos idênticas ou similares usando Tensor Cores na GPU.")

        item_videos = QListWidgetItem("🎬 Vídeos Similares")
        item_videos.setData(Qt.ItemDataRole.UserRole, ScanMode.VIDEOS)
        item_videos.setToolTip("Encontra vídeos similares usando decodificação por hardware NVDEC.")

        self.sidebar_modes.addItem(item_exact)
        self.sidebar_modes.addItem(item_images)
        self.sidebar_modes.addItem(item_videos)
        self.sidebar_modes.setCurrentRow(0)
        self.sidebar_modes.currentRowChanged.connect(self._on_mode_changed)

        sidebar_layout.addWidget(self.sidebar_modes)
        sidebar_layout.addStretch()

        # Botão de Gerenciamento e Limpeza de Cache
        self.btn_clear_cache = QPushButton("🧹 Limpar Cache")
        self.btn_clear_cache.setStyleSheet(
            "QPushButton { background-color: #202024; border: 1px solid #2e2e33; color: #a1a1aa; font-size: 11px; padding: 6px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #27272a; color: #f43f5e; border-color: #f43f5e; }"
        )
        self.btn_clear_cache.setToolTip("Exibe estatísticas e permite esvaziar o cache persistente de hashes e embeddings.")
        self.btn_clear_cache.clicked.connect(self._clear_cache_prompt)
        sidebar_layout.addWidget(self.btn_clear_cache)

        main_h_layout.addWidget(sidebar_widget)

        # -------------------------------------------------------------
        # 2. Painel Central (Controles, Tabela e Pastas)
        # -------------------------------------------------------------
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(15, 15, 15, 15)
        right_layout.setSpacing(10)

        # 2.1 Barra Superior de Ações (Estilo Krokiet)
        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)

        self.btn_scan = QPushButton("🔍 Escanear")
        self.btn_scan.setObjectName("primaryButton")
        self.btn_scan.setFixedWidth(110)
        self.btn_scan.clicked.connect(self._start_scan)

        self.btn_stop = QPushButton("⏹ Parar")
        self.btn_stop.setObjectName("stopButton")
        self.btn_stop.setFixedWidth(90)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_scan)

        top_bar.addWidget(self.btn_scan)
        top_bar.addWidget(self.btn_stop)

        # Botão com Menu Suspenso de Seleção (Direto e Enxuto)
        self.btn_select_menu = QPushButton("📋 Selecionar ▾")
        menu_select = QMenu(self)
        menu_select.addAction("Selecionar Menores", lambda: self.results_widget.select_custom("except_biggest_size"))
        menu_select.addAction("Selecionar Maiores", lambda: self.results_widget.select_custom("except_smallest_size"))
        menu_select.addSeparator()
        menu_select.addAction("Selecionar Mais Antigos", lambda: self.results_widget.select_custom("except_newest"))
        menu_select.addAction("Selecionar Mais Recentes", lambda: self.results_widget.select_custom("except_oldest"))
        menu_select.addSeparator()
        menu_select.addAction("Selecionar Caminho Mais Longo", lambda: self.results_widget.select_custom("except_shortest_path"))
        menu_select.addAction("Selecionar Caminho Mais Curto", lambda: self.results_widget.select_custom("except_longest_path"))
        menu_select.addSeparator()
        menu_select.addAction("Inverter Seleção no Grupo", lambda: self.results_widget.select_custom("invert_in_group"))
        menu_select.addAction("Inverter Seleção Geral", lambda: self.results_widget.invert_all())
        menu_select.addSeparator()
        menu_select.addAction("Desmarcar Tudo", lambda: self.results_widget.unselect_all())
        menu_select.addAction("Selecionar Tudo", lambda: self.results_widget.select_all())
        self.btn_select_menu.setMenu(menu_select)

        # Botões de Ação Krokiet
        self.btn_move = QPushButton("📁 Mover...")
        self.btn_move.setToolTip("Move os arquivos selecionados para outra pasta.")
        self.btn_move.clicked.connect(self._move_selected_files)

        self.btn_delete_trash = QPushButton("🗑️ Lixeira")
        self.btn_delete_trash.setToolTip("Envia os arquivos selecionados para a Lixeira do Windows.")
        self.btn_delete_trash.clicked.connect(self._delete_selected_to_trash)

        self.btn_delete_perm = QPushButton("❌ Excluir")
        self.btn_delete_perm.setObjectName("dangerButton")
        self.btn_delete_perm.setToolTip("Exclui permanentemente os arquivos selecionados do disco.")
        self.btn_delete_perm.clicked.connect(self._delete_selected_permanently)

        self.btn_save = QPushButton("💾 Salvar...")
        self.btn_save.setToolTip("Exporta a lista de duplicatas para um arquivo de texto.")
        self.btn_save.clicked.connect(self._export_results)

        top_bar.addWidget(self.btn_select_menu)
        top_bar.addWidget(self.btn_move)
        top_bar.addWidget(self.btn_delete_trash)
        top_bar.addWidget(self.btn_delete_perm)
        top_bar.addWidget(self.btn_save)

        top_bar.addSpacing(10)

        # Controle de Similaridade (Slider)
        self.container_slider = QWidget()
        slider_layout = QHBoxLayout(self.container_slider)
        slider_layout.setContentsMargins(0, 0, 0, 0)

        lbl_sim = QLabel("Similaridade:")
        self.lbl_sim_val = QLabel("85%")
        self.lbl_sim_val.setStyleSheet("font-weight: bold; color: #38bdf8; min-width: 35px;")

        self.slider_sim = QSlider(Qt.Orientation.Horizontal)
        self.slider_sim.setRange(70, 100)
        self.slider_sim.setValue(85)
        self.slider_sim.setFixedWidth(120)
        self.slider_sim.valueChanged.connect(lambda v: self.lbl_sim_val.setText(f"{v}%"))

        slider_layout.addWidget(lbl_sim)
        slider_layout.addWidget(self.slider_sim)
        slider_layout.addWidget(self.lbl_sim_val)

        self.container_slider.setVisible(False)
        top_bar.addWidget(self.container_slider)

        top_bar.addStretch()

        right_layout.addLayout(top_bar)

        # 2.2 Splitter vertical: Tabela de Resultados (cima) e Pastas (baixo) — Padrão Krokiet
        splitter = QSplitter(Qt.Orientation.Vertical)

        # Tabela de Resultados - TOPO
        self.results_widget = ResultsWidget()
        splitter.addWidget(self.results_widget)

        # Painel de Pastas (Incluídas com Ref e Excluídas) - BASE
        self.folder_panel = FolderPanel()
        splitter.addWidget(self.folder_panel)

        splitter.setSizes([520, 240])
        right_layout.addWidget(splitter)

        # 2.3 Barra de Status, Telemetria e Progresso (Elástica e Responsiva)
        status_bar_layout = QHBoxLayout()
        status_bar_layout.setSpacing(12)

        self.lbl_status = QLabel("Pronto para escanear. Adicione as pastas abaixo.")
        self.lbl_status.setStyleSheet("color: #a1a1aa;")
        self.lbl_status.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.progress_bar.setMinimumWidth(120)
        self.progress_bar.setVisible(False)

        # Badge de Telemetria (Tempo Decorrido / Estimado / Total)
        self.lbl_timer = QLabel("⏱️ 00:00")
        self.lbl_timer.setStyleSheet(
            "font-weight: bold; font-family: 'Consolas', monospace; color: #38bdf8; "
            "background-color: #202024; border: 1px solid #2e2e33; border-radius: 4px; padding: 2px 8px;"
        )
        self.lbl_timer.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.lbl_timer.setVisible(False)

        status_bar_layout.addWidget(self.lbl_status, stretch=0)
        status_bar_layout.addWidget(self.progress_bar, stretch=1)
        status_bar_layout.addWidget(self.lbl_timer, stretch=0)

        right_layout.addLayout(status_bar_layout)

        main_h_layout.addWidget(right_panel)

    @Slot(int)
    def _on_mode_changed(self, row: int):
        item = self.sidebar_modes.item(row)
        if item:
            self.current_mode = item.data(Qt.ItemDataRole.UserRole)
            # Mostra o slider apenas em modos visuais
            self.container_slider.setVisible(self.current_mode in (ScanMode.IMAGES, ScanMode.VIDEOS))
            self.results_widget.set_results([])
            self.lbl_status.setText(f"Modo alterado para: {item.text()}.")

    @staticmethod
    def _format_duration(seconds: float) -> str:
        s = int(seconds)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def _clear_cache_prompt(self):
        cache = CacheManager()
        stats = cache.get_stats()
        total_items = stats["exact_count"] + stats["images_count"] + stats["videos_count"]
        
        msg = (
            f"O cache persistente acelera escaneamentos repetidos.\n\n"
            f"Estatísticas atuais:\n"
            f"• Hashes Exatos salvos: {stats['exact_count']}\n"
            f"• Fotos em cache: {stats['images_count']}\n"
            f"• Vídeos em cache: {stats['videos_count']}\n"
            f"• Tamanho em disco: {stats['db_size_mb']} MB\n\n"
            f"Deseja esvaziar o cache para forçar um recálculo completo do zero?"
        )
        confirm = QMessageBox.question(
            self,
            "Limpar Cache Persistente",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if confirm == QMessageBox.StandardButton.Yes:
            cache.clear()
            self.lbl_status.setText("Cache persistente esvaziado com sucesso.")
            QMessageBox.information(self, "Cache Limpo", "Todas as assinaturas em cache foram removidas.")

    def _start_scan(self):
        scan_paths = self.folder_panel.get_scan_paths()
        if not scan_paths:
            QMessageBox.warning(
                self,
                "Nenhuma Pasta Selecionada",
                "Por favor, adicione pelo menos uma pasta na lista de escaneamento antes de iniciar."
            )
            return

        excluded_paths = self.folder_panel.get_excluded_paths()
        threshold = self.slider_sim.value() / 100.0

        self.btn_scan.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # Indeterminado no início
        self.lbl_status.setText("Iniciando escaneamento...")

        # Inicializa o temporizador
        self.start_time = time.time()
        self.latest_current = 0
        self.latest_total = 0
        self.lbl_timer.setText("⏱️ 00:00")
        self.lbl_timer.setVisible(True)
        self.ui_timer.start(250)

        self.worker = ScanWorker(
            scan_paths=scan_paths,
            excluded_paths=excluded_paths,
            mode=self.current_mode,
            similarity_threshold=threshold
        )
        self.worker.progress_status.connect(self._on_worker_progress)
        self.worker.scan_finished.connect(self._on_scan_finished)
        self.worker.scan_error.connect(self._on_scan_error)
        self.worker.finished.connect(self._finalize_scan_ui)
        self.worker.start()

    def _stop_scan(self):
        if self.worker and self.worker.isRunning():
            self.lbl_status.setText("Parando escaneamento...")
            self.btn_stop.setEnabled(False)
            self.worker.requestInterruption()

    def closeEvent(self, event):
        """Garante encerramento imediato de todos os processos e threads em segundo plano ao fechar a janela."""
        self.ui_timer.stop()
        if self.worker and self.worker.isRunning():
            self.lbl_status.setText("Encerrando...")
            self.worker.requestInterruption()
            if not self.worker.wait(400):
                self.worker.terminate()
                self.worker.wait(200)

        event.accept()
        app = QApplication.instance()
        if app:
            app.quit()

    @Slot()
    def _update_telemetry_tick(self):
        """Atualização autônoma de telemetria a cada 250ms, desacoplada do I/O de disco."""
        if not self.start_time:
            return
        elapsed = time.time() - self.start_time
        elapsed_str = self._format_duration(elapsed)

        if self.latest_total > 0 and self.latest_current > 1 and elapsed > 0.5:
            rate = self.latest_current / max(elapsed, 0.001)
            remaining = max(0, self.latest_total - self.latest_current)
            eta_sec = remaining / max(rate, 0.001)
            eta_str = self._format_duration(eta_sec)
            self.lbl_timer.setText(f"⏱️ {elapsed_str} | ETA: ~{eta_str} ({rate:.1f} it/s)")
        else:
            self.lbl_timer.setText(f"⏱️ {elapsed_str}")

    @Slot(str, int, int)
    def _on_worker_progress(self, msg: str, current: int, total: int):
        self.lbl_status.setText(msg)
        self.latest_current = current
        self.latest_total = total

        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(current)
        else:
            self.progress_bar.setRange(0, 0)

        self._update_telemetry_tick()

    @Slot(list)
    def _on_scan_finished(self, groups: List[DuplicateGroup]):
        self.ui_timer.stop()
        total_time_str = ""
        if self.start_time:
            total_elapsed = time.time() - self.start_time
            total_time_str = self._format_duration(total_elapsed)
            self.lbl_timer.setText(f"⏱️ Tempo Total: {total_time_str}")

        self.results_widget.set_results(groups)

        total_files = sum(len(g.files) for g in groups)
        total_reclaim = sum(g.reclaimable_bytes for g in groups)
        reclaim_mb = round(total_reclaim / (1024 * 1024), 2)

        time_suffix = f" em {total_time_str}" if total_time_str else ""
        if groups:
            self.lbl_status.setText(
                f"Escaneamento concluído{time_suffix}! Encontrados {len(groups)} grupos "
                f"({total_files} arquivos). Espaço recuperável: {reclaim_mb} MB."
            )
            # Aplica seleção inteligente automática (protegendo pastas de referência)
            self.results_widget.select_smart_duplicates()
        else:
            self.lbl_status.setText(f"Escaneamento concluído{time_suffix}. Nenhuma duplicata encontrada com os filtros atuais.")

    @Slot(str)
    def _on_scan_error(self, err_msg: str):
        self._finalize_scan_ui()
        self.lbl_status.setText("Erro no escaneamento.")
        QMessageBox.critical(self, "Erro de Execução", err_msg)

    def _finalize_scan_ui(self):
        self.ui_timer.stop()
        self.btn_scan.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress_bar.setVisible(False)

    def _delete_selected_to_trash(self):
        selected = self.results_widget.get_selected_files()
        if not selected:
            QMessageBox.information(
                self,
                "Nenhum Arquivo Selecionado",
                "Marque pelo menos um arquivo na lista para mover para a Lixeira."
            )
            return

        confirm = QMessageBox.question(
            self,
            "Confirmar Envio para a Lixeira",
            f"Deseja mover {len(selected)} arquivo(s) selecionado(s) para a Lixeira do Windows?\n\n"
            "Arquivos em pastas de referência não são excluídos.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if confirm == QMessageBox.StandardButton.Yes:
            deleted_set = set()
            errors = []

            for path in selected:
                try:
                    send2trash.send2trash(str(path))
                    deleted_set.add(path)
                except Exception as e:
                    errors.append(f"{path.name}: {str(e)}")

            self.results_widget.remove_deleted_files(deleted_set)

            if errors:
                QMessageBox.warning(
                    self,
                    "Concluído com Avisos",
                    f"{len(deleted_set)} arquivos movidos para a Lixeira.\n"
                    f"{len(errors)} erros:\n" + "\n".join(errors[:5])
                )
            else:
                QMessageBox.information(
                    self,
                    "Sucesso",
                    f"{len(deleted_set)} arquivo(s) foram movidos com segurança para a Lixeira do Windows!"
                )
                self.lbl_status.setText(f"{len(deleted_set)} arquivo(s) enviados para a Lixeira.")

    def _move_selected_files(self):
        """Move os arquivos selecionados para outra pasta informada pelo usuário."""
        selected = self.results_widget.get_selected_files()
        if not selected:
            QMessageBox.information(
                self,
                "Nenhum Arquivo Selecionado",
                "Marque pelo menos um arquivo na lista para mover."
            )
            return

        dest_dir = QFileDialog.getExistingDirectory(self, "Selecionar Pasta de Destino para Mover Arquivos")
        if not dest_dir:
            return

        dest_path = Path(dest_dir)
        moved_set = set()
        errors = []

        for p in selected:
            try:
                target = dest_path / p.name
                if target.exists():
                    stem = p.stem
                    suffix = p.suffix
                    idx = 1
                    while target.exists():
                        target = dest_path / f"{stem}_dup{idx}{suffix}"
                        idx += 1
                shutil.move(str(p), str(target))
                moved_set.add(p)
            except Exception as e:
                errors.append(f"{p.name}: {str(e)}")

        self.results_widget.remove_deleted_files(moved_set)

        if errors:
            QMessageBox.warning(
                self,
                "Concluído com Avisos",
                f"{len(moved_set)} arquivo(s) movidos para '{dest_path.name}'.\n"
                f"{len(errors)} erros:\n" + "\n".join(errors[:5])
            )
        else:
            QMessageBox.information(
                self,
                "Sucesso",
                f"{len(moved_set)} arquivo(s) foram movidos com segurança para:\n{dest_path}"
            )
            self.lbl_status.setText(f"{len(moved_set)} arquivo(s) movidos para {dest_path.name}.")

    def _delete_selected_permanently(self):
        """Exclui permanentemente os arquivos selecionados do disco com confirmação rígida."""
        selected = self.results_widget.get_selected_files()
        if not selected:
            QMessageBox.information(
                self,
                "Nenhum Arquivo Selecionado",
                "Marque pelo menos um arquivo na lista para excluir."
            )
            return

        confirm = QMessageBox.warning(
            self,
            "Confirmar Exclusão Permanente",
            f"ATENÇÃO: Deseja EXCLUIR DEFINITIVAMENTE {len(selected)} arquivo(s) selecionado(s)?\n\n"
            "Esta ação NÃO envia para a lixeira e NÃO pode ser desfeita!\n"
            "Arquivos em pastas de referência estão 100% protegidos e não serão tocados.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if confirm == QMessageBox.StandardButton.Yes:
            deleted_set = set()
            errors = []

            for path in selected:
                try:
                    path.unlink(missing_ok=True)
                    deleted_set.add(path)
                except Exception as e:
                    errors.append(f"{path.name}: {str(e)}")

            self.results_widget.remove_deleted_files(deleted_set)

            if errors:
                QMessageBox.warning(
                    self,
                    "Concluído com Avisos",
                    f"{len(deleted_set)} arquivos excluídos permanentemente.\n"
                    f"{len(errors)} erros:\n" + "\n".join(errors[:5])
                )
            else:
                QMessageBox.information(
                    self,
                    "Sucesso",
                    f"{len(deleted_set)} arquivo(s) foram excluídos permanentemente do disco."
                )
                self.lbl_status.setText(f"{len(deleted_set)} arquivo(s) excluídos permanentemente.")

    def _export_results(self):
        """Exporta os resultados de duplicatas para um arquivo de texto."""
        if not self.results_widget.groups:
            QMessageBox.information(
                self,
                "Nenhum Resultado",
                "Não há resultados de escaneamento para salvar."
            )
            return

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Salvar Lista de Duplicatas",
            str(Path.home() / "Desktop" / "duplicatas_dupeclean.txt"),
            "Arquivo de Texto (*.txt);;Todos os Arquivos (*.*)"
        )
        if not file_path:
            return

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("=== RELATÓRIO DE DUPLICATAS - DUPECLEAN ===\n")
                f.write(f"Gerado em: {time.strftime('%d/%m/%Y %H:%M:%S')}\n")
                f.write(f"Modo: {self.current_mode.value.upper()}\n")
                f.write(f"Total de Grupos: {len(self.results_widget.groups)}\n\n")

                for grp in self.results_widget.groups:
                    f.write("--------------------------------------------------\n")
                    f.write(f"Grupo #{grp.group_id} | Similaridade: {grp.similarity * 100:.1f}%\n")
                    for fl in grp.files:
                        ref_tag = " [REFERÊNCIA]" if fl.is_reference else ""
                        size_mb = fl.size / (1024 * 1024)
                        f.write(f"  • {fl.name} ({size_mb:.2f} MB){ref_tag}\n    {fl.path}\n")
                    f.write("\n")

            QMessageBox.information(
                self,
                "Sucesso",
                f"Relatório exportado com sucesso para:\n{file_path}"
            )
            self.lbl_status.setText(f"Relatório salvo em: {Path(file_path).name}")
        except Exception as e:
            QMessageBox.critical(self, "Erro ao Salvar", f"Não foi possível salvar o arquivo:\n{str(e)}")
