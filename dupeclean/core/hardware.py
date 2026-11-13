"""
Hardware Gatekeeper para o DupeClean.
Ensures execution runs on NVIDIA RTX 30, 40, or 50 series GPUs when available,
(Compute Capability >= 8.6 - Ampere, Ada Lovelace, Blackwell) e vincula
in-process PyTorch binding directly to CUDA device 0.
"""

import os
from typing import Tuple, Dict, Any


def enforce_high_performance_gpu():
    """
    Configures environment variables at runtime (scoped to the current process without touching the Windows registry)
    so all tensor and AI processing runs on the NVIDIA RTX GPU (Device 0).
    Ultra-fast execution (<1ms) so the Splash Screen appears immediately on launch.
    """
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"


def _cpu_fallback_info(reason: str, capability: str = "N/A", vram_gb: float = 0.0, device_id: Any = None) -> Dict[str, Any]:
    """Build the default metadata dictionary for CPU fallback execution."""
    return {
        "name": "NO GPU",
        "has_cuda": False,
        "capability": capability,
        "arch": "CPU Mode",
        "vram_gb": vram_gb,
        "device_id": device_id,
        "reason": reason,
    }


def _is_rtx_capability(major: int, minor: int) -> bool:
    """Return True if Compute Capability meets the RTX 30+ minimum (8.6)."""
    return (major > 8) or (major == 8 and minor >= 6)


def get_compute_device() -> Tuple[str, Any]:
    """
    Returns the active compute device and corresponding dtype:
    - ('cuda', torch.float16) when a compatible NVIDIA RTX (CC >= 8.6) is available.
    - ('cpu', torch.float32) as a universal fallback when no compatible GPU is present.
    """
    import torch

    if not torch.cuda.is_available() or torch.cuda.device_count() == 0:
        return "cpu", torch.float32

    try:
        major, minor = torch.cuda.get_device_capability(0)
        if _is_rtx_capability(major, minor):
            return "cuda", torch.float16
    except Exception:
        pass

    return "cpu", torch.float32


def check_gpu_compatibility() -> Tuple[bool, str, Dict[str, Any]]:
    """
    Inspects available graphics hardware for engine selection (CUDA RTX or CPU Fallback).
    Executado em segundo plano pelo HardwareInitWorker enquanto a Splash Screen anima.
    Retorna (pode_iniciar, mensagem_erro, info_hardware).
    """
    enforce_high_performance_gpu()

    import torch

    if not torch.cuda.is_available():
        return True, "", _cpu_fallback_info("No CUDA-capable NVIDIA GPU detected")

    device_count = torch.cuda.device_count()
    if device_count == 0:
        return True, "", _cpu_fallback_info("No accessible CUDA devices found")

    # Explicitly pin the primary device
    device_id = 0
    try:
        torch.cuda.set_device(device_id)
    except Exception:
        pass

    device_name = torch.cuda.get_device_name(device_id)
    capability = torch.cuda.get_device_capability(device_id)
    vram_bytes = torch.cuda.get_device_properties(device_id).total_memory
    vram_gb = round(vram_bytes / (1024 ** 3), 2)

    major, minor = capability

    # Supported architectures:
    # 8.6 / 8.9: Ampere / Ada Lovelace (RTX 30 and 40)
    # 9.0: Hopper
    # 10.0 / 12.0: Blackwell (RTX 50)
    if not _is_rtx_capability(major, minor):
        return True, "", _cpu_fallback_info(
            reason=f"{device_name} (Compute Capability {major}.{minor} < 8.6)",
            capability=f"{major}.{minor}",
            vram_gb=vram_gb,
            device_id=device_id,
        )

    # Architecture family identification
    if major >= 12 or major == 10:
        arch_name = "Blackwell (RTX 50 Series)"
    elif major == 8 and minor == 9:
        arch_name = "Ada Lovelace (RTX 40 Series)"
    elif major == 8 and minor == 6:
        arch_name = "Ampere (RTX 30 Series)"
    else:
        arch_name = f"NVIDIA Architecture ({major}.{minor})"

    info = {
        "name": device_name,
        "has_cuda": True,
        "capability": f"{major}.{minor}",
        "arch": arch_name,
        "vram_gb": vram_gb,
        "device_id": device_id
    }

    return True, "", info


def get_optimal_gpu_budgets() -> Dict[str, Any]:
    """
    Dynamically computes memory budgets and batch sizes
    baseando-se no dispositivo ativo (GPU RTX em VRAM ou fallback em CPU).
    """
    try:
        import torch
        device, _ = get_compute_device()
        if device != "cuda":
            return {
                "target_chunk_bytes": 128 * 1024 * 1024,
                "image_batch_size": 128,
                "vram_total_gb": 0.0,
                "vram_free_gb": 0.0,
                "gpu_name": "CPU",
            }

        free_bytes, total_bytes = torch.cuda.mem_get_info()
        total_gb = total_bytes / (1024 ** 3)
        free_gb = free_bytes / (1024 ** 3)

        # Safety Rule: allocate at most 25% of free VRAM for a single GEMM compute block.
        # Keeps 75% of free VRAM as a safety margin against WDDM swap to system RAM.
        # Floor: 256 MB (for 8 GB VRAM) | Ceiling: 2.0 GB (sweet spot for 24-32 GB VRAM).
        target_chunk_bytes = max(
            256 * 1024 * 1024,
            min(int(free_bytes * 0.25), 2 * 1024 * 1024 * 1024)
        )

        # Dynamic image extraction batch scaling by total VRAM
        if total_gb >= 24.0:
            image_batch_size = 1024
        elif total_gb >= 11.5:
            image_batch_size = 512
        else:
            image_batch_size = 256

        return {
            "target_chunk_bytes": target_chunk_bytes,
            "image_batch_size": image_batch_size,
            "vram_total_gb": round(total_gb, 2),
            "vram_free_gb": round(free_gb, 2),
            "gpu_name": torch.cuda.get_device_name(0),
        }
    except Exception:
        return {
            "target_chunk_bytes": 256 * 1024 * 1024,
            "image_batch_size": 256,
            "vram_total_gb": 8.0,
            "vram_free_gb": 4.0,
            "gpu_name": "NVIDIA RTX",
        }
