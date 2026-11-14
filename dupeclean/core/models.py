"""
Data models for DupeClean.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Any
from enum import Enum


def format_size(size_bytes: int) -> str:
    """Format byte counts into human-readable units."""
    val = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024.0:
            return f"{val:.2f} {unit}"
        val /= 1024.0
    return f"{val:.2f} PB"


class ScanMode(Enum):
    EXACT = "exact"      # 100% identical (xxhash)
    IMAGES = "images"    # Similar images (GPU Tensor Hash / Embedding)
    VIDEOS = "videos"    # Similar videos (NVDEC HWAccel + GPU Frame Sampling)


@dataclass
class ScanPath:
    path: Path
    is_reference: bool = False  # If True, files are NEVER suggested for deletion


@dataclass(slots=True)
class FileRecord:
    path: Path
    size: int
    mtime: float
    is_reference: bool = False
    name: str = ""
    ext: str = ""
    volume_id: int = 0
    file_id: int = 0
    prefix_hash: Optional[str] = None
    hash_value: Optional[str] = None
    embedding: Optional[Any] = None  # torch.Tensor on GPU or CPU
    duration: Optional[float] = None

    def __post_init__(self):
        if not self.name:
            self.name = self.path.name
        if not self.ext:
            self.ext = self.path.suffix.lower()


@dataclass
class DuplicateGroup:
    group_id: int
    files: List[FileRecord] = field(default_factory=list)
    similarity: float = 1.0  # 1.0 = 100% identical
    kind: ScanMode = ScanMode.EXACT

    @property
    def reclaimable_bytes(self) -> int:
        """Compute reclaimable space while keeping at least 1 file intact."""
        if len(self.files) <= 1:
            return 0
        ref_files = [f for f in self.files if f.is_reference]
        if ref_files:
            return sum(f.size for f in self.files if not f.is_reference)
        sizes = [f.size for f in self.files]
        # Keep the largest or first file intact
        return sum(sizes) - max(sizes)


@dataclass
class ScanProgress:
    stage: str
    current_item: int
    total_items: int
    current_path: str = ""
    message: str = ""
