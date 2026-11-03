"""
Hardware Gatekeeper para o DupeClean.
Garante que a execução ocorra exclusivamente em GPUs NVIDIA RTX Séries 30, 40 ou 50
(Compute Capability >= 8.6 - Ampere, Ada Lovelace, Blackwell) e vincula
em tempo de execução (in-process) o PyTorch diretamente ao dispositivo CUDA 0.
"""

import os
from typing import Tuple, Dict, Any


def enforce_high_performance_gpu():
    """
    Configura em tempo de execução (apenas no escopo do processo atual, sem tocar no registro do Windows)
    as variáveis de ambiente para que todo o processamento de tensores e IA ocorra na NVIDIA RTX (Device 0).
    Execução ultrarrápida (<1ms) para que a Splash Screen apareça instantaneamente ao clicar no aplicativo.
    """
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"


def check_gpu_compatibility() -> Tuple[bool, str, Dict[str, Any]]:
    """
    Verifica se o sistema possui uma GPU NVIDIA compatível.
    Executado em segundo plano pelo HardwareInitWorker enquanto a Splash Screen anima.
    Retorna (compativel, mensagem_erro, info_hardware).
    """
    enforce_high_performance_gpu()

    import torch

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

    # Fixa o dispositivo primário explicitamente
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
