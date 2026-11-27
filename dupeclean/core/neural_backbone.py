"""
Self-contained, offline DINOv2 ViT-S/14 visual backbone for DupeClean.
- Pure PyTorch with native F.scaled_dot_product_attention (FlashAttention / Tensor Cores on RTX 30/40/50).
- Zero external dependencies (no timm, no xformers, no torch.hub internet access).
- Loads compact 42 MB FP16 weights from models/dinov2_vits14_fp16.pt (portable & offline).
- Supports universal CPU fallback (FP32) producing cache-portable embeddings identical to RTX FP16.
"""

import math
import sys
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F

NEURAL_INPUT_SIZE = 224
NEURAL_DIM = 384

_IMAGENET_MEAN = (0.485, 0.456, 0.406)
_IMAGENET_STD = (0.229, 0.224, 0.225)


class _PatchEmbed(nn.Module):
    def __init__(self, img_size: int = 224, patch_size: int = 14, in_chans: int = 3, embed_dim: int = 384):
        super().__init__()
        self.patch_size = patch_size
        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=patch_size, stride=patch_size)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.proj(x).flatten(2).transpose(1, 2)


class _Attention(nn.Module):
    def __init__(self, dim: int = 384, num_heads: int = 6):
        super().__init__()
        self.num_heads = num_heads
        self.head_dim = dim // num_heads
        self.qkv = nn.Linear(dim, dim * 3, bias=True)
        self.proj = nn.Linear(dim, dim, bias=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        b, n, c = x.shape
        qkv = self.qkv(x).reshape(b, n, 3, self.num_heads, self.head_dim).permute(2, 0, 3, 1, 4)
        q, k, v = qkv.unbind(0)
        # Native FlashAttention / Memory-Efficient Tensor Core attention (zero xformers required)
        out = F.scaled_dot_product_attention(q, k, v)
        out = out.transpose(1, 2).reshape(b, n, c)
        return self.proj(out)


class _Mlp(nn.Module):
    def __init__(self, dim: int = 384, hidden_dim: int = 1536):
        super().__init__()
        self.fc1 = nn.Linear(dim, hidden_dim, bias=True)
        self.fc2 = nn.Linear(hidden_dim, dim, bias=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.fc2(F.gelu(self.fc1(x)))


class _LayerScale(nn.Module):
    def __init__(self, dim: int = 384):
        super().__init__()
        self.gamma = nn.Parameter(torch.ones(dim))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x * self.gamma


class _Block(nn.Module):
    def __init__(self, dim: int = 384, num_heads: int = 6):
        super().__init__()
        self.norm1 = nn.LayerNorm(dim, eps=1e-6)
        self.attn = _Attention(dim=dim, num_heads=num_heads)
        self.ls1 = _LayerScale(dim)
        self.norm2 = nn.LayerNorm(dim, eps=1e-6)
        self.mlp = _Mlp(dim=dim, hidden_dim=dim * 4)
        self.ls2 = _LayerScale(dim)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self.ls1(self.attn(self.norm1(x)))
        x = x + self.ls2(self.mlp(self.norm2(x)))
        return x


class ViTSmallPatch14(nn.Module):
    """Exact architectural match for Meta DINOv2 ViT-S/14 (384-D CLS visual descriptor)."""

    def __init__(self, img_size: int = 224, patch_size: int = 14, embed_dim: int = 384, depth: int = 12, num_heads: int = 6):
        super().__init__()
        self.patch_size = patch_size
        self.embed_dim = embed_dim
        self.patch_embed = _PatchEmbed(img_size=img_size, patch_size=patch_size, embed_dim=embed_dim)
        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        # DINOv2 pretrain checkpoint stores pos_embed at 518x518 (37x37 + 1 = 1370 tokens);
        # we interpolate dynamically to target grid (16x16 + 1 = 257 tokens for 224x224) at load time.
        num_patches = (img_size // patch_size) ** 2
        self.pos_embed = nn.Parameter(torch.zeros(1, num_patches + 1, embed_dim))
        self.blocks = nn.ModuleList([_Block(dim=embed_dim, num_heads=num_heads) for _ in range(depth)])
        self.norm = nn.LayerNorm(embed_dim, eps=1e-6)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.patch_embed(x)
        cls_tok = self.cls_token.expand(x.shape[0], -1, -1)
        x = torch.cat((cls_tok, x), dim=1) + self.pos_embed
        for blk in self.blocks:
            x = blk(x)
        x = self.norm(x)
        return x[:, 0]


def interpolate_pos_embed(pos_embed: torch.Tensor, target_grid: int = 16) -> torch.Tensor:
    """Bicubic-interpolate DINOv2 positional embeddings (e.g. 37x37 -> 16x16) once at export/load."""
    cls_pos = pos_embed[:, :1, :]
    patch_pos = pos_embed[:, 1:, :]
    num_tokens = patch_pos.shape[1]
    src_grid = int(round(math.sqrt(num_tokens)))
    if src_grid == target_grid:
        return pos_embed
    dim = patch_pos.shape[-1]
    # Matches official DINOv2 bicubic + 0.1 antialias offset convention
    patch_2d = patch_pos.float().reshape(1, src_grid, src_grid, dim).permute(0, 3, 1, 2)
    scale = (target_grid + 0.1) / src_grid
    patch_interp = F.interpolate(patch_2d, scale_factor=scale, mode="bicubic", align_corners=False)
    patch_interp = patch_interp[:, :, :target_grid, :target_grid]
    patch_out = patch_interp.permute(0, 2, 3, 1).reshape(1, target_grid * target_grid, dim).to(pos_embed.dtype)
    return torch.cat((cls_pos, patch_out), dim=1)


def get_backbone_weights_path() -> Path:
    """Resolve the bundled offline weights file path (works both from source and PyInstaller frozen builds)."""
    candidates = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        candidates.append(exe_dir / "models" / "dinov2_vits14_fp16.pt")
        candidates.append(exe_dir / "lib" / "models" / "dinov2_vits14_fp16.pt")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "models" / "dinov2_vits14_fp16.pt")
    repo_root = Path(__file__).resolve().parent.parent.parent
    candidates.append(repo_root / "models" / "dinov2_vits14_fp16.pt")

    for p in candidates:
        if p.is_file():
            return p
    return candidates[-1]


_MODEL_LOCK = threading.Lock()
_MODEL_CACHE: Dict[Tuple[str, str], Tuple[ViTSmallPatch14, torch.Tensor, torch.Tensor]] = {}


def is_neural_backbone_available() -> bool:
    """Return True if the offline DINOv2 FP16 weights file is present on disk."""
    return get_backbone_weights_path().is_file()


def get_neural_backbone(device: str, dtype: torch.dtype) -> Optional[Tuple[ViTSmallPatch14, torch.Tensor, torch.Tensor]]:
    """
    Lazily load and cache the offline ViTSmallPatch14 model on `device` with `dtype`
    (FP16 on RTX CUDA, FP32 on CPU fallback). Returns (model, mean_tensor, std_tensor) or None.
    """
    cache_key = (str(device), str(dtype))
    cached = _MODEL_CACHE.get(cache_key)
    if cached is not None:
        return cached

    weights_path = get_backbone_weights_path()
    if not weights_path.is_file():
        return None

    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(cache_key)
        if cached is not None:
            return cached

        state_dict = torch.load(str(weights_path), map_location="cpu", weights_only=True)
        # Drop unused mask_token if exported from raw checkpoint
        state_dict.pop("mask_token", None)
        if "pos_embed" in state_dict and state_dict["pos_embed"].shape[1] != 257:
            state_dict["pos_embed"] = interpolate_pos_embed(state_dict["pos_embed"], target_grid=16)

        model = ViTSmallPatch14(img_size=NEURAL_INPUT_SIZE, patch_size=14, embed_dim=NEURAL_DIM, depth=12, num_heads=6)
        model.load_state_dict(state_dict, strict=True)
        model = model.eval().to(device=device, dtype=dtype)
        for p in model.parameters():
            p.requires_grad_(False)

        mean_t = torch.tensor(_IMAGENET_MEAN, device=device, dtype=torch.float32).view(1, 3, 1, 1)
        std_t = torch.tensor(_IMAGENET_STD, device=device, dtype=torch.float32).view(1, 3, 1, 1)
        result = (model, mean_t, std_t)
        _MODEL_CACHE[cache_key] = result
        return result


def compute_hybrid_visual_embeddings(
    t_rgb_224: torch.Tensor,
    dct64: torch.Tensor,
    dct64_t: torch.Tensor,
    sw16: torch.Tensor,
    sw8: torch.Tensor,
    backbone_bundle: Tuple[ViTSmallPatch14, torch.Tensor, torch.Tensor],
    out_dtype: torch.dtype,
    w_dino: float = 0.60,
    w_dct: float = 0.30,
    w_col: float = 0.10,
) -> torch.Tensor:
    """
    Compute compact, high-precision 768-D Hybrid Visual Embeddings in FP32 and cast to `out_dtype`:
    - [0:384]   DINOv2-ViT-S/14 CLS semantic/copy-detection embedding (crop/watermark/burst resilient)
    - [384:768] YCbCr 64x64 2D DCT (Y 16x16 + Cb 8x8 + Cr 8x8, spectral whitening ALPHA=1.2)
                with signed global RGB mean in the 3 zeroed DC slots (0, 256, 320).
    Input `t_rgb_224`: float32 [B, 3, 224, 224] in [0.0, 1.0] on target compute device.
    """
    model, mean_t, std_t = backbone_bundle
    model_dtype = next(model.parameters()).dtype

    # 1. Neural semantic branch (FP16 Tensor Cores on RTX, FP32 on CPU)
    t_norm = ((t_rgb_224 - mean_t) / std_t).to(dtype=model_dtype)
    cls_raw = model(t_norm).float()
    cls_v = F.normalize(cls_raw, p=2, dim=1, eps=1e-6)  # [B, 384]

    # 2. Spectral YCbCr 2D DCT branch (64x64 anti-aliased area pool)
    t_64 = F.interpolate(t_rgb_224, size=(64, 64), mode="area")
    r_ch, g_ch, b_ch = t_64[:, 0], t_64[:, 1], t_64[:, 2]
    y_ch = 0.299 * r_ch + 0.587 * g_ch + 0.114 * b_ch
    cb_ch = -0.168736 * r_ch - 0.331264 * g_ch + 0.5 * b_ch
    cr_ch = 0.5 * r_ch - 0.418688 * g_ch - 0.081312 * b_ch

    dy = torch.matmul(dct64, torch.matmul(y_ch, dct64_t))[:, :16, :16]
    dcb = torch.matmul(dct64, torch.matmul(cb_ch, dct64_t))[:, :8, :8]
    dcr = torch.matmul(dct64, torch.matmul(cr_ch, dct64_t))[:, :8, :8]

    # Solid-canvas guard: attenuate ViT blank-canvas anisotropy on flat solid images (e.g. pure black vs white)
    ac_energy = (dy.abs() * (sw16 > 0)).mean(dim=(1, 2))
    non_solid = (ac_energy > 0.015).float().unsqueeze(1)

    b_size = t_rgb_224.shape[0]
    dct_384 = torch.cat(
        (
            (dy * sw16).reshape(b_size, -1),
            (dcb * sw8).reshape(b_size, -1),
            (dcr * sw8).reshape(b_size, -1),
        ),
        dim=1,
    )
    dct_norm = F.normalize(dct_384, p=2, dim=1, eps=1e-6)

    # 3. Signed global RGB mean in the 3 guaranteed-zero DC slots (0, 256, 320)
    rgb_mean = t_64.mean(dim=(2, 3)) - 0.5  # [-0.5, +0.5]
    rgb_mean_norm = F.normalize(rgb_mean, p=2, dim=1, eps=1e-6)

    dct_weighted = dct_norm * math.sqrt(w_dct)
    col_scale = math.sqrt(w_col)
    dct_weighted[:, 0] = rgb_mean_norm[:, 0] * col_scale
    dct_weighted[:, 256] = rgb_mean_norm[:, 1] * col_scale
    dct_weighted[:, 320] = rgb_mean_norm[:, 2] * col_scale

    fused = torch.cat((cls_v * non_solid * math.sqrt(w_dino), dct_weighted), dim=1)
    fused_norm = F.normalize(fused, p=2, dim=1, eps=1e-6)
    return fused_norm.to(dtype=out_dtype)
