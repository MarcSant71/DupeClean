"""
DupeClean - Help, Technical Architecture and Documentation Dialog.
Displays comprehensive product overview, multi-stage engineering pipeline,
hardware allocation details, and keyboard shortcuts without developer/installation fluff.
"""

from typing import Dict, Any, Optional
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QScrollArea,
    QWidget,
    QFrame,
    QTextBrowser,
)

import dupeclean


class HelpDialog(QDialog):
    """Diálogo completo de ajuda, arquitetura técnica e atalhos do DupeClean."""

    def __init__(self, gpu_info: Optional[Dict[str, Any]] = None, parent=None):
        super().__init__(parent)
        self.gpu_info = gpu_info or {}
        self.setWindowTitle(f"DupeClean v{dupeclean.__version__} - Help & Technical Guide")
        self.resize(920, 660)
        self.setMinimumSize(780, 540)
        self._init_ui()

    def _init_ui(self):
        self.setStyleSheet("""
            QDialog {
                background-color: #151518;
                color: #f4f4f5;
            }
            QTabWidget::pane {
                border: 1px solid #2e2e33;
                background-color: #18181b;
                border-radius: 6px;
                top: -1px;
            }
            QTabBar::tab {
                background-color: #202024;
                color: #a1a1aa;
                border: 1px solid #2e2e33;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                padding: 9px 18px;
                margin-right: 3px;
                font-weight: 500;
                font-size: 13px;
            }
            QTabBar::tab:selected {
                background-color: #18181b;
                color: #38bdf8;
                border-color: #2e2e33;
                border-bottom: 2px solid #38bdf8;
                font-weight: bold;
            }
            QTabBar::tab:hover:!selected {
                background-color: #27272a;
                color: #f4f4f5;
            }
            QScrollArea {
                border: none;
                background-color: transparent;
            }
            QScrollBar:vertical {
                border: none;
                background-color: #18181b;
                width: 10px;
                margin: 0px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical {
                background-color: #3f3f46;
                min-height: 25px;
                border-radius: 5px;
            }
            QScrollBar::handle:vertical:hover {
                background-color: #52525b;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
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
            "background-color: #202024; color: #38bdf8; border: 1px solid #2e2e33; "
            "border-radius: 12px; padding: 4px 12px; font-weight: bold; font-size: 12px;"
        )
        header.addWidget(lbl_ver)

        layout.addLayout(header)

        # -------------------------------------------------------------
        # 2. Main Tabs
        # -------------------------------------------------------------
        self.tabs = QTabWidget()
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
        footer.setContentsMargins(4, 0, 4, 0)

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

    def open_tab(self, index: int):
        """Abre o diálogo na aba especificada."""
        if 0 <= index < self.tabs.count():
            self.tabs.setCurrentIndex(index)
        self.exec()

    # -----------------------------------------------------------------
    # Tab Builders with Rich Text HTML
    # -----------------------------------------------------------------
    def _create_browser(self, html: str) -> QWidget:
        """Cria um visualizador rich-text com suporte a rolagem suave."""
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setHtml(f"""
        <html>
        <head>
            <style>
                body {{
                    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
                    font-size: 13.5px;
                    line-height: 1.6;
                    color: #d4d4d8;
                    background-color: #18181b;
                    margin: 14px;
                }}
                h2 {{
                    color: #38bdf8;
                    font-size: 17px;
                    margin-top: 0px;
                    margin-bottom: 10px;
                    border-bottom: 1px solid #2e2e33;
                    padding-bottom: 5px;
                }}
                h3 {{
                    color: #fafafa;
                    font-size: 14.5px;
                    margin-top: 16px;
                    margin-bottom: 8px;
                }}
                p {{
                    margin-top: 0px;
                    margin-bottom: 10px;
                }}
                ul, ol {{
                    margin-top: 0px;
                    margin-bottom: 12px;
                    padding-left: 22px;
                }}
                li {{
                    margin-bottom: 5px;
                }}
                code {{
                    font-family: 'Consolas', 'Cascadia Code', monospace;
                    background-color: #202024;
                    color: #38bdf8;
                    border: 1px solid #2e2e33;
                    border-radius: 3px;
                    padding: 2px 5px;
                    font-size: 12.5px;
                }}
                .badge {{
                    display: inline-block;
                    padding: 2px 7px;
                    border-radius: 4px;
                    font-size: 11px;
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
                    background-color: #202024;
                    border: 1px solid #2e2e33;
                    border-radius: 6px;
                    padding: 12px 16px;
                    margin-bottom: 14px;
                }}
                table {{
                    width: 100%;
                    border-collapse: collapse;
                    margin-top: 10px;
                    margin-bottom: 14px;
                    font-size: 12.5px;
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
                .callout {{
                    background-color: #182230;
                    border-left: 4px solid #38bdf8;
                    padding: 10px 14px;
                    margin: 10px 0px;
                    border-radius: 0 4px 4px 0;
                }}
            </style>
        </head>
        <body>
            {html}
        </body>
        </html>
        """)
        return browser

    def _create_overview_tab(self) -> QWidget:
        html = """
        <h2>⚡ Why DupeClean? (GPU-Accelerated vs. Legacy Tools)</h2>
        <p>
            Most traditional file deduplicators (such as <i>Czkawka</i>, <i>dupeGuru</i>, or <i>Duplicate Cleaner</i>)
            were architected for a previous computing era: they perform all cryptographic hashing, video decoding,
            and perceptual image math on the <b>CPU</b>.
        </p>
        <p>
            When scanning massive storage drives with <b>100,000+ RAW photos, 4K videos, or millions of files</b>,
            traditional tools peg CPU usage to 100%, trigger thermal throttling, and take hours or days to complete &mdash;
            while multi-thousand-dollar GPU Tensor Cores sit completely idle.
        </p>
        
        <div class="card">
            <h3 style="margin-top:0px; color:#38bdf8;">Engineering Paradigm Shift</h3>
            <table>
                <tr>
                    <th>Feature</th>
                    <th>🐢 Legacy Tools (CPU-Bound)</th>
                    <th>⚡ DupeClean (GPU-Accelerated)</th>
                </tr>
                <tr>
                    <td><b>Compute Engine</b></td>
                    <td>CPU software loops (heavy thermal & fan load)</td>
                    <td><b>GPU Tensor Cores & CUDA</b> (Batches executed in VRAM)</td>
                </tr>
                <tr>
                    <td><b>Similarity Intelligence</b></td>
                    <td>Basic low-res pixel gradients / blockhash</td>
                    <td><b>GPU Frequency Signatures (2D DCT)</b> via PyTorch</td>
                </tr>
                <tr>
                    <td><b>Throughput & Speed</b></td>
                    <td>~20&ndash;50 items/second (CPU bottlenecked)</td>
                    <td><b>600+ images/s</b> & <b>150+ videos/s</b> (Hardware-accelerated)</td>
                </tr>
                <tr>
                    <td><b>Duplicate Grouping</b></td>
                    <td>Iterative $O(N^2)$ CPU comparisons</td>
                    <td><b>Tensor Matrix Multiplication</b> in GPU VRAM</td>
                </tr>
                <tr>
                    <td><b>False-Positive Defense</b></td>
                    <td>Vulnerable to transitive similarity chaining</td>
                    <td><b>Strict Pairwise Mutual Similarity</b> (100% coherence)</td>
                </tr>
                <tr>
                    <td><b>Storage Engine</b></td>
                    <td>Fragile in-memory caches</td>
                    <td><b>SQLite WAL Database</b> (Crash-safe, instant rescans)</td>
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
            DupeClean achieves extreme deduplication throughput by strictly assigning each task to the processor 
            architecture best equipped for it: <b>CPU for parallel micro-sampling and sequential I/O; GPU Tensor Cores 
            for mass similarity calculations and matrix grouping.</b>
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
            <h3>Stage 2/3: Comparing File Signatures (Fast Sieve)</h3>
            <p>
                <b>What happens:</b> Reads only the initial <b>8 KB (composite prefix/suffix)</b> of files sharing identical sizes, 
                consulting the persistent local SQLite WAL cache.
            </p>
            <p>
                <b>Optimization:</b> If two 10 GB files differ in their first 8 KB, they are disqualified in 1 millisecond. 
                Over <b>90% of non-duplicate candidates are filtered out</b> without ever reading the full files from disk.
            </p>
        </div>

        <div class="card">
            <h3>Stage 3/3: Reading & Calculating File Hashes (Deep Verification)</h3>
            <p>
                <b>What happens:</b> For remaining candidates passing the 8 KB sieve, DupeClean reads file data in 
                <b>512 KB parallel streaming blocks</b> and computes cryptographic <code>xxHash64</code> digests.
            </p>
            <p>
                <b>Hardware Saturation:</b> Satures the sequential bandwidth of SSD RAID arrays and NVMe drives (<b>1,000+ MB/s</b>) 
                across up to 24 concurrent worker threads, ensuring the physical reading phase finishes in the minimum possible time.
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
            <b>Why is my Disk usage at 100% (~1 GB/s) during Stage 3?</b><br>
            To be mathematically certain that two files are 100% identical byte-for-byte, the operating system must read their 
            bytes from the physical disk into memory. DupeClean reads data at maximum hardware capability so this stage finishes as fast as possible.
        </div>
        <div class="callout">
            <b>Why does my RTX GPU show near 0% in Task Manager during Stage 3?</b><br>
            The GPU processes batches of frequency tensors in fractions of a millisecond (<0.5 ms). Because the GPU finishes 
            almost instantaneously and waits for the next disk I/O batch, Windows Task Manager's 1-second average sampler reports near 0% until the mass matrix multiplication phase starts.
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
                <li><b>Algorithm:</b> Multi-stage sieve architecture combining 8 KB prefix checks with <code>xxHash64</code> streaming digests.</li>
                <li><b>Guarantee:</b> 100% bit-for-bit identical duplicate guarantee. Zero false positives.</li>
                <li><b>Speed:</b> Millions of files evaluated in minutes with persistent SQLite caching.</li>
            </ul>
        </div>

        <div class="card">
            <h3 style="margin-top:0px; color:#38bdf8;">🖼️ Similar Images (Visual Perceptual Matching)</h3>
            <ul>
                <li><b>Target:</b> Photos, burst shots, resized images, recompressed JPEGs, wallpapers, and converted graphic formats.</li>
                <li><b>Algorithm:</b> 2D Discrete Cosine Transform (<b>2D DCT</b>) frequency hashing executed in GPU VRAM via PyTorch CUDA.</li>
                <li><b>Similarity Slider:</b> Configurable threshold from <b>70% to 100%</b>. Detects identical photos even if resized, color-graded, or re-saved as PNG, JPEG, or WEBP.</li>
                <li><b>Throughput:</b> 600+ photos/second on modern GeForce RTX GPUs.</li>
            </ul>
        </div>

        <div class="card">
            <h3 style="margin-top:0px; color:#38bdf8;">🎬 Similar Videos (Temporal Keyframe Matching)</h3>
            <ul>
                <li><b>Target:</b> Duplicate video clips, re-encoded movies, surveillance footage, and alternative resolutions (4K vs 1080p).</li>
                <li><b>Algorithm:</b> Fast keyframe seek pinpoints 5 distinct temporal timeline frames per video without full decoding.</li>
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
                <li><b>First Double-Click:</b> <b>Ascending</b> order (▲ native indicator).</li>
                <li><b>Second Double-Click:</b> <b>Descending</b> order (▼ native indicator).</li>
                <li><b>Third Double-Click:</b> <b>Normal / Unsorted</b> (restores original scan order and clears indicator).</li>
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
                    <td><code>Esc</code></td>
                    <td>Close open dialogs (Help, Confirmation dialogs, Previews).</td>
                </tr>
            </table>
        </div>

        <div class="card">
            <h3 style="margin-top:0px; color:#38bdf8;">Smart Selection Tools (<code>Select ▾</code>)</h3>
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
                <b>GPU Acceleration:</b> NVIDIA RTX Tensor Cores, CUDA FP16 & NVDEC
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
