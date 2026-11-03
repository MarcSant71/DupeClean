"""
Worker multithread (QThread) para escaneamento assíncrono sem congelar a interface gráfica.
"""

from pathlib import Path
from typing import List
from PySide6.QtCore import QThread, Signal

from dupeclean.core.models import ScanPath, ScanMode, DuplicateGroup
from dupeclean.core.scanner import crawl_directories
from dupeclean.core.hasher_exact import find_exact_duplicates
from dupeclean.core.hasher_image import extract_image_embeddings
from dupeclean.core.hasher_video import extract_video_embeddings
from dupeclean.core.matcher import cluster_by_similarity
from dupeclean.core.cache import CacheManager


class ScanWorker(QThread):
    """Executa a varredura e processamento na GPU em background."""
    
    progress_status = Signal(str, int, int)  # (mensagem, item_atual, total_itens)
    scan_finished = Signal(list)            # (List[DuplicateGroup])
    scan_error = Signal(str)

    def __init__(
        self,
        scan_paths: List[ScanPath],
        excluded_paths: List[Path],
        mode: ScanMode,
        similarity_threshold: float = 0.85,
        parent=None
    ):
        super().__init__(parent)
        self.scan_paths = scan_paths
        self.excluded_paths = excluded_paths
        self.mode = mode
        self.similarity_threshold = similarity_threshold

    def run(self):
        try:
            self.progress_status.emit("Rastreando pastas no disco...", 0, 0)

            # 1. Rastreamento
            records = crawl_directories(
                scan_paths=self.scan_paths,
                excluded_paths=self.excluded_paths,
                mode=self.mode,
                progress_callback=lambda msg, count: self.progress_status.emit(msg, count, 0),
                stop_check=self.isInterruptionRequested
            )

            if self.isInterruptionRequested():
                return

            if not records:
                self.scan_finished.emit([])
                return

            self.progress_status.emit(f"{len(records)} arquivos encontrados. Iniciando deduplicação...", 0, len(records))

            # 2. Execução conforme o Modo Selecionado com Cache Persistente
            groups: List[DuplicateGroup] = []
            cache = CacheManager()

            if self.mode == ScanMode.EXACT:
                groups = find_exact_duplicates(
                    records=records,
                    progress_callback=lambda msg, curr, tot: self.progress_status.emit(msg, curr, tot),
                    stop_check=self.isInterruptionRequested,
                    cache_manager=cache
                )

            elif self.mode == ScanMode.IMAGES:
                valid_records, embeddings = extract_image_embeddings(
                    records=records,
                    batch_size=512,
                    progress_callback=lambda msg, curr, tot: self.progress_status.emit(msg, curr, tot),
                    stop_check=self.isInterruptionRequested,
                    cache_manager=cache
                )
                if not self.isInterruptionRequested() and valid_records:
                    self.progress_status.emit("Comparando fotos...", 0, len(valid_records))
                    groups = cluster_by_similarity(
                        records=valid_records,
                        embeddings=embeddings,
                        similarity_threshold=self.similarity_threshold,
                        mode=ScanMode.IMAGES
                    )

            elif self.mode == ScanMode.VIDEOS:
                valid_records, embeddings = extract_video_embeddings(
                    records=records,
                    progress_callback=lambda msg, curr, tot: self.progress_status.emit(msg, curr, tot),
                    stop_check=self.isInterruptionRequested,
                    cache_manager=cache
                )
                if not self.isInterruptionRequested() and valid_records:
                    self.progress_status.emit("Comparando vídeos...", 0, len(valid_records))
                    groups = cluster_by_similarity(
                        records=valid_records,
                        embeddings=embeddings,
                        similarity_threshold=self.similarity_threshold,
                        mode=ScanMode.VIDEOS
                    )

            if not self.isInterruptionRequested():
                self.scan_finished.emit(groups)

        except Exception as e:
            self.scan_error.emit(f"Erro durante a execução: {str(e)}")
