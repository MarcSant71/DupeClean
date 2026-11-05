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
    """Executa a varredura e processamento na GPU em background com telemetria em múltiplos estágios."""
    
    progress_status = Signal(str, int, int)                    # (mensagem, item_atual, total_itens)
    progress_stage = Signal(str, int, int, int, int)           # (mensagem, item_atual, total_itens, estagio_num, total_estagios)
    scan_finished = Signal(list)                               # (List[DuplicateGroup])
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
            total_stages = 3 if self.mode == ScanMode.EXACT else 4
            cache = CacheManager()

            # No Estágio 1 (descoberta), o total é 0 para manter a animação fluida e sem ETA redundante
            self.progress_stage.emit("Scanning folders...", 0, 0, 1, total_stages)

            # 1. Rastreamento recursivo de arquivos no disco (Modo Descoberta - Estágio 1)
            def _crawl_cb(msg: str, count: int, tot: int):
                self.progress_stage.emit(msg, count, 0, 1, total_stages)

            records = crawl_directories(
                scan_paths=self.scan_paths,
                excluded_paths=self.excluded_paths,
                mode=self.mode,
                progress_callback=_crawl_cb,
                stop_check=self.isInterruptionRequested
            )

            if self.isInterruptionRequested():
                return

            if not records:
                self.scan_finished.emit([])
                return

            cache.save_folder_counts(self.scan_paths, len(records))
            self.progress_stage.emit(f"{len(records):,} files found.", len(records), len(records), 1, total_stages)

            # 2. Execução conforme o Modo Selecionado com Cache Persistente e Estágios
            groups: List[DuplicateGroup] = []

            if self.mode == ScanMode.EXACT:
                def _exact_cb(msg: str, curr: int, tot: int):
                    # Identifica se é estágio de agrupamento por tamanho ou hashing de conteúdo
                    st_num = 2 if ("size" in msg.lower() or "candidat" in msg.lower()) else 3
                    self.progress_stage.emit(msg, curr, tot, st_num, 3)

                groups = find_exact_duplicates(
                    records=records,
                    progress_callback=_exact_cb,
                    stop_check=self.isInterruptionRequested,
                    cache_manager=cache
                )

            elif self.mode == ScanMode.IMAGES:
                def _image_cb(msg: str, curr: int, tot: int):
                    # Se for mensagem de cache -> Estágio 2/4. Se for processamento de fotos -> Estágio 3/4
                    st_num = 2 if "cache" in msg.lower() else 3
                    self.progress_stage.emit(msg, curr, tot, st_num, 4)

                valid_records, embeddings = extract_image_embeddings(
                    records=records,
                    batch_size=256,
                    progress_callback=_image_cb,
                    stop_check=self.isInterruptionRequested,
                    cache_manager=cache
                )
                if not self.isInterruptionRequested() and valid_records:
                    self.progress_stage.emit("Clustering similar photos on RTX 5090...", 0, len(valid_records), 4, 4)
                    groups = cluster_by_similarity(
                        records=valid_records,
                        embeddings=embeddings,
                        similarity_threshold=self.similarity_threshold,
                        mode=ScanMode.IMAGES,
                        stop_check=self.isInterruptionRequested
                    )
                    self.progress_stage.emit("Clustering complete.", len(valid_records), len(valid_records), 4, 4)

            elif self.mode == ScanMode.VIDEOS:
                def _video_cb(msg: str, curr: int, tot: int):
                    st_num = 2 if "cache" in msg.lower() else 3
                    self.progress_stage.emit(msg, curr, tot, st_num, 4)

                valid_records, embeddings = extract_video_embeddings(
                    records=records,
                    progress_callback=_video_cb,
                    stop_check=self.isInterruptionRequested,
                    cache_manager=cache
                )
                if not self.isInterruptionRequested() and valid_records:
                    self.progress_stage.emit("Clustering similar videos on RTX 5090...", 0, len(valid_records), 4, 4)
                    groups = cluster_by_similarity(
                        records=valid_records,
                        embeddings=embeddings,
                        similarity_threshold=self.similarity_threshold,
                        mode=ScanMode.VIDEOS,
                        stop_check=self.isInterruptionRequested
                    )
                    self.progress_stage.emit("Clustering complete.", len(valid_records), len(valid_records), 4, 4)

            if not self.isInterruptionRequested():
                self.scan_finished.emit(groups)

        except Exception as e:
            self.scan_error.emit(f"Error during scan: {str(e)}")
        finally:
            if not self.isInterruptionRequested():
                try:
                    cache.checkpoint()
                except Exception:
                    pass
