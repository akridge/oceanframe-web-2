"""
Disk cache in front of cloud image reads: source images and cut patches.

Every curation read of a source image goes through here: the image proxy, crop
previews, neighbourhood classification, suggestions and image-folder export.
Dragging a patch around or paging through points therefore re-reads nothing
from GCS. Patches are keyed by (image, row, column, size), so a moved patch is
a new key and never shows its old crop. Nothing is ever written back to a bucket.
Bounded by CURATION_IMAGE_CACHE_MB, oldest-used first; a miss is never cached.
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import threading
from pathlib import Path

from PIL import Image

from curation import settings, storage

_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}
_dimensions: dict[str, tuple[int, int]] = {}
_writes = 0
_warned = False
_evicting = threading.Lock()
logger = logging.getLogger(__name__)
EVICT_EVERY = 20  # check the cap every N writes (and whenever the cap is 0)


def _root() -> Path:
    return settings.image_cache_dir()


def _key(uri: str) -> str:
    return hashlib.sha1(uri.encode("utf-8")).hexdigest()


def _lock_for(name: str) -> threading.Lock:
    with _guard:
        return _locks.setdefault(name, threading.Lock())


def _hit(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        os.utime(path, None)  # mark as recently used
    except OSError:
        pass
    return data


def _warn_once(what: str, exc: Exception) -> None:
    global _warned
    with _guard:
        first, _warned = not _warned, True
    if first:
        logger.warning("image cache %s failed (%s); serving without caching", what, exc)


def _write(path: Path, payload: bytes) -> None:
    """Best effort: a cache that cannot be written is a pass-through, never an error."""
    global _writes
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.part")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    except OSError as exc:
        _warn_once("write", exc)
        try:
            temporary.unlink()
        except OSError:
            pass
        return
    with _guard:
        _writes += 1
        due = _writes % EVICT_EVERY == 0 or settings.image_cache_mb() == 0
    if due:
        try:
            _evict(keep=path)
        except OSError as exc:
            _warn_once("eviction", exc)


def _evict(keep: Path | None = None) -> None:
    if not _evicting.acquire(blocking=False):
        return  # another eviction is already running
    try:
        cap = settings.image_cache_mb() * 1024 * 1024
        entries = []
        for p in _root().rglob("*"):
            if p.name.endswith(".part"):
                continue
            try:
                stat = p.stat()
            except OSError:
                continue  # vanished between listing and stat
            if p.is_file():
                entries.append((stat.st_mtime, stat.st_size, p))
        total = sum(size for _, size, _ in entries)
        for _, size, path in sorted(entries, key=lambda e: e[0]):
            if total <= cap:
                break
            if keep is not None and path == keep and total - size <= cap:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                continue
            total -= size
    finally:
        _evicting.release()


def read_source(uri: str) -> bytes:
    path = _root() / "sources" / _key(uri)
    data = _hit(path)
    if data is not None:
        return data
    with _lock_for(path.name):
        data = _hit(path)
        if data is not None:
            return data
        data = storage.read_bytes(uri)  # FileNotFoundError propagates; nothing cached for a miss
        _write(path, data)
        return data


def dimensions(uri: str) -> tuple[int, int]:
    size = _dimensions.get(uri)
    if size is None:
        with Image.open(io.BytesIO(read_source(uri))) as image:  # reads the header only
            size = _dimensions[uri] = image.size
    return size


def patch(uri: str, row_px: int, column_px: int, crop_size: int) -> bytes:
    path = _root() / "patches" / f"{_key(uri)}_{row_px}_{column_px}_{crop_size}.png"
    data = _hit(path)
    if data is not None:
        return data
    with Image.open(io.BytesIO(read_source(uri))) as source:
        source = source.convert("RGB")
        half = crop_size // 2
        crop = source.crop((column_px - half, row_px - half, column_px + half, row_px + half))
        if crop.size != (224, 224):
            crop = crop.resize((224, 224), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        crop.save(output, format="PNG")
    data = output.getvalue()
    _write(path, data)
    return data


def clear() -> None:
    """Empty the cache (tests)."""
    global _warned
    with _guard:
        _dimensions.clear()
        _locks.clear()
        _warned = False
    shutil.rmtree(_root(), ignore_errors=True)
