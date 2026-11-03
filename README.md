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
Double-click `run.bat` or run via terminal:
```powershell
python main.py
```
