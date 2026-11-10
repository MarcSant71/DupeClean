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


def get_optimal_gpu_budgets() -> Dict[str, Any]:
    """
    Calcula dinamicamente os orçamentos de memória VRAM e tamanhos de lote
    baseando-se estritamente na GPU real e na VRAM livre detectada em tempo de execução.
    
    Protege placas de 8 GB (RTX 3070 / 4060) contra transbordamento para RAM compartilhada,
    ao mesmo tempo em que extrai 100% da vazão dos Tensor Cores em placas de 24 GB / 32 GB (3090, 4090, 5090).
    """
    try:
        import torch
        if not torch.cuda.is_available():
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

        # Regra de Segurança: aloca no máximo 25% da VRAM livre para um único bloco de cálculo GEMM.
        # Garante 75% da VRAM livre como margem de segurança contra WDDM swap para a RAM.
        # Piso: 256 MB (para 8 GB VRAM) | Teto: 2.0 GB (ponto ótimo para 24-32 GB VRAM).
        target_chunk_bytes = max(
            256 * 1024 * 1024,
            min(int(free_bytes * 0.25), 2 * 1024 * 1024 * 1024)
        )

        # Escalonamento dinâmico do lote de extração de imagens por VRAM total
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
