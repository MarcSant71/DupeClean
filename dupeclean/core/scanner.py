"""
Rastreador de arquivos de alto desempenho (Filesystem Crawler).
Varre diretórios de forma recursiva e aplica filtros de exclusão e extensão.
"""

import os
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
    ".ogv", ".m2v", ".m4b"
}


def crawl_directories(
    scan_paths: List[ScanPath],
    excluded_paths: List[Path],
    mode: ScanMode,
    progress_callback: Optional[Callable[[str, int], None]] = None,
    stop_check: Optional[Callable[[], bool]] = None
) -> List[FileRecord]:
    """
    Rastreia todos os diretórios incluídos, marcando a flag 'is_reference'
    e respeitando os caminhos excluídos.
    """
    records: List[FileRecord] = []
    seen_paths: Set[str] = set()

    # Normalizar exclusões
    resolved_exclusions = [p.resolve() for p in excluded_paths if p.exists()]

    total_scanned = 0

    for sp in scan_paths:
        root_dir = sp.path.resolve()
        if not root_dir.exists():
            continue

        is_ref = sp.is_reference

        # os.walk é altamente otimizado no Windows
        for root, dirs, files in os.walk(root_dir, topdown=True):
            if stop_check and stop_check():
                break

            current_dir_path = Path(root).resolve()

            # Pular se estiver em pasta excluída
            if any(current_dir_path == exc or exc in current_dir_path.parents for exc in resolved_exclusions):
                dirs.clear()  # Não desce nas subpastas
                continue

            for file_name in files:
                if stop_check and stop_check():
                    break

                full_path = current_dir_path / file_name
                path_str = str(full_path).lower()

                if path_str in seen_paths:
                    continue

                ext = full_path.suffix.lower()

                # Filtrar pelo modo
                if mode == ScanMode.IMAGES and ext not in IMAGE_EXTENSIONS:
                    continue
                elif mode == ScanMode.VIDEOS and ext not in VIDEO_EXTENSIONS:
                    continue

                try:
                    stat = full_path.stat()
                    size = stat.st_size

                    # Ignora arquivos de 0 bytes
                    if size == 0:
                        continue

                    records.append(FileRecord(
                        path=full_path,
                        size=size,
                        mtime=stat.st_mtime,
                        is_reference=is_ref,
                        name=file_name,
                        ext=ext
                    ))
                    seen_paths.add(path_str)
                    total_scanned += 1

                    if progress_callback and total_scanned % 500 == 0:
                        progress_callback(f"Crawling files ({total_scanned} found)...", total_scanned)

                except (OSError, PermissionError):
                    # Arquivo inacessível ou sem permissão
                    continue

    return records
