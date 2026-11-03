"""
Modelos de dados para o DupeClean.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Any
from enum import Enum


class ScanMode(Enum):
    EXACT = "exact"      # 100% idênticos (xxhash)
    IMAGES = "images"    # Imagens similares (GPU Tensor Hash / Embedding)
    VIDEOS = "videos"    # Vídeos similares (NVDEC HWAccel + GPU Frame Sampling)


@dataclass
class ScanPath:
    path: Path
    is_reference: bool = False  # Se True, arquivos NUNCA são sugeridos para deleção


@dataclass
class FileRecord:
    path: Path
    size: int
    mtime: float
    is_reference: bool = False
    name: str = ""
    ext: str = ""
    hash_value: Optional[str] = None
    embedding: Optional[Any] = None  # torch.Tensor na GPU ou CPU

    def __post_init__(self):
        if not self.name:
            self.name = self.path.name
        if not self.ext:
            self.ext = self.path.suffix.lower()


@dataclass
class DuplicateGroup:
    group_id: int
    files: List[FileRecord] = field(default_factory=list)
    similarity: float = 1.0  # 1.0 = 100% idêntico
    kind: ScanMode = ScanMode.EXACT

    @property
    def reclaimable_bytes(self) -> int:
        """Calcula espaço recuperável deixando pelo menos 1 arquivo intacto."""
        if len(self.files) <= 1:
            return 0
        sizes = [f.size for f in self.files]
        # Deixa o maior ou o primeiro preservado
        return sum(sizes) - max(sizes)


@dataclass
class ScanProgress:
    stage: str
    current_item: int
    total_items: int
    current_path: str = ""
    message: str = ""
