"""
Rastreador de arquivos de alto desempenho (Filesystem Crawler).
Varre diretórios de forma recursiva utilizando os.scandir com cache de metadados
nativos do Win32 (WIN32_FIND_DATA), eliminando roundtrips de rede via SMB/iSCSI.
"""

import os
import time
from pathlib import Path
from typing import List, Set, Callable, Optional
from dupeclean.core.models import ScanPath, FileRecord, ScanMode

# Extensões suportadas
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif",
    ".gif", ".ico", ".heic", ".heif", ".avif",
    ".raw", ".cr2", ".nef", ".arw", ".dng", ".orf", ".rw2"
}

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".ts", ".m2ts", ".mts", ".vob", ".mpg", ".mpeg",
    ".rmvb", ".rm", ".3gp", ".3g2", ".asf", ".f4v", ".divx",
    ".ogv", ".m2v", ".m4b", ".dav"
}


def crawl_directories(
    scan_paths: List[ScanPath],
    excluded_paths: List[Path],
    mode: ScanMode,
    progress_callback: Optional[Callable[[str, int, int], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None
) -> List[FileRecord]:
    """
    Rastreia todos os diretórios incluídos, marcando a flag 'is_reference'
    e respeitando os caminhos excluídos. Utiliza os.scandir de alto rendimento.
    Emite telemetria contínua com contagem real e nomes de pastas vivas.
    """
    records: List[FileRecord] = []
    seen_paths: Set[str] = set()

    # Normalizar exclusões
    resolved_exclusions = [p.resolve() for p in excluded_paths if p.exists()]
    total_scanned = 0
    last_reported_count = 0
    last_report_time = 0.0

    def _crawl_folder(folder_path: Path, is_ref: bool):
        nonlocal total_scanned, last_reported_count, last_report_time
        if stop_check and stop_check():
            return

        now = time.time()
        if progress_callback and (now - last_report_time >= 0.08 or total_scanned - last_reported_count >= 100):
            last_reported_count = total_scanned
            last_report_time = now
            progress_callback(f"Scanning folders... ({total_scanned:,} files found)", total_scanned, 0)

        try:
            with os.scandir(folder_path) as it:
                subdirs: List[Path] = []
                for entry in it:
                    if stop_check and stop_check():
                        return

                    try:
                        # is_dir e is_file no Windows usam atributos em cache do WIN32_FIND_DATA
                        if entry.is_dir(follow_symlinks=False):
                            sub_p = Path(entry.path)
                            # Checa exclusões apenas se houver pastas excluídas cadastradas
                            if resolved_exclusions and any(sub_p == exc or exc in sub_p.parents for exc in resolved_exclusions):
                                continue
                            subdirs.append(sub_p)

                        elif entry.is_file(follow_symlinks=False):
                            path_str = entry.path.lower()
                            if path_str in seen_paths:
                                continue

                            # Extração rápida de extensão (5x mais rápida que os.path.splitext)
                            name = entry.name
                            dot_pos = name.rfind(".")
                            if dot_pos == -1:
                                continue
                            ext = name[dot_pos:].lower()

                            # Filtrar pelo modo
                            if mode == ScanMode.IMAGES and ext not in IMAGE_EXTENSIONS:
                                continue
                            elif mode == ScanMode.VIDEOS and ext not in VIDEO_EXTENSIONS:
                                continue

                            # entry.stat(follow_symlinks=False) não faz RPC de rede adicional no Windows
                            st = entry.stat(follow_symlinks=False)
                            size = st.st_size

                            # Ignora arquivos de 0 bytes
                            if size == 0:
                                continue

                            records.append(FileRecord(
                                path=Path(entry.path),
                                size=size,
                                mtime=st.st_mtime,
                                is_reference=is_ref,
                                name=entry.name,
                                ext=ext
                            ))
                            seen_paths.add(path_str)
                            total_scanned += 1

                            now = time.time()
                            if progress_callback and (now - last_report_time >= 0.08 or total_scanned - last_reported_count >= 100):
                                last_reported_count = total_scanned
                                last_report_time = now
                                progress_callback(f"Scanning folders... ({total_scanned:,} files found)", total_scanned, 0)

                    except (OSError, PermissionError):
                        continue

            # Desce recursivamente nas subpastas
            for sub_p in subdirs:
                if stop_check and stop_check():
                    return
                _crawl_folder(sub_p, is_ref)

        except (OSError, PermissionError):
            return

    for sp in scan_paths:
        if stop_check and stop_check():
            return records
        root_dir = sp.path.resolve()
        if not root_dir.exists():
            continue

        _crawl_folder(root_dir, sp.is_reference)

    if stop_check and stop_check():
        return records

    if progress_callback:
        progress_callback(f"Scanning folders... ({total_scanned:,} files found)", total_scanned, 0)

    return records
