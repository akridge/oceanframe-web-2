"""
Frame quality filters — exact ports of the JS browser app algorithms.

blur_score:   weighted Laplacian variance  (decode-worker.js ro/to)
compute_phash: 8x8 average hash → 8 bytes  (decode-worker.js io)
hamming:      bit-count of XOR             (app.js Xo)
filter_frames: two-pass blur + similarity  (app.js wc)
"""

from __future__ import annotations
import math
import numpy as np
from dataclasses import dataclass, field
from PIL import Image


@dataclass
class FrameData:
    index: int
    timestamp_ms: float
    blur_score: float
    phash: bytes          # 8 bytes = 64-bit perceptual hash
    thumbnail: Image.Image  # PIL Image, ~160px wide


# ── Blur ──────────────────────────────────────────────────────────────────────

def blur_score(gray: np.ndarray) -> float:
    """
    Center-weighted Laplacian variance.
    Matches decode-worker.js ro() exactly.
    Higher = sharper.  Lower = blurrier.
    """
    h, w = gray.shape
    n_interior = (w - 2) * (h - 2)
    stride = max(1, int(math.floor(math.sqrt(n_interior / 1000)))) if n_interior > 1000 else 1

    cx = (w - 1) / 2.0
    cy = (h - 1) / 2.0

    g = gray.astype(np.float32)

    ys = np.arange(1, h - 1, stride)
    xs = np.arange(1, w - 1, stride)

    # 4-connected Laplacian at all sampled interior points
    lap = (
        g[ys[:, None] - 1, xs[None, :]] +
        g[ys[:, None],     xs[None, :] - 1] +
        g[ys[:, None],     xs[None, :] + 1] +
        g[ys[:, None] + 1, xs[None, :]] -
        4.0 * g[ys[:, None], xs[None, :]]
    )

    # Center-distance weights:  w = 2 - max(|x-cx|/cx, |y-cy|/cy)
    norm_x = np.abs(xs - cx) / cx if cx > 0 else np.zeros_like(xs, dtype=np.float32)
    norm_y = np.abs(ys - cy) / cy if cy > 0 else np.zeros_like(ys, dtype=np.float32)
    weights = 2.0 - np.maximum(norm_x[np.newaxis, :], norm_y[:, np.newaxis])

    total_w = weights.sum()
    if total_w == 0:
        return 0.0

    mean_l  = (weights * lap).sum() / total_w
    var_l   = (weights * lap * lap).sum() / total_w - mean_l ** 2
    return float(var_l)


# ── Perceptual hash ────────────────────────────────────────────────────────────

def compute_phash(gray: np.ndarray) -> bytes:
    """
    8×8 average hash → 8-byte (64-bit) perceptual hash.
    Matches decode-worker.js io() exactly.
    """
    import cv2
    small = cv2.resize(gray, (8, 8), interpolation=cv2.INTER_AREA).astype(np.float32)
    mean  = small.mean()
    bits  = (small.flatten() > mean).astype(np.uint8)
    return bytes(np.packbits(bits).tolist())


def hamming(a: bytes, b: bytes) -> int:
    """Hamming distance between two 8-byte hashes. Matches app.js Xo()."""
    return sum(bin(x ^ y).count('1') for x, y in zip(a, b))


# ── Frame filtering ────────────────────────────────────────────────────────────

def filter_frames(
    frames: list[FrameData],
    blur_threshold: float,
    similarity_threshold: int,
) -> list[FrameData]:
    """
    Two-pass filter matching app.js wc():
      1. Keep frames where blur_score >= blur_threshold
      2. Greedy sequential similarity: skip if too similar to last kept frame
    """
    passing = [f for f in frames if f.blur_score >= blur_threshold]

    kept: list[FrameData] = []
    last_hash: bytes | None = None

    for f in passing:
        if last_hash is None or hamming(last_hash, f.phash) >= similarity_threshold:
            kept.append(f)
            last_hash = f.phash

    return kept


# ── Histogram helpers ─────────────────────────────────────────────────────────

def blur_histogram(frames: list[FrameData], bins: int = 60) -> tuple[list[float], list[int]]:
    """Returns (bin_edges, counts) for blur scores."""
    scores = [f.blur_score for f in frames]
    if not scores:
        return [], []
    max_s = max(scores) or 1.0
    step  = max_s / bins
    edges = [i * step for i in range(bins + 1)]
    counts = [0] * bins
    for s in scores:
        idx = min(int(s / step), bins - 1)
        counts[idx] += 1
    return edges, counts


def similarity_histogram(frames: list[FrameData], bins: int = 32) -> tuple[list[int], list[int]]:
    """Returns (distances 0-32, counts) for consecutive frame pairs."""
    dists = [hamming(frames[i].phash, frames[i + 1].phash) for i in range(len(frames) - 1)]
    counts = [0] * (bins + 1)
    for d in dists:
        counts[min(d, bins)] += 1
    return list(range(bins + 1)), counts
