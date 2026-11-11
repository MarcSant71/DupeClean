# DupeClean ⚡

**GPU-Accelerated Duplicate File Finder**

[![GitHub Release](https://img.shields.io/github/v/release/MarcSant71/DupeClean?color=007acc&logo=github)](https://github.com/MarcSant71/DupeClean/releases/latest)
[![Platform](https://img.shields.io/badge/Platform-Windows%2010%20%2F%2011%20x64-blue?logo=windows)](https://github.com/MarcSant71/DupeClean/releases/latest)
[![GPU](https://img.shields.io/badge/NVIDIA-RTX%20GPU%20Required-76b900?logo=nvidia)](https://github.com/MarcSant71/DupeClean/releases/latest)

High-performance duplicate file, image, and video finder engineered and optimized exclusively for **NVIDIA GeForce RTX (30, 40, and 50 Series)** GPUs with Ampere, Ada Lovelace, and Blackwell architectures.

> 📦 **Portable Release (No Python Installation Required):**  
> Download the pre-built standalone package [**`DupeClean-v1.1.2-windows-cuda.7z`**](https://github.com/MarcSant71/DupeClean/releases/latest), extract with [7-Zip](https://www.7-zip.org/) or WinRAR, and run `DupeClean.exe` directly.

---

## ⚡ Why DupeClean? (DupeClean vs. Traditional Tools)

Most popular deduplicators (Czkawka, dupeGuru, Duplicate Cleaner, etc.) were built for a previous computing era: they run all image hashing, vector calculations, and video decoding on the **CPU**. 

When scanning libraries with **100,000+ RAW photos or 4K videos**, legacy tools spike CPU usage to 100%, trigger thermal throttling, spin fans at maximum speed, and take **hours or days** - while your multi-thousand-dollar GPU sits completely idle.

**DupeClean changes the paradigm by moving the entire workload into dedicated NVIDIA silicon:**

| Feature | 🐢 Traditional Tools (Czkawka, dupeGuru, etc.) | ⚡ DupeClean (GPU-Accelerated) |
| :--- | :--- | :--- |
| **Compute Engine** | CPU-bound (Software loop, high thermal load) | **GPU Tensor Cores & CUDA** (Batches executed in VRAM) |
| **Similarity Intelligence** | Basic low-res pixel hashes (Gradient/Blockhash) | **GPU Frequency Signatures (2D DCT)** via PyTorch |
| **Throughput & Speed** | ~20–50 images/second (CPU bottlenecked) | **600+ images/second** (Parallel decoding + GPU compute) |
| **Video Keyframe Extraction** | Full sequential decoding (Slow, heavy lag) | **Fast Keyframe Seek + GPU Signatures** (150+ videos/s) |
| **UI Design Philosophy** | Crowded interfaces with dozens of technical options | **KISS Principle**: Clean dark mode, zero fluff, instant feedback |
| **Caching Pipeline** | In-memory or fragile cache | **SQLite WAL Engine**: Resumable, crash-safe, instant rescans |

---

## 🚀 Engineering Highlights

* **Zero CPU Bottlenecks:** Frequency feature extraction, normalizations, and similarity comparisons execute directly in GPU **VRAM** and **Tensor Cores** via PyTorch CUDA.
* **Parallel Multi-Process Extraction:** Multi-threaded image decoding feeds the GPU, while temporal video keyframe extraction pinpoints 5 distinct timeline samples without reading full files.
* **Reference Folder Protection (`Ref`):** Designate any master/backup folder as a **Reference**. DupeClean matches duplicate files against it, while ensuring reference files are strictly locked and never marked for deletion (following industry best practices established by tools like Czkawka).
* **Krokiet-Inspired Ergonomics:**
  * Clean, native Dark Theme (KISS principle).
  * Drag & Drop folder support from Windows Explorer.
  * Similarity threshold slider (70% to 100%) for perceptual image and video matching.
  * Safe file deletion via the Windows Recycle Bin (`Send2Trash`).

---

## 🔬 Pipeline Architecture & Hardware Allocation

DupeClean achieves extreme deduplication throughput by strictly assigning each task to the processor architecture best equipped for it: **CPU for parallel micro-sampling and I/O; GPU Tensor Cores for mass similarity calculations and duplicate grouping.**

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                   DUPECLEAN PIPELINE                                   │
└────────────────────────────────────────────────────────────────────────────────────────┘
  Stage 1: Discovery         Fast multi-threaded directory crawl & metadata indexing (CPU)
         │
  Stage 2: Cache Check       Instant lookup in SQLite WAL database (<5 ms per 10k items)
         │
  Stage 3: Frame Sampling    Multi-process CPU workers pinpoint 5 keyframes (Fast Seek)
         │                   Batched directly into GPU VRAM for frequency signatures (FP16)
         │
  Stage 4: GPU Grouping      Pairwise Cosine Similarity & Mutual Similarity Grouping
                             executed 100% inside RTX VRAM via Tensor Cores
```

### The 4-Stage Breakdown

#### Stage 1/4: Discovery & Metadata Enumeration (CPU / Disk I/O)
* **What happens:** Recursively crawls target folders, indexes paths, isolates user-defined **Reference Folders** (`Ref`), and extracts metadata (file size, timestamps, container extensions).
* **Optimization:** Discards zero-byte files, filters out excluded directories, and indexes hundreds of thousands of files in seconds using low-overhead OS system calls.

#### Stage 2/4: Exact Pre-filtering & Persistent Cache (CPU & SQLite WAL)
* **What happens:** Consults the local persistent database (`dupeclean_cache.db`) using SQLite WAL mode.
* **Optimization:** 
  * Files whose signatures already exist are loaded into memory at **over 15,000 files/second**, bypassing redundant disk reads entirely.
  * In *Duplicate Files* mode, applies accelerated xxHash64 on the initial 4 KB of files with identical sizes to eliminate 90%+ of non-duplicate candidates before reading full files.

#### Stage 3/4: Feature Extraction & Micro-Sampling (Multi-Process CPU → GPU Batching)
* **What happens:** Uncached images and videos are read, decoded, and converted into compact perceptual feature vectors.
* **Architecture Choice (Why CPU Multi-processing for Keyframes?):**
  * **Video Transcoding vs. Deduplication Sampling:** Video converters transcode hundreds of thousands of *continuous frames* from a single video, where paying the ~20 ms GPU session startup cost makes sense. In deduplication, we only need **5 temporal keyframes** across **100,000+ distinct files**.
  * **Zero Setup Overhead:** Standard GeForce drivers restrict concurrent NVDEC hardware decode sessions (typically 3 to 8 streams). Creating and tearing down GPU decoder contexts for 5 frames per file causes massive driver latency.
  * **Parallel CPU Workers:** DupeClean allocates `(CPU Threads // 2)` isolated worker processes. In standard indexed containers (MP4, MKV, MOV), workers execute **Fast Keyframe Seek**, reading only the header index and fetching the 5 I-frames in **1–2 ms** without reading the full file.
* **Immediate GPU Hand-off:** As soon as workers gather a batch of frames, PyTorch transfers them to the RTX GPU to execute **frequency image hashing (2D DCT)** in half-precision (FP16) in less than 0.5 ms.

#### Stage 4/4: Mass Tensor Core Duplicate Grouping (100% GPU VRAM)
* **What happens:** All file signatures reside in GPU VRAM as a contiguous matrix (e.g., `[154,624 x 3,840]` in FP16). The GPU performs all-to-all similarity comparisons via tensor matrix multiplication and applies **Mutual Similarity Grouping**.
* **Architecture Choice:** What would take hours of CPU multi-threading is executed in seconds on RTX **Tensor Cores**, guaranteeing that every file in a group directly matches all other files in that group with zero false-positive chaining.

---

### 💡 Frequently Asked Questions on Hardware Usage

#### *"Why is my CPU usage high (~50–60%) while my RTX GPU stays near 0% during Stage 3?"*
During Stage 3, multiple CPU workers are concurrently opening containers and extracting keyframes from disk. The RTX GPU processes each batch of 128 frames in **fractions of a millisecond** (<0.5 ms). Because the GPU finishes almost instantly and waits for the next disk/CPU batch, Windows Task Manager's 1-second average sampler reports ~0% utilization until Stage 4 begins.

#### *"How does DupeClean adapt to different CPUs (e.g., Core i9 vs. Ryzen 5)?"*
Worker allocation scales dynamically based on logical processor count:
* **Intel Core i9-14900K (32 threads):** 16 workers (~50–60 videos/s on raw DVR files, 250+ videos/s on MP4).
* **AMD Ryzen 5 4500 (12 threads):** 6 workers (~15 videos/s on raw DVR files, 70+ videos/s on MP4).
* Half of your CPU threads are always kept free so Windows, desktop audio, and the UI never freeze or stutter.

#### *"Does it automatically optimize for different RTX GPUs (RTX 3070 vs. 3090 vs. 5090)?"*
Yes. The `get_optimal_gpu_budgets()` engine inspects VRAM capacity and free headroom in real time via `torch.cuda.mem_get_info()`:
* **8 GB GPUs (e.g., RTX 3070, 4060):** Restricts GPU calculation blocks to **256 MB** to prevent Windows WDDM from swapping VRAM into system RAM (zero out-of-memory crashes).
* **24 GB / 32 GB GPUs (e.g., RTX 3090, 4090, 5090):** Automatically scales up to **2.0 GB blocks** and 1,024-item batches, saturating memory bandwidth and 3rd/4th/5th Gen Tensor Cores.

---

## 📦 Package Size & Architecture Transparency

Users often ask: **"Traditional tools like Czkawka are only ~15 MB - why is DupeClean's portable download ~1.7 GB (extracting to ~4.7 GB)?"**

Traditional deduplicators are lightweight single-binary executables because they run basic CPU-only pixel arithmetic using standard OS routines.

**DupeClean is a full-stack, GPU-accelerated AI vision workstation.** To deliver maximum throughput with **zero setup hassle**, the standalone package embeds everything out of the box:

1. **Complete NVIDIA CUDA 12 Runtimes (~3.5 GB uncompressed):**  
   Bundles compiled C++ hardware libraries (`torch_cuda.dll`, `cublas`, `cudnn`, `cusolver`, `cufft`) with pre-compiled kernel microcode engineered for NVIDIA RTX Ampere, Ada Lovelace, and Blackwell Tensor Cores.
2. **GPU Vision Engine (PyTorch CUDA):**  
   High-performance tensor processing pipelines that compute 2D DCT frequency signatures directly in GPU VRAM.
3. **High-Performance Video Pipeline:**  
   Built-in PyAV and FFmpeg libraries optimized for rapid multi-point keyframe sampling across video timelines.
4. **Self-Contained Python & PySide6 Environment:**  
   You do **not** need Python installed, no pip packages, no MSVC runtimes, and no 3+ GB NVIDIA CUDA Toolkit. Everything runs out of the box.

> [!NOTE]
> Thanks to **7-Zip LZMA2 Solid compression**, repeated patterns across all CUDA binaries compress down to **~1.7 GB**. Once extracted, DupeClean runs 100% locally with zero internet access, complete data privacy, and raw GPU performance.

> [!TIP]
> **Prefer a lightweight download? Run from source!**  
> If you already have Python and PyTorch installed (e.g., for ComfyUI, Stable Diffusion, or development), you don't need to download the 1.7 GB standalone bundle. Simply clone the repository (`git clone`) and run locally - the entire source code is **under 1 MB**! See [Running from Source](#-running-from-source-developers--advanced-users) below.

---

## ⚡ Quick Start (Portable - Recommended)

For 99% of users, **no installation, no Python, and no command line setup is required**:

1. Download [**`DupeClean-v1.1.2-windows-cuda.7z`**](https://github.com/MarcSant71/DupeClean/releases/latest) from the latest release.
2. Extract the archive using [7-Zip](https://www.7-zip.org/) or WinRAR.
3. Launch **`DupeClean.exe`** directly. Everything (Python runtime, CUDA 12 libraries, and GPU models) is pre-bundled and runs out of the box.

---

## 💻 System Requirements

* **GPU:** Dedicated NVIDIA GeForce RTX (30, 40, or 50 Series) with Compute Capability ≥ 8.6 (Ampere, Ada Lovelace, or Blackwell).
* **VRAM:** 4 GB minimum (8 GB+ recommended for massive photo and video libraries).
* **Operating System:** Windows 10 or Windows 11 (64-bit).
* **NVIDIA Driver:** Driver version **550.xx or higher** (supporting CUDA 12.4+ / 12.8+).

*(Note: Python and PyTorch are **not** required if you use the pre-built portable package above).*

---

## 🛠️ Running from Source (Developers & Advanced Users)

> [!NOTE]
> **This section is strictly optional!**  
> If you prefer not to download the ~1.7 GB standalone release and already have Python and CUDA on your system (e.g., for AI/ML development, Stable Diffusion, or ComfyUI), you can run directly from the source code. The repository source download is **under 1 MB**.

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
