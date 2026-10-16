"""
In-memory session store.  Supports both video and image-collection sources.
"""
from __future__ import annotations
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config import SESSION_TTL


@dataclass
class VideoMeta:
    fps: float
    frame_count: int
    width: int
    height: int
    duration_s: float
    codec: str
    file_size: int
    filename: str

    @property
    def duration_str(self) -> str:
        mins, secs = divmod(int(self.duration_s), 60)
        hrs, mins  = divmod(mins, 60)
        return f"{hrs:02d}:{mins:02d}:{secs:02d}" if hrs else f"{mins:02d}:{secs:02d}"

    @property
    def file_size_str(self) -> str:
        n = self.file_size
        for unit in ('B', 'KB', 'MB', 'GB'):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} GB"

    def to_dict(self) -> dict:
        return {
            "fps":           round(self.fps, 2),
            "frame_count":   self.frame_count,
            "width":         self.width,
            "height":        self.height,
            "duration_s":    round(self.duration_s, 2),
            "duration_str":  self.duration_str,
            "codec":         self.codec,
            "file_size_str": self.file_size_str,
            "filename":      self.filename,
        }


@dataclass
class FrameRecord:
    """Lightweight per-frame record stored in session (no PIL image)."""
    index:        int
    timestamp_ms: float
    blur_score:   float
    phash_hex:    str       # 16-char hex (8 bytes)
    brightness:   float = 0.0   # mean luminance 0–255
    color_cast:   float = 1.0   # R/B ratio; <0.8 blue-heavy, >1.2 red-heavy


@dataclass
class Session:
    id:           str
    video_path:   str           # empty string for image-collection sessions
    meta:         VideoMeta
    source_type:  str                    = "video"   # "video" | "images"
    image_paths:  dict[int, str]         = field(default_factory=dict)
    frames:       list[FrameRecord]      = field(default_factory=list)
    thumbnails:   dict[int, str]         = field(default_factory=dict)
    cancel_flag:  bool                   = False
    created_at:   float                  = field(default_factory=time.time)


# ── Store ─────────────────────────────────────────────────────────────────────

_sessions: dict[str, Session] = {}


def clear_thumbnails(session: Session) -> None:
    for path in session.thumbnails.values():
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass
    session.thumbnails.clear()


def create_session(video_path: str, meta: VideoMeta) -> Session:
    _cleanup()
    sid = str(uuid.uuid4())
    s   = Session(id=sid, video_path=video_path, meta=meta)
    _sessions[sid] = s
    return s


def get_session(session_id: str) -> Optional[Session]:
    return _sessions.get(session_id)


def delete_session(session_id: str) -> None:
    s = _sessions.pop(session_id, None)
    if s:
        clear_thumbnails(s)
        # Clean up uploaded files
        try:
            if s.video_path:
                Path(s.video_path).unlink(missing_ok=True)
        except Exception:
            pass
        for p in s.image_paths.values():
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass


def _cleanup() -> None:
    now   = time.time()
    stale = [k for k, v in _sessions.items() if now - v.created_at > SESSION_TTL]
    for k in stale:
        delete_session(k)