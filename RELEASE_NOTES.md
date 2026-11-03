Standalone portable release for Windows x64 with NVIDIA RTX GPU acceleration.

### Key Features
- **GPU Neural Embeddings:** MobileNetV3 visual embeddings computed directly in VRAM via PyTorch CUDA.
- **Multi-Tier Pipeline:** Size grouping, xxHash64 exact hashing, and perceptual similarity.
- **Hardware NVDEC Acceleration:** High-throughput video decoding using dedicated NVIDIA silicon decoders.
- **Reference Folder Protection:** Protects master directories (`Ref`) from accidental deletion.
- **State & Folder Persistence:** Automatically remembers scan folders and Reference flags in local SQLite database (`data/DupeClean.db`) across sessions.
- **100% Portable:** Pre-bundled with Python runtime, CUDA DLLs, and FFmpeg. No installation required.
- **KISS Dark UI:** Clean PySide6 interface with non-blocking real-time telemetry.

### Quick Start (Portable)
1. Download **`DupeClean-v1.0.0-windows-cuda.7z`** below.
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
