Standalone portable release for Windows x64 with NVIDIA RTX GPU acceleration.

### What's New in v1.0.8
- **Deterministic Scan Concurrency Protection (KISS):** Centralized scanning state controller (`_set_scanning_state`). Automatically locks the `Clear Cache` utility button, sidebar scan mode selectors, similarity slider, folder panel, and batch action buttons (`Move`, `Recycle Bin`, `Delete`, `Save`) during active scans, eliminating SQLite lock collisions (`VACUUM` race conditions) and background worker conflicts.
- **Comprehensive Disabled Visual States:** Added full `:disabled` styling for sidebar mode selectors (`sidebarModes`), item highlights, inner divider rules, similarity slider track/handle, and the clear cache button. Inactive controls visually dim into dark/gray tones (`#18181b`, `#222226`, `#52525b`) during scanning, providing instant visual feedback.
- **Fail-Fast Cache Guard Clause:** Added worker thread state verification to `_clear_cache_prompt` and safe exception handling (`try...except`) to guarantee cache maintenance cannot be triggered during an ongoing scan.

### What's New in v1.0.7
- **Redesigned Modern Startup Splash Screen:** Centered, refined and streamlined splash screen architecture with 50% larger typography and high-definition visual scaling (36px title, 18px subtitle, 18px status, 9px progress bar).
- **Pixel-Perfect Vertical Symmetry & Proportions:** Engineered mathematically balanced vertical margins (35px top margin to title, 35px bottom margin to progress bar) and compact card framing (490x250) that eliminates lateral voids.
- **Uncut Font Glyph Rendering:** Added explicit transparent widget backgrounds and ample vertical clearance to eliminate Qt layout compression and preserve full font descenders across all display scale factors.
- **Harmonic Uniform Spacing:** Normalized inter-element spacing to uniform 8px intervals across title, subtitle, status telemetry, and active progress bar.

### What's New in v1.0.6
- **Clean Discovery Indexing Architecture:** Removed fragile crawler estimations that could cause premature 100% progress freezes when scanning folders of varying depths. The filesystem crawler operates in pure discovery mode (`Scanning folders... (X files found)`) with a continuous wave activity indicator.
- **KISS Visual Conciseness:** Replaced rapidly flickering folder/file labels with a static, stable status message (`Scanning folders... (X files found)`), eliminating visual noise while maintaining live discovery counts and transfer speeds (`files/s`).
- **Mathematical Multi-Phase Precision:** Once directory discovery finishes and exact file counts are known, the progress bar deterministically transitions to exact 0% to 100% processing for exact hashing, MobileNetV3 visual embeddings, and NVDEC hardware video decoding.

### What's New in v1.0.5
- **Zero-Layout-Shift Persistent Status Bar:** Engineered permanent two-row telemetry footer with retained geometry (`setRetainSizeWhenHidden(True)`). Eliminates horizontal dancing and vertical layout shifts when starting or completing scans. The full-width progress bar dynamically scales to window dimensions and full-screen maximization without shifting content or tables.
- **Instant Application Startup (<100ms):** PyTorch and CUDA 12 runtimes are lazily imported and verified exclusively within the background `HardwareInitWorker` thread, cutting cold-start launch latency from 12 seconds to under 100 milliseconds and instantly displaying the splash screen.
- **Organic Splash Animation:** Re-architected splash screen progress indicator with dynamic asymptotic acceleration, smoothly advancing while the GPU Gatekeeper validates Ampere/Ada Lovelace/Blackwell hardware.

### What's New in v1.0.4
- **Block GEMM Memory Architecture (~40 GB to ~500 MB):** Replaced dense $N \times N$ matrix similarity calculation with chunked Block GEMM (2048-row blocks) on GPU. Reduced memory footprint from ~40 GB to ~500 MB for 140k+ files, eliminating OS memory swap freezes and checking `stop_check` every 5ms.
- **Deterministic Progress & Instant Estimations:** SQLite-backed file count estimation (`estimate_file_count`) querying recent crawl history and persistent metadata in <1ms without redundant disk I/O, replacing bouncing marquee animations with smooth 0–100% progress bars.
- **Live Indexing Telemetry:** Real-time crawler telemetry reporting active subfolder names, live speed (`files/s`), and continuous ETA calculations.
- **Instant Cancellation Latency (20ms):** Active thread pool future cancellation (`pending.cancel()`) and atomic loops, reducing abort latency from 40s+ to 20ms.
- **Refined Status Feedback:** Clear and accurate completion messages ("Estimated space to free: X MB") with proper singular/plural grammar and explicit "Scan cancelled by user." feedback.

### What's New in v1.0.3
- **High-Throughput Concurrent I/O Engine:** Multi-threaded parallel file reader with 4 MB streaming buffers in `hasher_exact.py`, designed to maximize data transfer rates and eliminate I/O bottlenecks across local NVMe/SATA drives as well as high-speed networked storage (NAS, SAN, iSCSI).
- **Fast Win32 Metadata Crawler:** Directory traversal rewritten using native `os.scandir` caching, eliminating 100% of redundant individual network RPC `stat()` calls over SMB.
- **Instant Scan Cancellation (<0.1s):** Granular atomic checks at 4 MB buffer boundaries and immediate executor queue cancellation (`cancel_futures=True`), reducing scan abort response time from 30 seconds to ~100 milliseconds.
- **In-Process Hardware Binding:** Explicit CUDA Device 0 and NVDEC silicon assignment (`torch.cuda.set_device(0)` and `-hwaccel_device 0`), fully isolated without modifying the Windows registry.
- **Clean Repository Architecture:** Centralized all execution, debug, and build scripts into `scripts/` (`run.bat`, `run_debug.bat`, `build.bat`), keeping root strictly clean.

### Key Features
- **GPU Neural Embeddings:** MobileNetV3 visual embeddings computed directly in VRAM via PyTorch CUDA.
- **Multi-Tier Pipeline:** Size grouping, xxHash64 exact hashing, and perceptual similarity.
- **Hardware NVDEC Acceleration:** High-throughput video decoding using dedicated NVIDIA silicon decoders.
- **Reference Folder Protection:** Protects master directories (`Ref`) from accidental deletion.
- **State & Folder Persistence:** Automatically remembers scan folders and Reference flags in local SQLite database (`data/DupeClean.db`) across sessions.
- **Massive Dataset Resilience:** Industrial-grade SQLite engine with 8KB page sizes, 256MB mmap I/O, strict connection lifecycle management, and automatic 64MB WAL capping, tested and validated on libraries with millions of files without locking or storage limits.
- **100% Portable:** Pre-bundled with Python runtime, CUDA DLLs, and FFmpeg. No installation required.
- **KISS Dark UI:** Clean PySide6 interface with non-blocking real-time telemetry.

### Quick Start (Portable)
1. Download **`DupeClean-v1.0.8-windows-cuda.7z`** below.
2. Extract with [7-Zip](https://www.7-zip.org/) or WinRAR to any folder.
3. Launch **`DupeClean.exe`**.

---

### 📦 Why is the Download ~1.7 GB? (~4.7 GB Unpacked)
Traditional tools are lightweight (~15 MB) because they run basic pixel math on the CPU. **DupeClean bundles a complete GPU AI workstation:**
- Full **NVIDIA CUDA 12 hardware runtimes** (`torch_cuda`, `cublas`, `cudnn`, `cusolver`, `cufft`).
- **PyTorch deep learning vision models** (MobileNetV3).
- Hardware-accelerated **FFmpeg NVDEC** binaries.
- Completely isolated Python 3 runtime — **zero installation or setup required**.

### 💡 Prefer a lightweight download? Run from source!
If you already have Python and PyTorch installed on your machine (e.g., from ComfyUI or SD), you don't need this 1.7 GB bundle! You can clone and run directly from source (**under 1 MB** of code):

```bash
git clone https://github.com/MarcSant71/DupeClean.git
cd DupeClean
pip install -r requirements.txt
python main.py
```
