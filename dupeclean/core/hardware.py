"""
Hardware Gatekeeper para o DupeClean.
Garante que a execução ocorra exclusivamente em GPUs NVIDIA RTX Séries 30, 40 ou 50
(Compute Capability >= 8.6 - Ampere, Ada Lovelace, Blackwell).
"""

from typing import Tuple, Dict, Any
import torch


def check_gpu_compatibility() -> Tuple[bool, str, Dict[str, Any]]:
    """
    Verifica se o sistema possui uma GPU NVIDIA compatível.
    Retorna (compativel, mensagem_erro, info_hardware).
    """
    if not torch.cuda.is_available():
        return (
            False,
            "No CUDA-capable NVIDIA GPU was detected on this system.\n\n"
            "DupeClean is engineered specifically for hardware acceleration\n"
            "on NVIDIA GeForce RTX GPUs (30, 40, or 50 Series).",
            {}
        )

    device_count = torch.cuda.device_count()
    if device_count == 0:
        return False, "No accessible CUDA devices found.", {}

    # Get primary device
    device_id = 0
    device_name = torch.cuda.get_device_name(device_id)
    capability = torch.cuda.get_device_capability(device_id)
    vram_bytes = torch.cuda.get_device_properties(device_id).total_memory
    vram_gb = round(vram_bytes / (1024 ** 3), 2)

    major, minor = capability

    # Supported architectures:
    # 8.6 / 8.9: Ampere / Ada Lovelace (RTX 30 and 40)
    # 9.0: Hopper
    # 10.0 / 12.0: Blackwell (RTX 50)
    if (major < 8) or (major == 8 and minor < 6):
        return (
            False,
            f"Incompatible GPU detected: {device_name}\n\n"
            f"Compute Capability: {major}.{minor}\n"
            "DupeClean requires an NVIDIA RTX 30, 40, or 50 Series GPU\n"
            "(Compute Capability 8.6 or higher).",
            {}
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
        "capability": f"{major}.{minor}",
        "arch": arch_name,
        "vram_gb": vram_gb,
        "device_id": device_id
    }

    return True, "", info
