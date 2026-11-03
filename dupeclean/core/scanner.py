"""
Rastreador de arquivos de alto desempenho (Filesystem Crawler).
Varre diretórios de forma recursiva utilizando os.scandir com cache de metadados
nativos do Win32 (WIN32_FIND_DATA), eliminando roundtrips de rede via SMB/iSCSI.
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
    e respeitando os caminhos excluídos. Utiliza os.scandir de alto rendimento.
    """
    records: List[FileRecord] = []
    seen_paths: Set[str] = set()

    # Normalizar exclusões
    resolved_exclusions = [p.resolve() for p in excluded_paths if p.exists()]
    total_scanned = 0

    def _crawl_folder(folder_path: Path, is_ref: bool):
        nonlocal total_scanned
        if stop_check and stop_check():
            return

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
                            # Checa exclusões
                            if not any(sub_p == exc or exc in sub_p.parents for exc in resolved_exclusions):
                                subdirs.append(sub_p)

                        elif entry.is_file(follow_symlinks=False):
                            path_str = entry.path.lower()
                            if path_str in seen_paths:
                                continue

                            ext = os.path.splitext(entry.name)[1].lower()

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

                            if progress_callback and total_scanned % 1000 == 0:
                                progress_callback(f"Indexing files ({total_scanned})...", total_scanned)

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
        root_dir = sp.path.resolve()
        if not root_dir.exists():
            continue

        _crawl_folder(root_dir, sp.is_reference)

    if progress_callback:
        progress_callback(f"Indexing completed: {total_scanned} files.", total_scanned)

    return records
