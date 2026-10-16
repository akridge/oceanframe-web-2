"""
POST /api/upload        — single video file
POST /api/upload-images — one or more image files (treated as sequential frames)
"""
from pathlib import Path
import uuid

import cv2
from fastapi import APIRouter, File, HTTPException, UploadFile
from typing import List

from config import MAX_UPLOAD_BYTES, UPLOAD_DIR
from core.video import VideoReader, SUPPORTED_EXTENSIONS
from session import VideoMeta, create_session

router = APIRouter()

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.tiff', '.tif', '.bmp', '.webp'}
_UPLOAD_CHUNK_SIZE = 1024 * 1024


def _copy_upload_to_dest(file: UploadFile, dest: Path) -> int:
    total = 0
    with dest.open("wb") as fh:
        while True:
            chunk = file.file.read(_UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"Upload exceeds the maximum allowed size of {MAX_UPLOAD_BYTES} bytes",
                )
            fh.write(chunk)
    return total


def _close_upload(file: UploadFile) -> None:
    file.file.close()


def _remove_upload(dest: Path) -> None:
    dest.unlink(missing_ok=True)


# ── Video upload ───────────────────────────────────────────────────────────────

@router.post("/upload")
def upload_video(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported format '{suffix}'. Accepted: {', '.join(sorted(SUPPORTED_EXTENSIONS))}",
        )

    dest = UPLOAD_DIR / f"{uuid.uuid4()}{suffix}"
    try:
        _copy_upload_to_dest(file, dest)
    except Exception:
        _remove_upload(dest)
        raise
    finally:
        _close_upload(file)

    reader = VideoReader()
    try:
        m = reader.open(str(dest))
    except IOError as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=str(exc))
    finally:
        reader.close()

    meta = VideoMeta(
        fps=m.fps,
        frame_count=m.frame_count,
        width=m.width,
        height=m.height,
        duration_s=m.duration_s,
        codec=m.codec,
        file_size=dest.stat().st_size,
        filename=file.filename or dest.name,
    )
    session = create_session(str(dest), meta)
    return {"session_id": session.id, "meta": meta.to_dict(), "source_type": "video"}


# ── Image-collection upload ────────────────────────────────────────────────────

@router.post("/upload-images")
def upload_images(files: List[UploadFile] = File(...)):
    saved: dict[int, str] = {}
    total_size = 0
    names: list[str] = []

    # Sort files by name so order is deterministic
    sorted_files = sorted(files, key=lambda f: (f.filename or "").lower())

    for i, file in enumerate(sorted_files):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            _close_upload(file)
            continue
        dest = UPLOAD_DIR / f"{uuid.uuid4()}{suffix}"
        try:
            _copy_upload_to_dest(file, dest)
        except Exception:
            _remove_upload(dest)
            raise
        finally:
            _close_upload(file)
        saved[i]    = str(dest)
        total_size += dest.stat().st_size
        names.append(file.filename or dest.name)

    if not saved:
        raise HTTPException(422, "No supported image files found. Accepted: " +
                            ", ".join(sorted(IMAGE_EXTENSIONS)))

    # Read dimensions from first valid image
    width = height = 0
    first_path = next(iter(saved.values()), None)
    first_bgr = cv2.imread(first_path) if first_path else None
    if first_bgr is not None:
        height, width = first_bgr.shape[:2]

    n = len(saved)
    meta = VideoMeta(
        fps=1.0,
        frame_count=n,
        width=width,
        height=height,
        duration_s=float(n),
        codec="images",
        file_size=total_size,
        filename=f"{n} image{'s' if n != 1 else ''}",
    )

    session              = create_session("", meta)
    session.source_type  = "images"
    session.image_paths  = saved

    return {
        "session_id":  session.id,
        "meta":        meta.to_dict(),
        "source_type": "images",
        "filenames":   names,
    }
