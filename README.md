# DupeClean ⚡

**GPU-Accelerated Duplicate File Finder**

High-performance duplicate file, image, and video finder engineered and optimized exclusively for **NVIDIA GeForce RTX (30, 40, and 50 Series)** GPUs with Ampere, Ada Lovelace, and Blackwell architectures.

---

## 🚀 Engineering Highlights

* **Zero CPU Bottlenecks:** Image processing, discrete cosine transforms (2D DCT), and vector similarity comparisons execute directly in GPU **VRAM** and **Tensor Cores** via PyTorch CUDA.
* **Parallel Decoding & NVDEC:** High-throughput multi-threaded image decoding feeds the GPU, while temporal video frame extraction utilizes dedicated NVIDIA silicon decoders via FFmpeg Hardware Acceleration (`-hwaccel cuda`).
* **Strict Reference Folder Protection (`Ref`):** Mark any master/backup folder as a **Reference**. DupeClean matches duplicate files against it, but **never suggests or selects reference files for deletion**.
* **Krokiet-Inspired Ergonomics:**
  * Clean, native Dark Theme (KISS principle).
  * Drag & Drop folder support from Windows Explorer.
  * Similarity threshold slider (70% to 100%) for perceptual image and video matching.
  * Safe file deletion via the Windows Recycle Bin (`Send2Trash`).

---

## 🛠️ Getting Started

### Option 1: Quick Launch
Double-click:
```cmd
run.bat
```

### Option 2: Terminal
```powershell
.\.venv\Scripts\activate
python main.py
```

---

## 💻 Hardware Requirements
* **GPU:** NVIDIA GeForce RTX 30, 40, or 50 Series (Compute Capability ≥ 8.6).
* **NVIDIA Driver:** 550+ (with CUDA 12.4+ / 12.8+ support).
* **OS:** Windows 10/11 64-bit.
