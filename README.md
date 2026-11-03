# DupeClean ⚡

**GPU-Accelerated Duplicate File Finder**

[![GitHub Release](https://img.shields.io/github/v/release/MarcSant71/DupeClean?color=007acc&logo=github)](https://github.com/MarcSant71/DupeClean/releases/latest)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%20%2F%2011%20x64-blue?logo=windows)](https://github.com/MarcSant71/DupeClean/releases/latest)
[![GPU](https://img.shields.io/badge/NVIDIA-RTX%20GPU%20Required-76b900?logo=nvidia)](https://github.com/MarcSant71/DupeClean/releases/latest)

High-performance duplicate file, image, and video finder engineered and optimized exclusively for **NVIDIA GeForce RTX (30, 40, and 50 Series)** GPUs with Ampere, Ada Lovelace, and Blackwell architectures.

> 📦 **Portable Release (No Python Installation Required):**  
> Download the pre-built standalone package [**`DupeClean-v1.0.3-windows-cuda.7z`**](https://github.com/MarcSant71/DupeClean/releases/latest), extract with [7-Zip](https://www.7-zip.org/) or WinRAR, and run `DupeClean.exe` directly.

---

## ⚡ Why DupeClean? (DupeClean vs. Traditional Tools)

Most popular deduplicators (Czkawka, dupeGuru, Duplicate Cleaner, etc.) were built for a previous computing era: they run all image hashing, vector calculations, and video decoding on the **CPU**. 

When scanning libraries with **100,000+ RAW photos or 4K videos**, legacy tools spike CPU usage to 100%, trigger thermal throttling, spin fans at maximum speed, and take **hours or days** — while your multi-thousand-dollar GPU sits completely idle.

**DupeClean changes the paradigm by moving the entire workload into dedicated NVIDIA silicon:**

| Feature | 🐢 Traditional Tools (Czkawka, dupeGuru, etc.) | ⚡ DupeClean (GPU-Accelerated) |
| :--- | :--- | :--- |
| **Compute Engine** | CPU-bound (Software loop, high thermal load) | **GPU Tensor Cores & CUDA** (Batches executed in VRAM) |
| **Similarity Intelligence** | Basic low-res pixel hashes (Gradient/Blockhash) | **Deep Neural Embeddings (MobileNetV3)** via PyTorch |
| **Throughput & Speed** | ~20–50 images/second (CPU bottlenecked) | **600+ images/second** (Parallel decoding + GPU compute) |
| **Video Frame Extraction** | Software CPU decoding (Slow, heavy lag) | **Hardware NVDEC** via dedicated NVIDIA silicon decoders |
| **UI Design Philosophy** | Crowded interfaces with dozens of technical options | **KISS Principle**: Clean dark mode, zero fluff, instant feedback |
| **Caching Pipeline** | In-memory or fragile cache | **SQLite WAL Engine**: Resumable, crash-safe, instant rescans |

---

## 🚀 Engineering Highlights

* **Zero CPU Bottlenecks:** Image processing, neural feature extraction, and vector similarity comparisons execute directly in GPU **VRAM** and **Tensor Cores** via PyTorch CUDA.
* **Parallel Decoding & NVDEC:** High-throughput multi-threaded image decoding feeds the GPU, while temporal video frame extraction utilizes dedicated NVIDIA silicon decoders via FFmpeg Hardware Acceleration (`-hwaccel cuda`).
* **Reference Folder Protection (`Ref`):** Designate any master/backup folder as a **Reference**. DupeClean matches duplicate files against it, while ensuring reference files are strictly locked and never marked for deletion (following industry best practices established by tools like Czkawka).
* **Krokiet-Inspired Ergonomics:**
  * Clean, native Dark Theme (KISS principle).
  * Drag & Drop folder support from Windows Explorer.
  * Similarity threshold slider (70% to 100%) for perceptual image and video matching.
  * Safe file deletion via the Windows Recycle Bin (`Send2Trash`).

---

## 📦 Package Size & Architecture Transparency

Users often ask: **"Traditional tools like Czkawka are only ~15 MB — why is DupeClean's portable download ~1.7 GB (extracting to ~4.7 GB)?"**

Traditional deduplicators are lightweight single-binary executables because they run basic CPU-only pixel arithmetic using standard OS routines.

**DupeClean is a full-stack, GPU-accelerated AI vision workstation.** To deliver maximum throughput with **zero setup hassle**, the standalone package embeds everything out of the box:

1. **Complete NVIDIA CUDA 12 Runtimes (~3.5 GB uncompressed):**  
   Bundles compiled C++ hardware libraries (`torch_cuda.dll`, `cublas`, `cudnn`, `cusolver`, `cufft`) with pre-compiled kernel microcode engineered for NVIDIA RTX Ampere, Ada Lovelace, and Blackwell Tensor Cores.
2. **Deep Learning Vision Engine (PyTorch & Torchvision):**  
   Pre-trained MobileNetV3 neural network architectures and tensor processing pipelines that extract semantic perceptual embeddings directly in GPU VRAM.
3. **Hardware NVDEC Video Pipeline (FFmpeg GPU Binaries):**  
   Bundles custom `ffmpeg` and `ffprobe` binaries built with NVIDIA NVDEC/CUDA hardware acceleration for direct silicon video frame extraction.
4. **Self-Contained Python & PySide6 Environment:**  
   You do **not** need Python installed, no pip packages, no MSVC runtimes, and no 3+ GB NVIDIA CUDA Toolkit. Everything runs out of the box.

> [!NOTE]
> Thanks to **7-Zip LZMA2 Solid compression**, repeated patterns across all CUDA binaries compress down to **~1.7 GB**. Once extracted, DupeClean runs 100% locally with zero internet access, complete data privacy, and raw GPU performance.

> [!TIP]
> **Prefer a lightweight download? Run from source!**  
> If you already have Python and PyTorch installed (e.g., for ComfyUI, Stable Diffusion, or development), you don't need to download the 1.7 GB standalone bundle. Simply clone the repository (`git clone`) and run locally — the entire source code is **under 1 MB**! See [Installation & Setup](#-installation--setup) below.

---

## 💻 Hardware & Software Requirements

### Hardware
* **GPU:** Dedicated NVIDIA GeForce RTX (30, 40, or 50 Series) with Compute Capability ≥ 8.6 (Ampere, Ada Lovelace, or Blackwell).
* **VRAM:** 4 GB minimum (8 GB+ recommended for massive photo and video datasets).

### Software & Environment
* **Operating System:** Windows 10 or Windows 11 (64-bit).
* **NVIDIA Driver:** Driver version **550.xx or higher** (supporting CUDA 12.4+ / 12.8+).
* **Python:** Python **3.10, 3.11, or 3.12** (64-bit).
* **PyTorch:** PyTorch 2.4+ compiled with CUDA support.
* **FFmpeg (for Video Deduplication):** `ffmpeg` and `ffprobe` with NVIDIA NVDEC/CUDA hardware acceleration enabled (placed in the project `bin/` directory or available in system `PATH`).

---

## 🛠️ Installation & Setup

### 1. Clone the Repository
```powershell
git clone https://github.com/MarcSant71/DupeClean.git
cd DupeClean
```

### 2. Create and Activate Virtual Environment
```powershell
python -m venv .venv
.\.venv\Scripts\activate
```

### 3. Install PyTorch with CUDA Support
Install the PyTorch CUDA build corresponding to your GPU generation:
```powershell
# For RTX 30 / 40 Series (CUDA 12.4):
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124

# For RTX 50 Series (Blackwell - CUDA 12.8):
pip install --pre torch torchvision --index-url https://download.pytorch.org/whl/nightly/cu128
```

### 4. Install Dependencies
```powershell
pip install -r requirements.txt
```

### 5. Run the Application
Double-click `scripts\run.bat` or run via terminal:
```powershell
python main.py
```

### 6. Build Standalone Portable Distribution (Optional)
To freeze the application into a standalone package with PyInstaller:
```powershell
.\scripts\build.bat
```
