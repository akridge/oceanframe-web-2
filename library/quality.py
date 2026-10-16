"""
OceanFrame quality scoring for library assets.

Reuses the frame analyser's metrics so a still in the library and a frame pulled
out of a video are scored on the same scale:

* ``blur``       — ``core.filters.blur_score``: centre-weighted Laplacian variance
* ``phash``      — 8x8 average hash, the same 64-bit form ``core.filters`` emits
* ``brightness`` — mean luma, as in ``analysis.py``
* ``color_cast`` — mean R / mean B, as in ``analysis.py``
* ``contrast``   — luma standard deviation (new; stills vary far more than a
                   single dive's video does)

The four sub-scores are each 0-100 and combined with ``WEIGHTS`` into
``quality``.  Retune the constants here for a different survey programme; the
pipeline reads them and nothing else does.
"""
from __future__ import annotations

import math

import numpy as np
from PIL import Image

from core.filters import blur_score, compute_phash, hamming
from library.models import QualityMetrics

# ── Tunables ──────────────────────────────────────────────────────────────────

WEIGHTS = {
    "sharpness": 0.40,
    "exposure":  0.25,
    "contrast":  0.20,
    "colour":    0.15,
}

BLUR_REF      = 600.0        # Laplacian variance that already counts as "sharp"
EXPOSURE_BAND = (110.0, 160.0)   # comfortable mean-luma window
EXPOSURE_FALLOFF = 70.0      # luma units outside the band that reach score 0
CONTRAST_REF  = 64.0         # luma std that saturates the contrast sub-score
CAST_TOLERANCE = 0.85        # |ln(R/B)| scale; larger = more forgiving of blue water

_LUMA = np.asarray([0.299, 0.587, 0.114], dtype=np.float32)


# ── Sub-scores ────────────────────────────────────────────────────────────────

def sharpness_score(blur: float) -> float:
    """Log-compressed: variance spans orders of magnitude, perceived sharpness does not."""
    return float(min(100.0, 100.0 * math.log1p(max(blur, 0.0)) / math.log1p(BLUR_REF)))


def exposure_score(brightness: float) -> float:
    low, high = EXPOSURE_BAND
    if low <= brightness <= high:
        return 100.0
    distance = low - brightness if brightness < low else brightness - high
    return float(max(0.0, 100.0 - 100.0 * distance / EXPOSURE_FALLOFF))


def contrast_score(contrast: float) -> float:
    return float(min(100.0, 100.0 * contrast / CONTRAST_REF))


def colour_score(color_cast: float) -> float:
    """
    Gaussian penalty on |ln(R/B)|.

    Underwater imagery is legitimately blue-green, so this is deliberately soft:
    a cast of 0.5 still scores ~51, while a fully colour-collapsed 0.2 scores ~7.
    """
    deviation = abs(math.log(max(color_cast, 1e-3)))
    return float(100.0 * math.exp(-((deviation / CAST_TOLERANCE) ** 2)))


def composite(blur: float, brightness: float, contrast: float, color_cast: float) -> float:
    parts = {
        "sharpness": sharpness_score(blur),
        "exposure":  exposure_score(brightness),
        "contrast":  contrast_score(contrast),
        "colour":    colour_score(color_cast),
    }
    # One decimal: the same precision the UI badges and the range filters use,
    # so "quality >= 60" never excludes something displayed as 60.
    return round(sum(parts[k] * w for k, w in WEIGHTS.items()), 1)


def breakdown(blur: float, brightness: float, contrast: float, color_cast: float) -> dict:
    """Per-component scores, for the asset detail panel."""
    return {
        "sharpness": round(sharpness_score(blur), 1),
        "exposure":  round(exposure_score(brightness), 1),
        "contrast":  round(contrast_score(contrast), 1),
        "colour":    round(colour_score(color_cast), 1),
        "weights":   WEIGHTS,
    }


# ── Perceptual hash ───────────────────────────────────────────────────────────
#
# The library reuses ``core.filters.compute_phash`` rather than reimplementing
# it.  A near-equivalent (Pillow's BOX resize) disagrees with cv2's INTER_AREA
# on the odd cell that lands right at the threshold, and a hash that is only
# *almost* the same is worse than useless: video frames and library stills would
# stop deduping against each other for no visible reason.


def phash_hex(gray: np.ndarray) -> str:
    # core.filters resizes with cv2, which wants a concrete 8-bit array; the
    # library's luma is float, so quantise once here rather than in every caller.
    return compute_phash(np.clip(gray, 0, 255).astype(np.uint8)).hex()


def phash_distance(a_hex: str, b_hex: str) -> int:
    """Hamming distance between two hex phashes; 64 (max) when either is missing."""
    if not a_hex or not b_hex or len(a_hex) != len(b_hex):
        return 64
    try:
        return hamming(bytes.fromhex(a_hex), bytes.fromhex(b_hex))
    except ValueError:
        return 64


# ── Entry point ───────────────────────────────────────────────────────────────

def analyse(image: Image.Image) -> QualityMetrics:
    """Compute every metric for one decoded image."""
    rgb_img = image.convert("RGB")
    width, height = rgb_img.size
    rgb = np.asarray(rgb_img, dtype=np.float32)
    gray = rgb @ _LUMA

    blur = blur_score(gray) if min(gray.shape) > 2 else 0.0
    brightness = float(gray.mean())
    contrast = float(gray.std())
    r_mean = float(rgb[..., 0].mean()) + 1.0
    b_mean = float(rgb[..., 2].mean()) + 1.0
    cast = r_mean / b_mean

    return QualityMetrics(
        blur=round(blur, 2),
        brightness=round(brightness, 1),
        contrast=round(contrast, 1),
        color_cast=round(cast, 3),
        quality=composite(blur, brightness, contrast, cast),
        phash=phash_hex(gray),
        width=width,
        height=height,
    )
