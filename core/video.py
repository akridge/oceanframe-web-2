"""
VideoReader — wraps cv2.VideoCapture.

iter_frames yields (frame_index, timestamp_ms, bgr_ndarray).
Keyframe mode samples ~1 frame per second (matches browser app behaviour).
"""

from __future__ import annotations
import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


SUPPORTED_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.mts', '.m2ts', '.m4v'}


@dataclass
class VideoMeta:
    path: str
    fps: float
    frame_count: int
    width: int
    height: int
    duration_s: float
    codec: str

    @property
    def duration_str(self) -> str:
        mins, secs = divmod(int(self.duration_s), 60)
        hrs,  mins = divmod(mins, 60)
        return f"{hrs:02d}:{mins:02d}:{secs:02d}" if hrs else f"{mins:02d}:{secs:02d}"

    @property
    def file_size_str(self) -> str:
        size = Path(self.path).stat().st_size
        for unit in ('B', 'KB', 'MB', 'GB'):
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"


class VideoReader:
    def __init__(self):
        self._cap: cv2.VideoCapture | None = None
        self.meta: VideoMeta | None = None

    def open(self, path: str) -> VideoMeta:
        if self._cap:
            self._cap.release()
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            raise IOError(f"Cannot open video: {path}")

        fps         = cap.get(cv2.CAP_PROP_FPS) or 30.0
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        width       = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height      = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        duration_s  = frame_count / fps if fps else 0.0
        fourcc_int  = int(cap.get(cv2.CAP_PROP_FOURCC))
        codec = ''.join(chr((fourcc_int >> 8 * i) & 0xFF) for i in range(4)).strip('\x00')

        self._cap = cap
        self.meta = VideoMeta(path, fps, frame_count, width, height, duration_s, codec)
        return self.meta

    def iter_frames(
        self,
        mode: str = 'all',
        cancel: threading.Event | None = None,
    ):
        """
        Yields (frame_index, timestamp_ms, bgr_ndarray).

        mode='all'       — every frame
        mode='keyframes' — ~1 frame per second (frame-index/fps sampling)
        """
        if not self._cap or not self.meta:
            raise RuntimeError("Call open() first")

        cap   = self._cap
        meta  = self.meta
        sample_every_frames = meta.fps if (meta.fps and meta.fps > 0) else 30.0

        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        frame_idx = 0
        next_sample_frame = 0.0

        while True:
            if cancel and cancel.is_set():
                return

            ret, bgr = cap.read()
            if not ret:
                break

            raw_ts_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
            if raw_ts_ms > 0:
                ts_ms = raw_ts_ms
            else:
                # Fallback timestamp for containers/codecs where POS_MSEC is unstable.
                ts_ms = (frame_idx / sample_every_frames) * 1000.0

            if mode == 'keyframes':
                # Deterministic ~1fps cadence based on frame index avoids sparse
                # POS_MSEC jumps seen with some H.264 files in OpenCV.
                if frame_idx + 0.5 >= next_sample_frame:
                    yield frame_idx, ts_ms, bgr
                    next_sample_frame += sample_every_frames
            else:
                yield frame_idx, ts_ms, bgr

            frame_idx += 1

    def read_at(self, timestamp_ms: float) -> np.ndarray | None:
        """Seek to timestamp and return one BGR frame (for export)."""
        if not self._cap:
            return None
        self._cap.set(cv2.CAP_PROP_POS_MSEC, timestamp_ms)
        ret, bgr = self._cap.read()
        return bgr if ret else None

    def close(self):
        if self._cap:
            self._cap.release()
            self._cap = None
