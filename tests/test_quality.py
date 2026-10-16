"""Quality metrics, and the claim that the library's phash matches core.filters."""
from __future__ import annotations

import numpy as np
import pytest

from conftest import make_image


def test_phash_matches_core_filters():
    """
    Library stills and video frames must hash identically, otherwise they stop
    deduping against each other for no visible reason.  quality.phash_hex is a
    thin wrapper over core.filters.compute_phash; this pins that.
    """
    cv2 = pytest.importorskip("cv2")
    from core.filters import compute_phash as core_phash
    from library.quality import phash_hex

    rng = np.random.RandomState(0)
    for _ in range(8):
        gray = cv2.GaussianBlur((rng.rand(240, 320) * 255).astype(np.uint8), (0, 0), 3)
        assert phash_hex(gray.astype(np.float32)) == core_phash(gray).hex()


def test_sharp_scores_above_blurred():
    from library.quality import analyse

    sharp = analyse(make_image(blobs=8))
    soft = analyse(make_image(blobs=8, blur=6.0))
    assert sharp.blur > soft.blur
    assert sharp.quality > soft.quality


def test_underexposed_is_penalised():
    from library.quality import analyse

    normal = analyse(make_image(blobs=6))
    dark = analyse(make_image(blobs=6, scale=0.12))
    assert dark.brightness < normal.brightness
    assert dark.quality < normal.quality


def test_composite_stays_in_range():
    from library.quality import composite

    for blur in (0.0, 10.0, 5000.0):
        for brightness in (0.0, 130.0, 255.0):
            for cast in (0.1, 1.0, 4.0):
                assert 0.0 <= composite(blur, brightness, 30.0, cast) <= 100.0


def test_colour_score_is_soft_on_blue_water():
    from library.quality import colour_score

    # Underwater imagery is legitimately blue; the penalty must be gradual.
    assert colour_score(1.0) == pytest.approx(100.0)
    assert 40 < colour_score(0.5) < 65
    assert colour_score(0.2) < 15


def test_phash_distance_handles_missing_values():
    from library.quality import phash_distance

    assert phash_distance("", "abcd") == 64
    assert phash_distance("ff00ff00ff00ff00", "ff00ff00ff00ff00") == 0
