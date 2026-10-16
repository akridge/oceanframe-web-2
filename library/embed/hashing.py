"""
Dependency-free image descriptor.

This is the fallback embedder: it needs nothing beyond numpy + Pillow, so the
library indexes, dedupes, and does image-to-image similarity on any machine,
including CI and a workstation with no GPU.  It cannot do text search — that is
what the CLIP backend is for.

The 256-d descriptor is four blocks, each L2-normalised on its own so that no
single block dominates the cosine, then concatenated and normalised again:

    [0:64]    low-frequency DCT of the 32x32 luma (structure / layout)
    [64:112]  HSV histogram, 24 hue x 12 sat x 12 val marginals (colour)
    [112:208] 4x4 spatial grid x 6 stats (local colour + texture)
    [208:256] 6-cell x 8-bin gradient orientation histogram (edges)
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from library.embed.base import Embedder, l2_normalise

_GRID = 4
_DCT_N = 32
_DCT_K = 8


def _dct_matrix(n: int) -> np.ndarray:
    """Orthonormal DCT-II matrix; avoids a scipy dependency."""
    k = np.arange(n)[:, None]
    i = np.arange(n)[None, :]
    mat = np.cos(np.pi * (2 * i + 1) * k / (2 * n))
    mat[0] *= np.sqrt(1.0 / n)
    mat[1:] *= np.sqrt(2.0 / n)
    return mat.astype(np.float32)


_DCT = _dct_matrix(_DCT_N)


def _block_norm(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 1e-8 else vec


def _structure(gray32: np.ndarray) -> np.ndarray:
    coeffs = _DCT @ gray32 @ _DCT.T
    block = coeffs[:_DCT_K, :_DCT_K].flatten()
    block[0] = 0.0                              # drop DC: brightness lives elsewhere
    return _block_norm(np.tanh(block / 64.0))


def _colour(rgb: np.ndarray) -> np.ndarray:
    small = Image.fromarray((rgb * 255).astype(np.uint8)).convert("HSV")
    hsv = np.asarray(small, dtype=np.float32) / 255.0
    h_hist, _ = np.histogram(hsv[..., 0], bins=24, range=(0.0, 1.0))
    s_hist, _ = np.histogram(hsv[..., 1], bins=12, range=(0.0, 1.0))
    v_hist, _ = np.histogram(hsv[..., 2], bins=12, range=(0.0, 1.0))
    hist = np.concatenate([h_hist, s_hist, v_hist]).astype(np.float32)
    return _block_norm(np.sqrt(hist / max(hsv[..., 0].size, 1)))


def _grid_stats(rgb: np.ndarray, gray: np.ndarray) -> np.ndarray:
    h, w = gray.shape
    ys = np.linspace(0, h, _GRID + 1).astype(int)
    xs = np.linspace(0, w, _GRID + 1).astype(int)
    gy, gx = np.gradient(gray)
    mag = np.hypot(gy, gx)
    maxc = rgb.max(axis=2)
    minc = rgb.min(axis=2)
    sat = (maxc - minc) / np.maximum(maxc, 1e-6)

    feats = []
    for r in range(_GRID):
        for c in range(_GRID):
            cell_rgb = rgb[ys[r]:ys[r + 1], xs[c]:xs[c + 1]]
            cell_g = gray[ys[r]:ys[r + 1], xs[c]:xs[c + 1]]
            cell_m = mag[ys[r]:ys[r + 1], xs[c]:xs[c + 1]]
            cell_s = sat[ys[r]:ys[r + 1], xs[c]:xs[c + 1]]
            if cell_g.size == 0:
                feats.extend([0.0] * 6)
                continue
            feats.extend([
                float(cell_rgb[..., 0].mean()),
                float(cell_rgb[..., 1].mean()),
                float(cell_rgb[..., 2].mean()),
                float(cell_m.mean()),
                float(cell_g.std()),
                float(cell_s.mean()),
            ])
    return _block_norm(np.asarray(feats, dtype=np.float32))


def _orientations(gray: np.ndarray) -> np.ndarray:
    gy, gx = np.gradient(gray)
    mag = np.hypot(gy, gx)
    ang = (np.arctan2(gy, gx) + np.pi) / (2 * np.pi)     # 0..1
    h, w = gray.shape
    ys = np.linspace(0, h, 3).astype(int)
    xs = np.linspace(0, w, 3).astype(int)
    out = []
    for r in range(2):
        for c in range(2):
            cell_a = ang[ys[r]:ys[r + 1], xs[c]:xs[c + 1]].ravel()
            cell_m = mag[ys[r]:ys[r + 1], xs[c]:xs[c + 1]].ravel()
            hist, _ = np.histogram(cell_a, bins=8, range=(0.0, 1.0), weights=cell_m)
            out.append(hist)
    # Two extra whole-image cells (top half / bottom half) capture the
    # horizon-like structure common in transect imagery.
    for half in (ang[: h // 2], ang[h // 2:]):
        hist, _ = np.histogram(half.ravel(), bins=8, range=(0.0, 1.0))
        out.append(hist)
    return _block_norm(np.sqrt(np.concatenate(out).astype(np.float32)))


class HashingEmbedder(Embedder):
    name = "hash"
    dim = 256
    supports_text = False

    def embed_images(self, images: list[Image.Image]) -> np.ndarray:
        rows = np.zeros((len(images), self.dim), dtype=np.float32)
        for i, img in enumerate(images):
            rgb_img = img.convert("RGB").resize((_DCT_N, _DCT_N), Image.BILINEAR)
            rgb = np.asarray(rgb_img, dtype=np.float32) / 255.0
            gray = rgb @ np.asarray([0.299, 0.587, 0.114], dtype=np.float32)
            rows[i] = np.concatenate([
                _structure(gray * 255.0),
                _colour(rgb),
                _grid_stats(rgb, gray),
                _orientations(gray),
            ])
        return l2_normalise(rows)
