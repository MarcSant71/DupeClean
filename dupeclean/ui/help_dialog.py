"""
DupeClean - Help, Technical Architecture and Documentation Dialog.
Displays comprehensive product overview, multi-stage engineering pipeline,
hardware allocation details, and keyboard shortcuts without developer/installation fluff.
"""

from typing import Dict, Any, Optional
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QWidget,
    QFrame,
    QTextBrowser,
)

import dupeclean


class ZoomableBrowser(QTextBrowser):
    """Visualizador rich-text com suporte a zoom proporcional via Ctrl + Scroll e Ctrl +/-."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._zoom_level = 0
        self._body_html = ""
        self.setOpenExternalLinks(True)
        self.setFrameShape(QFrame.Shape.NoFrame)

    def set_styled_html(self, body_html: str) -> None:
        """Define o HTML base e aplica a escala de zoom atual a todos os estilos CSS."""
        self._body_html = body_html
        self._apply_zoom()

    def _apply_zoom(self) -> None:
        """Re-renderiza o HTML escalando proporcionalmente fontes, tabelas e espaçamentos."""
        scale = 1.0 + 0.1 * self._zoom_level
        sb = self.verticalScrollBar()
        prev_max = sb.maximum()
        scroll_ratio = (sb.value() / prev_max) if prev_max > 0 else 0.0

        body_px = round(13.5 * scale, 1)
        h2_px = round(17.0 * scale, 1)
        h3_px = round(14.5 * scale, 1)
        code_px = round(12.5 * scale, 1)
        badge_px = round(11.0 * scale, 1)
        table_px = round(12.5 * scale, 1)

        base_font = self.font()
        base_font.setPointSizeF(round(9.0 * scale, 1))
        self.setFont(base_font)

        self.setHtml(f"""
        <html>
        <head>
            <style>
                body {{
                    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
                    font-size: {body_px}px;
                    line-height: 1.6;
                    color: #d4d4d8;
                    background-color: transparent;
                    margin: 14px 22px;
                }}
                h2 {{
                    color: #38bdf8;
                    font-size: {h2_px}px;
                    margin-top: 0px;
                    margin-bottom: 12px;
                    border-bottom: 1px solid #2e2e33;
                    padding-bottom: 6px;
                }}
                h3 {{
                    color: #38bdf8;
                    font-size: {h3_px}px;
                    margin-top: 18px;
                    margin-bottom: 8px;
                }}
                p {{
                    margin-top: 0px;
                    margin-bottom: 12px;
                }}
                ul, ol {{
                    margin-top: 4px;
                    margin-bottom: 14px;
                    padding-left: 22px;
                }}
                li {{
                    margin-bottom: 6px;
                }}
                code {{
                    font-family: 'Consolas', 'Cascadia Code', monospace;
                    background-color: #202024;
                    color: #38bdf8;
                    border: 1px solid #2e2e33;
                    border-radius: 3px;
                    padding: 2px 5px;
                    font-size: {code_px}px;
                }}
                .badge {{
                    display: inline-block;
                    padding: 2px 7px;
                    border-radius: 4px;
                    font-size: {badge_px}px;
                    font-weight: bold;
                }}
                .badge-cyan {{
                    background-color: #0c4a6e;
                    color: #38bdf8;
                    border: 1px solid #0284c7;
                }}
                .badge-green {{
                    background-color: #14532d;
                    color: #4ade80;
                    border: 1px solid #16a34a;
                }}
                .card {{
                    background-color: transparent;
                    border: none;
                    padding: 0px;
                    margin-bottom: 18px;
                }}
                .callout {{
                    background-color: transparent;
                    border-left: 3px solid #38bdf8;
                    padding: 4px 14px;
                    margin: 14px 0px;
                }}
                table {{
                    width: 100%;
                    border-collapse: collapse;
                    margin-top: 10px;
                    margin-bottom: 14px;
                    font-size: {table_px}px;
                }}
                th {{
                    background-color: #202024;
                    color: #38bdf8;
                    text-align: left;
                    padding: 8px 12px;
                    border: 1px solid #2e2e33;
                }}
                td {{
                    padding: 7px 12px;
                    border: 1px solid #27272a;
                    color: #d4d4d8;
                }}
                tr:nth-child(even) {{
                    background-color: #1b1b1e;
                }}
            </style>
        </head>
        <body>
            {self._body_html}
        </body>
        </html>
        """)

        if scroll_ratio > 0 and sb.maximum() > 0:
            sb.setValue(int(scroll_ratio * sb.maximum()))

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self.zoom_in()
            elif delta < 0:
                self.zoom_out()
            event.accept()
        else:
            super().wheelEvent(event)

    def keyPressEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
                self.zoom_in()
                event.accept()
                return
            elif event.key() == Qt.Key.Key_Minus:
                self.zoom_out()
                event.accept()
                return
            elif event.key() == Qt.Key.Key_0:
                self.reset_zoom()
                event.accept()
                return
        super().keyPressEvent(event)

    def zoom_in(self):
        if self._zoom_level < 8:
            self._zoom_level += 1
            if self._body_html:
                self._apply_zoom()
            else:
                self.zoomIn(1)

    def zoom_out(self):
        if self._zoom_level > -4:
            self._zoom_level -= 1
            if self._body_html:
                self._apply_zoom()
            else:
                self.zoomOut(1)

    def reset_zoom(self):
        if self._zoom_level != 0:
            prev = self._zoom_level
            self._zoom_level = 0
            if self._body_html:
                self._apply_zoom()
            elif prev > 0:
                self.zoomOut(prev)
            else:
                self.zoomIn(-prev)


class HelpDialog(QDialog):
    """Diálogo completo de ajuda, arquitetura técnica e atalhos do DupeClean."""

    def __init__(self, gpu_info: Optional[Dict[str, Any]] = None, parent=None):
        super().__init__(parent)
        self.gpu_info = gpu_info or {}
        self.setWindowTitle(f"DupeClean v{dupeclean.__version__} - Help & Technical Guide")

        # Habilita botões nativos de Maximizar, Minimizar e Restaurar do Windows
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
        )

        # Dimensionamento Proporcional Dinâmico (~82% da largura e ~86% da altura)
        if parent:
            pw = parent.width()
            ph = parent.height()
        else:
            screen = QApplication.primaryScreen()
            s_geo = screen.availableGeometry() if screen else None
            pw = s_geo.width() if s_geo else 1280
            ph = s_geo.height() if s_geo else 800

        w = max(880, int(pw * 0.82))
        h = max(620, int(ph * 0.86))
        self.resize(w, h)
        self.setMinimumSize(780, 520)

        self._init_ui()

    def _init_ui(self):
        self.setStyleSheet("""
            QDialog {
                background-color: #151518;
                color: #f4f4f5;
            }
            QTabWidget::pane {
                border: 1px solid #3f3f46;
                border-radius: 6px;
                border-top-left-radius: 0px;
                background-color: #18181b;
                padding: 2px;
                top: -1px;
            }
            QTabBar::tab {
                background-color: #202024;
                color: #a1a1aa;
                border: 1px solid #3f3f46;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                padding: 8px 16px;
                margin-right: 3px;
                font-weight: 500;
                font-size: 13px;
            }
            QTabBar::tab:selected {
                background-color: #18181b;
                color: #38bdf8;
                border-color: #3f3f46;
                border-bottom: 2px solid #38bdf8;
                font-weight: bold;
            }
            QTabBar::tab:hover:!selected {
                background-color: #27272a;
                color: #f4f4f5;
                border-color: #52525b;
            }
            QTextBrowser {
                border: none;
                background-color: transparent;
            }
            QScrollArea {
                border: none;
                background-color: transparent;
            }
            QScrollBar:vertical {
                border: none;
                background: transparent;
                width: 8px;
                margin: 8px 6px 8px 0px;
            }
            QScrollBar::handle:vertical {
                background-color: #3f3f46;
                min-height: 25px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical:hover {
                background-color: #52525b;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                border: none;
                background: none;
                height: 0px;
                width: 0px;
            }
            QScrollBar::up-arrow:vertical, QScrollBar::down-arrow:vertical {
                border: none;
                background: none;
                width: 0px;
                height: 0px;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                border: none;
                background: none;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        # -------------------------------------------------------------
        # 1. Header (Logo, Version & GPU Badge)
        # -------------------------------------------------------------
        header = QHBoxLayout()
        header.setSpacing(10)

        lbl_logo = QLabel("⚡")
        lbl_logo.setStyleSheet("font-size: 28px;")
        header.addWidget(lbl_logo)

        title_vbox = QVBoxLayout()
        title_vbox.setSpacing(2)

        lbl_title = QLabel("DupeClean")
        lbl_title.setStyleSheet("font-size: 22px; font-weight: bold; color: #38bdf8; letter-spacing: 0.5px;")

        lbl_desc = QLabel("GPU-Accelerated Duplicate File, Image & Video Finder")
        lbl_desc.setStyleSheet("font-size: 12px; color: #a1a1aa;")

        title_vbox.addWidget(lbl_title)
        title_vbox.addWidget(lbl_desc)
        header.addLayout(title_vbox)

        header.addStretch()

        # Version Badge
        lbl_ver = QLabel(f"v{dupeclean.__version__}")
        lbl_ver.setStyleSheet(
            "background-color: #202024; color: #38bdf8; border: 1px solid #3f3f46; "
            "border-radius: 12px; padding: 4px 12px; font-weight: bold; font-size: 12px;"
        )
        header.addWidget(lbl_ver)

        layout.addLayout(header)

        # -------------------------------------------------------------
        # 2. Main Tabs
        # -------------------------------------------------------------
        self.tabs = QTabWidget(self)
        self.tabs.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tabs.addTab(self._create_overview_tab(), "📘 Product Overview")
        self.tabs.addTab(self._create_pipeline_tab(), "🔬 Technical Pipeline & Stages")
        self.tabs.addTab(self._create_modes_tab(), "🎯 Scan Modes")
        self.tabs.addTab(self._create_shortcuts_tab(), "⌨️ Shortcuts & Tips")
        self.tabs.addTab(self._create_contact_tab(), "📬 Contact Information")

        layout.addWidget(self.tabs)

        # -------------------------------------------------------------
        # 3. Footer (GitHub link, Close button)
        # -------------------------------------------------------------
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)

        btn_repo = QPushButton("🌐 View on GitHub")
        btn_repo.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_repo.setStyleSheet(
            "QPushButton { background-color: #202024; border: 1px solid #3f3f46; border-radius: 5px; "
            "color: #e4e4e7; padding: 7px 16px; font-size: 12px; font-weight: 500; } "
            "QPushButton:hover { background-color: #27272a; border-color: #38bdf8; color: #38bdf8; }"
        )
        btn_repo.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://github.com/MarcSant71/DupeClean")))
        footer.addWidget(btn_repo)

        footer.addStretch()

        btn_close = QPushButton("Close")
        btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_close.setStyleSheet(
            "QPushButton { background-color: #0284c7; border: 1px solid #0284c7; border-radius: 5px; "
            "color: #ffffff; padding: 7px 22px; font-weight: 600; font-size: 12px; } "
            "QPushButton:hover { background-color: #0369a1; border-color: #0369a1; }"
        )
        btn_close.clicked.connect(self.accept)
        footer.addWidget(btn_close)

        layout.addLayout(footer)

        # Atalhos globais do diálogo para funcionar independente do widget com foco (abas, botões ou texto)
        for seq in ("Ctrl++", "Ctrl+="):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(lambda: self._apply_shortcut_zoom(1))

        sc_out = QShortcut(QKeySequence("Ctrl+-"), self)
        sc_out.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc_out.activated.connect(lambda: self._apply_shortcut_zoom(-1))

        sc_reset = QShortcut(QKeySequence("Ctrl+0"), self)
        sc_reset.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        sc_reset.activated.connect(lambda: self._apply_shortcut_zoom(0))

    def _apply_shortcut_zoom(self, direction: int) -> None:
        """Aplica zoom (+1/-1/0) à aba ativa do leitor independente do foco atual."""
        browser = self.tabs.currentWidget()
        if not isinstance(browser, ZoomableBrowser):
            return
        if direction > 0:
            browser.zoom_in()
        elif direction < 0:
            browser.zoom_out()
        else:
            browser.reset_zoom()

    def open_tab(self, index: int):
        """Abre o diálogo na aba especificada."""
        if 0 <= index < self.tabs.count():
            self.tabs.setCurrentIndex(index)
        self.exec()

    # -----------------------------------------------------------------
    # Tab Builders with Rich Text HTML
    # -----------------------------------------------------------------
    def _create_browser(self, html: str) -> QWidget:
        """Cria um visualizador rich-text com suporte a rolagem suave e zoom."""
        browser = ZoomableBrowser()
        browser.setFrameShape(QFrame.Shape.NoFrame)
        browser.viewport().setAutoFillBackground(False)
        browser.setStyleSheet("QTextBrowser { border: none; background-color: transparent; }")
        browser.set_styled_html(html)
        return browser

    def _create_overview_tab(self) -> QWidget:
        html = """
        <h2>⚡ Why DupeClean? (Superior Perceptual Quality + GPU/CPU Flexibility)</h2>
        <p>
            Most traditional file deduplicators (such as <i>Czkawka</i>, <i>dupeGuru</i>, or <i>Duplicate Cleaner</i>)
            compress images into coarse binary perceptual hashes compared via bit-level Hamming distance &mdash; trading away 
            visual nuance and missing resized, cropped, or re-compressed duplicates.
        </p>
        <p>
            <b>DupeClean compares continuous 2D DCT embeddings (768-D for images / 3840-D for videos) via dense 
            all-against-all (<i>N &times; N</i>) matrix multiplication, delivering superior visual matching quality.</b> 
            It runs out of the box on standard CPUs (universal FP32 fallback) so every machine can use all scan modes, 
            while <b>an NVIDIA RTX GPU is recommended for superior performance</b> on massive photo and video collections.
        </p>
        
        <div class="card">
            <h3 style="margin-top:0px; color:#38bdf8;">Engineering Comparison</h3>
            <table>
                <tr>
                    <th>Feature</th>
                    <th>🐢 Legacy Tools (Binary pHash)</th>
                    <th>⚡ DupeClean (Dense Embedding Engine)</th>
                </tr>
                <tr>
                    <td><b>Compute Engine</b></td>
                    <td>CPU software loops only</td>
                    <td><b>NVIDIA RTX Tensor Cores (CUDA FP16)</b> + <b>Universal CPU Fallback (FP32)</b></td>
                </tr>
                <tr>
                    <td><b>Similarity Intelligence</b></td>
                    <td>Coarse binary pHash / low-res pixel gradients</td>
                    <td><b>Continuous 2D DCT Embeddings (768-D / 3840-D)</b> with spectral whitening</td>
                </tr>
                <tr>
                    <td><b>Throughput & Speed</b></td>
                    <td>~20&ndash;50 items/second</td>
                    <td><b>600+ images/s</b> &amp; <b>150+ videos/s</b> on RTX GPUs (RTX recommended for best speed)</td>
                </tr>
                <tr>
                    <td><b>Duplicate Grouping</b></td>
                    <td>Pairwise Hamming heuristics / bucketed hashing</td>
                    <td><b>Dense All-Against-All (<i>N &times; N</i>) Cosine Matrix Multiplication</b></td>
                </tr>
                <tr>
                    <td><b>False-Positive Defense</b></td>
                    <td>Vulnerable to transitive similarity chaining</td>
                    <td><b>Strict Pairwise Mutual Similarity</b> (100% coherence)</td>
                </tr>
                <tr>
                    <td><b>Storage Engine</b></td>
                    <td>Fragile in-memory caches</td>
                    <td><b>SQLite WAL Database</b> (Crash-safe, instant rescans across CPU &amp; GPU)</td>
                </tr>
            </table>
        </div>

        <h2>🛡️ Safety First & Reference Folder Protection</h2>
        <ul>
            <li>
                <b>Non-Destructive Operations:</b> File deletion defaults to the <b>Windows Recycle Bin</b> 
                (<code>Send2Trash</code>), allowing instant recovery of accidentally removed files.
            </li>
            <li>
                <b>Protected Reference Folders (<code>Ref</code>):</b> Designate any master backup folder as 
                <b>Reference</b>. DupeClean compares duplicate files against it, while ensuring files inside 
                reference folders are strictly protected and <b>never marked for deletion</b>.
            </li>
            <li>
                <b>Zero-Churn Hash Cache:</b> Damaged or unreadable files are automatically skipped and cached 
                so subsequent scans never freeze or waste time on known unreadable media.
            </li>
        </ul>
        """
        return self._create_browser(html)

    def _create_pipeline_tab(self) -> QWidget:
        html = """
        <h2>🔬 Multi-Stage Pipeline Architecture</h2>
        <p>
            DupeClean achieves superior accuracy and throughput by strictly assigning each task to the processor 
            architecture best equipped for it: <b>CPU for parallel micro-sampling and sequential I/O; NVIDIA RTX Tensor Cores 
            (with automatic FP32 CPU fallback when no RTX GPU is present) for mass dense <i>N &times; N</i> matrix matching.</b>
        </p>

        <div class="card">
            <h3>Stage 1/3: Scanning Folders (Discovery & Metadata Enumeration)</h3>
            <p>
                <b>What happens:</b> Recursively crawls target folders, indexes paths, isolates user-defined 
                <b>Reference Folders</b> (<code>Ref</code>), and extracts metadata (file size, timestamps, container extensions).
            </p>
            <p>
                <b>Optimization:</b> Discards zero-byte files, filters out excluded paths, and indexes hundreds of thousands 
                of files in seconds using low-overhead OS system calls. Files with unique sizes are instantly eliminated.
            </p>
        </div>

        <div class="card">
            <h3>Stage 2/3: Calculating Partial Hashes (Fast Sieve)</h3>
            <p>
                <b>What happens:</b> Small/medium files (<b>&le; 256 KB</b>) are hashed in a single read pass without ever needing Stage 3. 
                Larger files sharing identical sizes read only <b>8 KB (4 KB head + 4 KB tail)</b>, consulting the persistent local SQLite WAL cache.
            </p>
            <p>
                <b>Optimization:</b> Eliminates double file-open overhead on hundreds of thousands of small photos/documents while disqualifying 
                mismatched multi-gigabyte files in 1 millisecond (filtering over <b>90% of non-duplicate candidates</b> before full reads).
            </p>
        </div>

        <div class="card">
            <h3>Stage 3/3: Calculating Full Hashes (Deep Verification)</h3>
            <p>
                <b>What happens:</b> For larger candidates (&gt; 256 KB) passing the 8 KB sieve, DupeClean reads file data in 
                <b>512 KB parallel streaming blocks</b> and computes high-speed <code>xxHash64</code> digests.
            </p>
            <p>
                <b>Hardware Scaling:</b> Maximizes sequential storage read throughput (up to NVMe limits) using pre-allocated 
                <code>512 KB</code> streaming buffers (<code>buffering=0</code> with <code>readinto</code>, avoiding Python buffer overhead and chunk allocations) across <b>4 to 32 concurrent worker threads</b> scaled dynamically to your CPU core count.
            </p>
        </div>

        <div class="card">
            <h3>Result Phase: Loading Results & Group Mounting</h3>
            <p>
                <b>What happens:</b> Instantaneous $O(1)$ in-memory hash table grouping. Matching hashes are bundled into 
                duplicate clusters and mounted into the interactive results grid with zero UI freezes.
            </p>
        </div>

        <h2>💡 Hardware Telemetry FAQ</h2>
        <div class="callout">
            <b>Why does Duplicate Files have two hashing stages (<i>Partial Hashes</i> vs. <i>Full Hashes</i>)?</b><br>
            Guaranteeing two files are 100% bit-for-bit identical mathematically requires reading every byte—if a single byte differs in the middle of a 2 GB file, only a full-file hash can catch it.<br>
            &bull; <b>Partial Hashes (Stage 2):</b> Files <b>&le; 256 KB</b> are hashed completely in a single read pass (avoiding double file-open overhead). Larger files (<b>&gt; 256 KB</b>) read only <b>8 KB (4 KB head + 4 KB tail)</b> to eliminate 90%+ of size-colliding non-duplicates without touching full file bodies.<br>
            &bull; <b>Full Hashes (Stage 3):</b> Only larger files (&gt; 256 KB) whose 8 KB partial hashes match are streamed in full via <code>xxHash64</code> for absolute verification.
        </div>
        <div class="callout">
            <b>Why is my Disk read speed at 100% (~1–3+ GB/s) during Duplicate Files Stage 3?</b><br>
            To guarantee with 100% mathematical certainty that two files are identical bit-for-bit, their raw bytes must be streamed from physical storage into memory. DupeClean uses multi-threaded 512 KB streaming buffers (<code>buffering=0</code> + <code>readinto</code>) to keep your drive reading at full sequential speed so this phase finishes as fast as possible.
        </div>
        <div class="callout">
            <b>Why does my RTX GPU stay near 0% in Task Manager during Stage 2 & 3 of Image/Video scans?</b><br>
            During Stage 2, CPU workers decode images and extract video keyframes from disk. Once a batch is ready (<b>256&ndash;1,024 images</b> depending on VRAM, or <b>128 videos</b>), the RTX GPU processes it in <b>fractions of a millisecond</b> (&lt;0.5 ms) and immediately waits for the next disk/CPU batch. Because the GPU compute finishes so quickly, Windows Task Manager's 1-second average sampler shows near 0% until the all-to-all mass matrix multiplication in Stage 3 begins.
        </div>
        <div class="callout">
            <b>How does DupeClean adapt to different CPUs (e.g., 4-core laptops vs. Core i9 / Ryzen 9)?</b><br>
            Worker allocation scales dynamically from <code>os.cpu_count()</code>: Exact Hashing and Image Decoding allocate between <b>4 and 32 threads</b> matching your CPU threads, while Video Keyframe Extraction caps parallel sub-processes at <b>50% of logical cores (4 to 16 processes)</b> so Windows, background audio, and the UI remain completely responsive.
        </div>
        <div class="callout">
            <b>Can DupeClean scan photos and videos without an NVIDIA RTX GPU?</b><br>
            Yes! DupeClean automatically runs in universal <b>CPU Mode (NO GPU)</b> using AVX-accelerated FP32 PyTorch tensors, preserving the exact same continuous 768-D / 3840-D DCT embeddings and SQLite cache format. However, <b>an NVIDIA RTX GPU (Compute Capability 8.6+) is strongly recommended for superior performance</b> on large libraries.
        </div>
        <div class="callout">
            <b>Does it automatically optimize for different RTX GPUs (RTX 3070 vs. 3090 vs. 5090)?</b><br>
            Yes. When an RTX GPU is active, the engine inspects VRAM capacity and free headroom in real time via <code>torch.cuda.mem_get_info()</code>: on 8 GB cards (e.g., RTX 3070/4060), calculation blocks are capped at <b>256 MB</b> to prevent Windows WDDM paging overhead; on 24 GB / 32 GB GPUs (e.g., RTX 3090/4090/5090), it automatically scales up to <b>2.0 GB blocks</b> and 1,024-item batches to fully saturate memory bandwidth and Tensor Cores.
        </div>
        """
        return self._create_browser(html)

    def _create_modes_tab(self) -> QWidget:
        html = """
        <h2>🎯 Scan Modes & Algorithms</h2>
        <p>
            DupeClean provides three specialized scanning modes tailored to different types of media and deduplication requirements:
        </p>

        <div class="card">
            <h3 style="margin-top:0px; color:#38bdf8;">📁 Duplicate Files (Exact Matching)</h3>
            <ul>
                <li><b>Target:</b> Documents, archives, binaries, installers, backups, and identical media files.</li>
                <li><b>Algorithm:</b> Multi-stage architecture combining a 256 KB single-pass fast-path and 8 KB (head + tail) partial hashes with full <code>xxHash64</code> streaming digests.</li>
                <li><b>Guarantee:</b> 100% bit-for-bit identical duplicate guarantee. Zero false positives.</li>
                <li><b>Speed:</b> Millions of files evaluated in minutes with persistent SQLite caching.</li>
            </ul>
        </div>

        <div class="card">
            <h3 style="margin-top:0px; color:#38bdf8;">🖼️ Similar Images (Visual Perceptual Matching)</h3>
            <ul>
                <li><b>Target:</b> Photos, burst shots, resized images, recompressed JPEGs, wallpapers, and converted graphic formats.</li>
                <li><b>Algorithm:</b> Continuous <b>768-D 2D DCT embeddings</b> compared via dense all-against-all (<i>N &times; N</i>) matrix multiplication (PyTorch CUDA FP16 or CPU FP32 fallback) for superior visual precision.</li>
                <li><b>Similarity Slider:</b> Configurable threshold from <b>70% to 100%</b>. Detects identical photos even if resized, color-graded, or re-saved as PNG, JPEG, or WEBP.</li>
                <li><b>Hardware Note:</b> Runs on CPU as a universal fallback; an <b>NVIDIA RTX GPU is recommended for superior performance</b> (600+ photos/second).</li>
            </ul>
        </div>

        <div class="card">
            <h3 style="margin-top:0px; color:#38bdf8;">🎬 Similar Videos (Temporal Keyframe Matching)</h3>
            <ul>
                <li><b>Target:</b> Duplicate video clips, re-encoded movies, surveillance footage, and alternative resolutions (4K vs 1080p).</li>
                <li><b>Algorithm:</b> Fast keyframe seek pinpoints 5 distinct temporal timeline frames per video without full decoding, producing continuous <b>3,840-D embeddings</b> matched via dense <i>N &times; N</i> similarity matrices.</li>
                <li><b>Mutual Similarity Protection:</b> Guarantees that every video within a cluster directly matches all other videos in that cluster (eliminates false-positive chaining).</li>
                <li><b>Surveillance & CCTV Ready:</b> Fully supports raw DVR containers (such as Dahua/Hikvision <code>.dav</code> clips) and normalized infrared night-vision footage.</li>
            </ul>
        </div>
        """
        return self._create_browser(html)

    def _create_shortcuts_tab(self) -> QWidget:
        html = """
        <h2>⌨️ Shortcuts & Interactive Ergonomics</h2>
        <p>
            DupeClean is designed with the <b>KISS principle</b> (Keep It Simple, Stupid): clean, distraction-free visual design 
            with powerful hidden shortcuts.
        </p>

        <div class="card">
            <h3 style="margin-top:0px; color:#38bdf8;">Interactive Column Sorting (3-State Cycle)</h3>
            <p>
                Double-click any column header (<b>Size, Name, Similarity, Date, Full Path</b>) to cycle through 3 sorting states:
            </p>
            <ol>
                <li><b>First Double-Click:</b> <b>Ascending</b> order (displays ▲).</li>
                <li><b>Second Double-Click:</b> <b>Descending</b> order (displays ▼).</li>
                <li><b>Third Double-Click:</b> <b>Normal / Unsorted</b> (restores original scan order and clears the symbol).</li>
            </ol>
            <p>
                <i>Note: Checked files and selection metrics are 100% preserved during all sorting operations.</i>
            </p>
        </div>

        <div class="card">
            <h3 style="margin-top:0px; color:#38bdf8;">Keyboard Shortcuts</h3>
            <table>
                <tr>
                    <th>Key</th>
                    <th>Action</th>
                </tr>
                <tr>
                    <td><code>Space</code></td>
                    <td>Toggle media preview panel (instant photo preview or video details).</td>
                </tr>
                <tr>
                    <td><code>Up / Down</code></td>
                    <td>Navigate between duplicate groups and files in the results tree.</td>
                </tr>
                <tr>
                    <td><code>Ctrl + Scroll</code> / <code>Ctrl + / -</code></td>
                    <td>Dynamic zoom in / out on the <b>Results Grid</b>, <b>Media Preview</b> (drag to pan), and <b>Help Reader</b> (<code>Ctrl 0</code> to reset).</td>
                </tr>
                <tr>
                    <td><code>Esc</code></td>
                    <td>Close open dialogs (Help, Confirmation dialogs, Previews).</td>
                </tr>
            </table>
        </div>

        <div class="card">
            <h3 style="margin-top:0px; color:#38bdf8;">Smart Selection Tools (<code>Select</code>)</h3>
            <p>
                Quickly select duplicates across thousands of groups with one click:
            </p>
            <ul>
                <li><b>Select Duplicates (Smart):</b> Automatically selects all duplicates while keeping the primary master file safe.</li>
                <li><b>Select Older / Newer:</b> Retains the newest or oldest version across all clusters.</li>
                <li><b>Select Smaller / Larger:</b> Useful for keeping the highest-resolution version.</li>
                <li><b>Select Longest / Shortest Path:</b> Cleans up deeply nested duplicate folders.</li>
                <li><b>Invert Selection:</b> Inverts checkboxes within each group or across the entire dataset.</li>
            </ul>
        </div>

        <div class="card">
            <h3 style="margin-top:0px; color:#38bdf8;">Drag & Drop Support</h3>
            <p>
                Drag folders directly from <b>Windows File Explorer</b> into the <b>Folders to Scan</b> or <b>Excluded Folders</b> tables.
            </p>
        </div>
        """
        return self._create_browser(html)

    def _create_contact_tab(self) -> QWidget:
        html = f"""
        <h2>📬 Contact & Developer Information</h2>

        <div class="card">
            <h3 style="margin-top:0px; color:#38bdf8;">Dominio Digital</h3>
            <p style="margin-bottom: 6px;">
                <b>Developer:</b> Marcelo Santos &bull; Dominio Digital<br>
                <b>Email:</b> <a style="color: #38bdf8;" href="mailto:contato@dominiodigital.dev.br">contato@dominiodigital.dev.br</a><br>
                <b>Repository:</b> <a style="color: #38bdf8;" href="https://github.com/MarcSant71/DupeClean">https://github.com/MarcSant71/DupeClean</a>
            </p>
        </div>

        <div class="card">
            <h3 style="margin-top:0px; color:#38bdf8;">Application Details</h3>
            <p style="margin-bottom: 6px;">
                <b>Product:</b> DupeClean v{dupeclean.__version__}<br>
                <b>Platform:</b> Windows 10 / 11 x64 (Standalone Portable)<br>
                <b>License:</b> GNU General Public License v3.0 (GPLv3, Free Software)<br>
                <b>Compute Engine:</b> NVIDIA RTX (CUDA FP16, recommended) &amp; Universal CPU Fallback (FP32)
            </p>
        </div>

        <div class="card">
            <h3 style="margin-top:0px; color:#38bdf8;">Feedback & Inquiries</h3>
            <p>
                Have questions, bug reports, feature requests, or commercial inquiries?
            </p>
            <ul>
                <li>Submit an issue on the official <b><a style="color: #38bdf8;" href="https://github.com/MarcSant71/DupeClean/issues">GitHub Issues</a></b> tracker.</li>
                <li>Reach out directly via email at <b><a style="color: #38bdf8;" href="mailto:contato@dominiodigital.dev.br">contato@dominiodigital.dev.br</a></b>.</li>
            </ul>
        </div>
        """
        return self._create_browser(html)
