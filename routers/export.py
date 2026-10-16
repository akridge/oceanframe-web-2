"""
GET  /api/thumb/{session_id}/{frame_index}  — stored thumbnail JPEG
GET  /api/frame/{session_id}/{frame_index}  — full-resolution frame
GET  /api/csv/{session_id}                  — enriched frame metadata CSV
POST /api/export/{session_id}               — ZIP of selected frames (optional EXIF)
"""
from __future__ import annotations
import io
import csv
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

import cv2
import piexif
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response, StreamingResponse
from PIL import Image
from pydantic import BaseModel
from starlette.background import BackgroundTask

from core.video import VideoReader
from session import get_session, FrameRecord

router = APIRouter()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _read_bgr_video(session, timestamp_ms: float):
    reader = VideoReader()
    reader.open(session.video_path)
    bgr = reader.read_at(timestamp_ms)
    reader.close()
    return bgr


def _read_bgr_image(session, frame_index: int):
    path = session.image_paths.get(frame_index)
    if not path:
        return None
    return cv2.imread(path)


def _get_bgr(session, frame_index: int, timestamp_ms: float):
    if session.source_type == "images":
        return _read_bgr_image(session, frame_index)
    return _read_bgr_video(session, timestamp_ms)


def _bgr_to_pil(bgr) -> Image.Image:
    return Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))


def _resize_pil(img: Image.Image, max_long_edge: Optional[int]) -> Image.Image:
    if not max_long_edge:
        return img
    w, h  = img.size
    long  = max(w, h)
    if long <= max_long_edge:
        return img
    scale = max_long_edge / long
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)


def _build_exif(record: FrameRecord, stem: str) -> bytes:
    """Build minimal EXIF bytes with frame metadata."""
    try:
        desc = (
            f"OceanFrame | file:{stem} | frame:{record.index} | "
            f"ts:{record.timestamp_ms:.0f}ms | blur:{record.blur_score} | "
            f"brightness:{record.brightness} | color_cast:{record.color_cast} | "
            f"hash:{record.phash_hex}"
        ).encode('utf-8')
        exif_dict = {
            "0th": {
                piexif.ImageIFD.ImageDescription: desc,
                piexif.ImageIFD.Software: b"OceanFrame",
            },
            "Exif": {
                piexif.ExifIFD.UserComment: b"ASCII\x00\x00\x00" + desc,
            },
        }
        return piexif.dump(exif_dict)
    except Exception:
        return b""


def _save_jpeg(img: Image.Image, quality: int, exif_bytes: bytes = b"") -> bytes:
    buf = io.BytesIO()
    kwargs: dict = {"format": "JPEG", "quality": quality, "optimize": True}
    if exif_bytes:
        kwargs["exif"] = exif_bytes
    img.save(buf, **kwargs)
    return buf.getvalue()


# ── Thumbnail ──────────────────────────────────────────────────────────────────

@router.get("/thumb/{session_id}/{frame_index}")
def get_thumbnail(session_id: str, frame_index: int):
    session = get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    path = session.thumbnails.get(frame_index)
    if path is None:
        raise HTTPException(404, "Thumbnail not found")
    thumb_path = Path(path)
    if not thumb_path.exists():
        raise HTTPException(404, "Thumbnail not found")
    return Response(content=thumb_path.read_bytes(), media_type="image/jpeg")


# ── Full-resolution frame ──────────────────────────────────────────────────────

@router.get("/frame/{session_id}/{frame_index}")
async def get_full_frame(session_id: str, frame_index: int):
    session = get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    lookup = {f.index: f for f in session.frames}
    record = lookup.get(frame_index)
    if record is None:
        raise HTTPException(404, "Frame not found")

    def _read() -> bytes:
        bgr = _get_bgr(session, frame_index, record.timestamp_ms)
        if bgr is None:
            raise ValueError("Could not read frame")
        img = _bgr_to_pil(bgr)
        return _save_jpeg(img, quality=92)

    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as pool:
        jpeg = await loop.run_in_executor(pool, _read)

    return Response(
        content=jpeg,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )


# ── CSV export ─────────────────────────────────────────────────────────────────

class CsvRequest(BaseModel):
    kept_indices:    list[int] = []
    manual_excludes: list[int] = []
    manual_includes: list[int] = []
    tags:            dict[str, list[str]] = {}   # str(index) -> [tag, …]

@router.post("/csv/{session_id}")
def export_csv(session_id: str, req: CsvRequest):
    session = get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    kept_set     = set(req.kept_indices)
    excl_set     = set(req.manual_excludes)
    incl_set     = set(req.manual_includes)

    buf    = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "frame_index", "timestamp_ms", "timestamp_s",
        "blur_score", "brightness", "color_cast", "phash_hex",
        "status", "tags",
    ])

    for f in session.frames:
        if f.index in excl_set:
            status = "manual_excluded"
        elif f.index in incl_set:
            status = "manual_included"
        elif f.index in kept_set:
            status = "kept"
        else:
            status = "filtered"

        frame_tags = ";".join(req.tags.get(str(f.index), []))

        writer.writerow([
            f.index,
            round(f.timestamp_ms, 1),
            round(f.timestamp_ms / 1000, 3),
            f.blur_score,
            f.brightness,
            f.color_cast,
            f.phash_hex,
            status,
            frame_tags,
        ])

    stem = Path(session.meta.filename).stem
    return Response(
        content=buf.getvalue().encode(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{stem}_frames.csv"'},
    )


# ── ZIP export ─────────────────────────────────────────────────────────────────

class ExportRequest(BaseModel):
    frame_indices:   list[int]
    format:          str           = "jpeg"
    quality:         int           = 90
    max_long_edge:   Optional[int] = None
    write_exif:      bool          = True
    tags:            dict[str, list[str]] = {}


@router.post("/export/{session_id}")
async def export_zip(session_id: str, req: ExportRequest):
    session = get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    if not req.frame_indices:
        raise HTTPException(422, "No frames selected")

    lookup   = {f.index: f for f in session.frames}
    selected = [lookup[i] for i in req.frame_indices if i in lookup]
    if not selected:
        raise HTTPException(422, "None of the requested frame indices exist")

    fmt  = req.format.lower()
    if fmt not in {"jpeg", "png"}:
        raise HTTPException(422, "Unsupported export format")
    ext  = "jpg" if fmt == "jpeg" else "png"
    stem = Path(session.meta.filename).stem

    def _build() -> str:
        with tempfile.NamedTemporaryFile(prefix="oceanframe-export-", suffix=".zip", delete=False) as tmp:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                for record in selected:
                    bgr = _get_bgr(session, record.index, record.timestamp_ms)
                    if bgr is None:
                        continue

                    img = _resize_pil(_bgr_to_pil(bgr), req.max_long_edge)

                    if fmt == "jpeg":
                        exif = _build_exif(record, stem) if req.write_exif else b""
                        data = _save_jpeg(img, req.quality, exif)
                    else:
                        png_buf = io.BytesIO()
                        img.save(png_buf, format="PNG")
                        data = png_buf.getvalue()

                    zf.writestr(f"{stem}_frame{record.index:06d}.{ext}", data)

        return tmp.name

    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as pool:
        zip_path = await loop.run_in_executor(pool, _build)

    def _stream_file(path: str):
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        _stream_file(zip_path),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{stem}_frames.zip"'},
        background=BackgroundTask(os.remove, zip_path),
    )
