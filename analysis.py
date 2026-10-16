"""
Analysis workers — video and image-collection sources.
Runs in a ThreadPoolExecutor, posts SSE events via a thread-safe callback.
"""
from __future__ import annotations
import io
import math
import uuid
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from core.filters import blur_score as compute_blur, compute_phash
from core.video import VideoReader
from config import THUMBNAIL_DIR, THUMB_WIDTH
from session import Session, FrameRecord


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _jpeg_thumbnail(bgr: np.ndarray, width: int = THUMB_WIDTH) -> bytes:
    h, w  = bgr.shape[:2]
    new_h = max(1, int(h * width / w))
    small = cv2.resize(bgr, (width, new_h), interpolation=cv2.INTER_AREA)
    buf   = io.BytesIO()
    Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)).save(
        buf, format='JPEG', quality=75, optimize=True
    )
    return buf.getvalue()


def _store_thumbnail(session: Session, frame_index: int, thumb: bytes) -> str:
    path = THUMBNAIL_DIR / f"{session.id}_{frame_index:06d}_{uuid.uuid4().hex}.jpg"
    path.write_bytes(thumb)
    return str(path)


def _brightness(gray: np.ndarray) -> float:
    """Mean luminance of the frame (0–255). Higher = brighter."""
    return round(float(gray.mean()), 1)


def _color_cast(bgr: np.ndarray) -> float:
    """
    R/B channel ratio as an underwater colour-cast indicator.
    ~1.0 = neutral;  <0.8 = blue/green-heavy (deep water);  >1.2 = red-heavy.
    """
    b = float(bgr[:, :, 0].mean()) + 1.0
    r = float(bgr[:, :, 2].mean()) + 1.0
    return round(r / b, 3)


def _make_record(idx: int, ts_ms: float, bgr: np.ndarray, gray: np.ndarray) -> tuple[FrameRecord, bytes]:
    bs       = compute_blur(gray)
    ph_hex   = compute_phash(gray).hex()
    bright   = _brightness(gray)
    cast     = _color_cast(bgr)
    thumb    = _jpeg_thumbnail(bgr)
    record   = FrameRecord(
        index=idx,
        timestamp_ms=ts_ms,
        blur_score=round(bs, 2),
        phash_hex=ph_hex,
        brightness=bright,
        color_cast=cast,
    )
    return record, thumb


def _sse_payload(record: FrameRecord, count: int, frac: float) -> dict:
    return {
        'type':         'frame',
        'index':        record.index,
        'timestamp_ms': round(record.timestamp_ms, 1),
        'blur_score':   record.blur_score,
        'phash_hex':    record.phash_hex,
        'brightness':   record.brightness,
        'color_cast':   record.color_cast,
        'count':        count,
        'frac':         round(frac, 4),
    }


# ── Video analysis ─────────────────────────────────────────────────────────────

def run_analysis(session: Session, post_event, mode: str = 'all') -> None:
    reader = VideoReader()
    try:
        meta      = reader.open(session.video_path)
        raw_total = max(meta.frame_count, 1)
        if mode == 'keyframes':
            # ~1 sample per second; duration-based estimate is more stable than
            # frame-count/fps when metadata fps is inaccurate.
            approx_total = max(int(meta.duration_s) + 1, 1)
        else:
            approx_total = raw_total
        count = 0

        for idx, ts_ms, bgr in reader.iter_frames(mode=mode):
            if session.cancel_flag:
                post_event({'type': 'cancelled', 'saved': count})
                return

            gray            = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            record, thumb   = _make_record(idx, ts_ms, bgr, gray)
            session.frames.append(record)
            session.thumbnails[idx] = _store_thumbnail(session, idx, thumb)
            count += 1

            frac = min(count / approx_total, 0.99)
            # Emit every frame so the browser has a complete frame set for
            # filtering, counts, and grid rendering.
            post_event(_sse_payload(record, count, frac))

        post_event({'type': 'complete', 'total': count})

    except Exception as exc:
        post_event({'type': 'error', 'message': str(exc)})
    finally:
        reader.close()


# ── Image-collection analysis ──────────────────────────────────────────────────

def run_image_analysis(session: Session, post_event) -> None:
    """Process a folder of uploaded images treated as sequential frames."""
    entries = sorted(session.image_paths.items())   # sorted by frame index (upload order)
    total   = max(len(entries), 1)
    count   = 0

    try:
        for idx, path in entries:
            if session.cancel_flag:
                post_event({'type': 'cancelled', 'saved': count})
                return

            bgr = cv2.imread(path)
            if bgr is None:
                continue

            gray            = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            ts_ms           = float(count * 1000)   # synthetic: 1 frame per second
            record, thumb   = _make_record(idx, ts_ms, bgr, gray)
            session.frames.append(record)
            session.thumbnails[idx] = _store_thumbnail(session, idx, thumb)
            count += 1

            frac = min(count / total, 0.99)
            post_event(_sse_payload(record, count, frac))

        post_event({'type': 'complete', 'total': count})

    except Exception as exc:
        post_event({'type': 'error', 'message': str(exc)})
