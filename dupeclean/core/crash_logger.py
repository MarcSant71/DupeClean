"""
Gerenciador de Logs de Falhas Críticas com Anonimização LGPD e Retenção de 7 Dias.
Registra no disco estritamente quando ocorre uma falha fatal ou exceção não tratada,
garantindo 100% de privacidade ao expurgar nomes de usuários, diretórios e arquivos pessoais.
"""

import os
import sys
import time
import re
import platform
import traceback
import ctypes
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, Union

from dupeclean import __version__


def get_project_root() -> Path:
    """Retorna o diretório base da aplicação (raiz em dev ou diretório do executável congelado)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent.parent


def get_logs_dir() -> Path:
    """Retorna o diretório data/logs para armazenamento dos relatórios de falha."""
    base_dir = get_project_root()
    logs_dir = base_dir / "data" / "logs"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
        return logs_dir
    except Exception:
        appdata = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "DupeClean" / "data" / "logs"
        appdata.mkdir(parents=True, exist_ok=True)
        return appdata


def prune_old_logs(logs_dir: Path, max_age_days: int = 7) -> int:
    """
    Remove arquivos de log na pasta logs_dir com tempo de modificação superior a max_age_days.
    Retorna a quantidade de arquivos excluídos.
    """
    pruned_count = 0
    try:
        now = time.time()
        max_age_seconds = max_age_days * 86400
        for item in logs_dir.glob("crash_*.log"):
            try:
                if (now - item.stat().st_mtime) > max_age_seconds:
                    item.unlink()
                    pruned_count += 1
            except Exception:
                pass
    except Exception:
        pass
    return pruned_count


def sanitize_text(raw_text: str, project_root: Optional[Path] = None) -> str:
    """
    Aplica filtros de privacidade em conformidade rigorosa com a LGPD:
    - Mascara o nome do usuário do Windows (%USERNAME% / Home).
    - Converte caminhos absolutos do código-fonte em caminhos relativos ao projeto.
    - Mascara caminhos locais de pastas/arquivos de usuário ([REDACTED_PATH]).
    - Mascara caminhos UNC de rede ([REDACTED_UNC_PATH]).
    """
    if not raw_text:
        return ""

    sanitized = raw_text
    root = project_root or get_project_root()
    root_str = str(root)

    # 1. Mascara nome de usuário do sistema operacional
    username = os.environ.get("USERNAME", "")
    if username and len(username) >= 2:
        sanitized = re.sub(re.escape(username), "[USER]", sanitized, flags=re.IGNORECASE)

    try:
        home_user = Path.home().name
        if home_user and len(home_user) >= 2 and home_user.lower() != username.lower():
            sanitized = re.sub(re.escape(home_user), "[USER]", sanitized, flags=re.IGNORECASE)
    except Exception:
        pass

    # 2. Converte caminhos absolutos do código-fonte para relativos
    variants = [
        root_str,
        root_str.replace("/", "\\"),
        root_str.replace("\\", "/"),
    ]
    for v in variants:
        if v:
            sanitized = sanitized.replace(v + "\\", "")
            sanitized = sanitized.replace(v + "/", "")
            sanitized = sanitized.replace(v, ".")

    # 3. Simplifica caminhos do runtime Python / site-packages
    sanitized = re.sub(r'["\'][^"\']*[\\/]site-packages[\\/]([^"\']+)["\']', r'"site-packages/\1"', sanitized)
    sanitized = re.sub(r'["\'][^"\']*[\\/]Lib[\\/]([^"\']+)["\']', r'"python-lib/\1"', sanitized)

    # 4. Mascara caminhos de rede UNC: \\server\share\...
    sanitized = re.sub(r'(\\\\[A-Za-z0-9_.\-]+(?:\\[^\s"\'\r\n\t]+)*)', r'[REDACTED_UNC_PATH]', sanitized)

    # 5. Mascara caminhos de unidades locais do usuário: [A-Za-z]:\...
    def _path_replacer(match):
        full = match.group(0)
        # Identifica se possui extensão de arquivo no final
        ext_match = re.search(r'(\.[a-zA-Z0-9_]{1,8})$', full.rstrip("\"'"))
        ext = ext_match.group(1).lower() if ext_match else ""
        return f"[REDACTED_PATH/*{ext}]" if ext else "[REDACTED_PATH]"

    # Mascara caminhos delimitados por aspas
    sanitized = re.sub(r'["\'][A-Za-z]:\\[^"\'\r\n\t]+["\']', _path_replacer, sanitized)
    # Mascara caminhos soltos no texto que não pertençam ao projeto
    sanitized = re.sub(r'(?<![A-Za-z0-9_])[A-Za-z]:\\(?:[^\s"\'\r\n\t,;:]+)', _path_replacer, sanitized)

    return sanitized


def get_system_telemetry() -> Dict[str, Any]:
    """Coleta telemetria técnica de hardware e sistema sem qualquer identificador pessoal."""
    telemetry = {
        "dupeclean_version": __version__,
        "os": f"{platform.system()} {platform.release()} (build {platform.version()})",
        "python": f"{platform.python_version()} ({platform.architecture()[0]})",
        "cpu_count": os.cpu_count() or 1,
    }

    # Memória RAM do Sistema (Windows GlobalMemoryStatusEx)
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            telemetry["ram_total_gb"] = round(stat.ullTotalPhys / (1024 ** 3), 2)
            telemetry["ram_avail_gb"] = round(stat.ullAvailPhys / (1024 ** 3), 2)
    except Exception:
        pass

    # Informações de GPU / CUDA / PyTorch
    try:
        import torch
        telemetry["pytorch_version"] = torch.__version__
        telemetry["cuda_available"] = torch.cuda.is_available()
        if torch.cuda.is_available():
            telemetry["cuda_version"] = torch.version.cuda
            telemetry["gpu_name"] = torch.cuda.get_device_name(0)
            telemetry["vram_total_gb"] = round(torch.cuda.get_device_properties(0).total_memory / (1024 ** 3), 2)
            telemetry["vram_allocated_gb"] = round(torch.cuda.memory_allocated(0) / (1024 ** 3), 2)
            telemetry["vram_reserved_gb"] = round(torch.cuda.memory_reserved(0) / (1024 ** 3), 2)
    except Exception:
        pass

    return telemetry


def log_critical_crash(
    exc: Union[Exception, Tuple[Any, Any, Any]],
    context: Optional[Dict[str, str]] = None
) -> Optional[Path]:
    """
    Ponto de entrada oficial para gravação de falhas críticas:
    1. Formata traceback e mensagem de erro.
    2. Aplica saneamento estrito (LGPD).
    3. Executa a limpeza de logs com mais de 7 dias de retenção.
    4. Grava em data/logs/crash_YYYY-MM-DD.log.
    Retorna o Path do arquivo gravado, ou None em caso de falha.
    """
    try:
        logs_dir = get_logs_dir()
        prune_old_logs(logs_dir, max_age_days=7)

        # Trata formato da exceção
        if isinstance(exc, tuple) and len(exc) == 3:
            exc_type, exc_val, exc_tb = exc
            raw_tb = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
            exc_class_name = exc_type.__name__ if hasattr(exc_type, "__name__") else str(exc_type)
            exc_message = str(exc_val)
        elif isinstance(exc, BaseException):
            raw_tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            exc_class_name = type(exc).__name__
            exc_message = str(exc)
        else:
            raw_tb = str(exc)
            exc_class_name = "UnknownException"
            exc_message = str(exc)

        # Coleta telemetria e sanitiza
        telemetry = get_system_telemetry()
        sanitized_tb = sanitize_text(raw_tb)
        sanitized_msg = sanitize_text(exc_message)

        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        timestamp_str = now.strftime("%Y-%m-%d %H:%M:%S")

        log_path = logs_dir / f"crash_{date_str}.log"

        lines = [
            "=" * 80,
            f"CRITICAL ERROR REPORT - {timestamp_str} (Local Time)",
            "=" * 80,
            "Environment & Hardware:",
            f"  • DupeClean Version : {telemetry.get('dupeclean_version', 'Unknown')}",
            f"  • Operating System  : {telemetry.get('os', 'Unknown')}",
            f"  • Python Runtime    : {telemetry.get('python', 'Unknown')}",
            f"  • System RAM        : Total: {telemetry.get('ram_total_gb', 'N/A')} GB | Available: {telemetry.get('ram_avail_gb', 'N/A')} GB",
        ]

        if telemetry.get("cuda_available"):
            lines.extend([
                f"  • GPU Model         : {telemetry.get('gpu_name', 'Unknown')}",
                f"  • PyTorch / CUDA    : {telemetry.get('pytorch_version', 'N/A')} | CUDA {telemetry.get('cuda_version', 'N/A')}",
                f"  • GPU VRAM (GB)     : Total: {telemetry.get('vram_total_gb', 'N/A')} | Allocated: {telemetry.get('vram_allocated_gb', 'N/A')} | Reserved: {telemetry.get('vram_reserved_gb', 'N/A')}",
            ])

        if context:
            lines.append("Execution Context:")
            for k, v in context.items():
                lines.append(f"  • {k}: {sanitize_text(str(v))}")

        lines.extend([
            "",
            "Exception Details:",
            f"  • Class   : {exc_class_name}",
            f"  • Message : {sanitized_msg}",
            "",
            "Sanitized Stack Trace (LGPD Compliant):",
            sanitized_tb.rstrip(),
            "=" * 80,
            "\n"
        ])

        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines))

        return log_path
    except Exception:
        return None
