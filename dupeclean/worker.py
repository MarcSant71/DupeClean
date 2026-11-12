"""
Worker multithread (QThread) para escaneamento assíncrono sem congelar a interface gráfica.
"""

from pathlib import Path
from typing import List, Optional, Set
import shutil
import send2trash
import torch
from PySide6.QtCore import QThread, Signal

from dupeclean.core.models import ScanPath, ScanMode, DuplicateGroup
from dupeclean.core.scanner import crawl_directories
from dupeclean.core.hasher_exact import find_exact_duplicates
from dupeclean.core.hasher_image import extract_image_embeddings
from dupeclean.core.hasher_video import extract_video_embeddings
from dupeclean.core.matcher import cluster_by_similarity, filter_reference_groups
from dupeclean.core.cache import CacheManager


class ScanWorker(QThread):
    """Executa a varredura e processamento na GPU em background com telemetria em múltiplos estágios."""
    
    progress_status = Signal(str, "qint64", "qint64")                    # (mensagem, item_atual, total_itens)
    progress_stage = Signal(str, "qint64", "qint64", int, int)           # (mensagem, item_atual, total_itens, estagio_num, total_estagios)
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
                def _exact_cb(msg: str, curr: int, tot: int, st_num: int = 2):
                    self.progress_stage.emit(msg, curr, tot, st_num, 3)

                groups = find_exact_duplicates(
                    records=records,
                    progress_callback=_exact_cb,
                    stop_check=self.isInterruptionRequested,
                    cache_manager=cache
                )

            elif self.mode == ScanMode.IMAGES:
                from dupeclean.core.hardware import get_optimal_gpu_budgets
                budgets = get_optimal_gpu_budgets()
                img_batch = budgets.get("image_batch_size", 256)

                def _image_cb(msg: str, curr: int, tot: int):
                    st_num = 2 if "cache" in msg.lower() else 3
                    self.progress_stage.emit(msg, curr, tot, st_num, 4)

                valid_records, embeddings = extract_image_embeddings(
                    records=records,
                    batch_size=img_batch,
                    progress_callback=_image_cb,
                    stop_check=self.isInterruptionRequested,
                    cache_manager=cache
                )
                if not self.isInterruptionRequested() and valid_records:
                    def _cluster_img_cb(msg: str, curr: int, tot: int):
                        self.progress_stage.emit(msg, curr, tot, 4, 4)

                    groups = cluster_by_similarity(
                        records=valid_records,
                        embeddings=embeddings,
                        similarity_threshold=self.similarity_threshold,
                        mode=ScanMode.IMAGES,
                        stop_check=self.isInterruptionRequested,
                        progress_callback=_cluster_img_cb
                    )
                    self.progress_stage.emit("Grouping complete.", len(valid_records), len(valid_records), 4, 4)
                del embeddings
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()

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
                    def _cluster_vid_cb(msg: str, curr: int, tot: int):
                        self.progress_stage.emit(msg, curr, tot, 4, 4)

                    groups = cluster_by_similarity(
                        records=valid_records,
                        embeddings=embeddings,
                        similarity_threshold=self.similarity_threshold,
                        mode=ScanMode.VIDEOS,
                        stop_check=self.isInterruptionRequested,
                        progress_callback=_cluster_vid_cb
                    )
                    self.progress_stage.emit("Grouping complete.", len(valid_records), len(valid_records), 4, 4)
                del embeddings
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()

            if not self.isInterruptionRequested():
                groups = filter_reference_groups(groups)
                self.scan_finished.emit(groups)

        except Exception as e:
            try:
                from dupeclean.core.crash_logger import log_critical_crash
                ctx = {
                    "Scan Mode": getattr(self.mode, "name", str(self.mode)),
                    "Files Count": f"{len(records):,}" if "records" in locals() and records else "0"
                }
                log_critical_crash(e, context=ctx)
            except Exception:
                pass
            self.scan_error.emit(f"Error during scan: {str(e)}")
        finally:
            if not self.isInterruptionRequested():
                try:
                    cache.checkpoint()
                except Exception:
                    pass


class FileActionWorker(QThread):
    """Executa ações de arquivos (lixeira, exclusão permanente ou movimentação) em background sem congelar a UI."""

    progress = Signal(str, "qint64", "qint64")  # (status_msg, current_count, total_count)
    finished_action = Signal(set, list)        # (deleted_paths_set, errors_list)

    def __init__(
        self,
        paths: List[Path],
        action: str,  # 'trash', 'delete', 'move'
        dest_dir: Optional[Path] = None,
        parent=None
    ):
        super().__init__(parent)
        self.paths = paths
        self.action = action
        self.dest_dir = dest_dir

    def run(self):
        deleted_set: Set[Path] = set()
        errors: List[str] = []
        total = len(self.paths)

        verb_map = {
            "trash": "Moving to Recycle Bin",
            "delete": "Permanently deleting",
            "move": "Moving files"
        }
        verb = verb_map.get(self.action, "Processing")

        for idx, path in enumerate(self.paths, 1):
            if self.isInterruptionRequested():
                break

            try:
                if self.action == "trash":
                    send2trash.send2trash(str(path))
                    deleted_set.add(path)
                elif self.action == "delete":
                    path.unlink(missing_ok=True)
                    deleted_set.add(path)
                elif self.action == "move" and self.dest_dir:
                    target = self.dest_dir / path.name
                    if target.exists():
                        stem = path.stem
                        suffix = path.suffix
                        c = 1
                        while target.exists():
                            target = self.dest_dir / f"{stem}_dup{c}{suffix}"
                            c += 1
                    shutil.move(str(path), str(target))
                    deleted_set.add(path)
            except Exception as e:
                errors.append(f"{path.name}: {str(e)}")

            # Emite telemetria periódica (a cada 20 arquivos ou nos extremos) para manter o event loop leve e 60fps
            if idx % 20 == 0 or idx == total or idx == 1:
                self.progress.emit(f"{verb} ({idx:,} / {total:,}) - {path.name}", idx, total)

        self.finished_action.emit(deleted_set, errors)
