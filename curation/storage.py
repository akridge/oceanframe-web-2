"""
Reading curation images and CSVs from GCS (or a local path, for testing).

Goes through the library's ``GCSBackend``, so it gets the same behaviour:
Application Default Credentials when present, an anonymous client for public
buckets otherwise. A missing object is always ``FileNotFoundError`` here, so
callers can try fallbacks without knowing about google.api_core.

Callers use ``storage.read_bytes`` / ``storage.open_text`` through the module so
tests can replace them.
"""
from __future__ import annotations

import io
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO

from library.storage.gcs import GCSBackend, parse_gs_uri
from library.storage.local import file_uri_to_path

_backends: dict[str, GCSBackend] = {}
_lock = threading.Lock()


def _backend(bucket: str) -> GCSBackend:
    with _lock:
        backend = _backends.get(bucket)
        if backend is None:
            backend = _backends[bucket] = GCSBackend(f"gs://{bucket}")
        return backend


def _is_missing(exc: Exception) -> bool:
    try:
        from google.api_core.exceptions import NotFound  # noqa: PLC0415
    except ImportError:
        return False
    return isinstance(exc, NotFound)


def _local_path(uri: str) -> Path:
    return Path(file_uri_to_path(uri) if uri.startswith("file://") else uri)


def _local_allowed(uri: str) -> None:
    from curation import settings  # noqa: PLC0415 — avoid an import cycle at load
    if not settings.allow_local_sources():
        raise PermissionError(f"Local paths are not readable here: {uri!r} (CURATION_ALLOW_LOCAL_SOURCES is off)")


def read_bytes(uri: str) -> bytes:
    if not uri.startswith("gs://"):
        _local_allowed(uri)
        path = _local_path(uri)
        if not path.is_file():
            raise FileNotFoundError(uri)
        return path.read_bytes()
    bucket, _ = parse_gs_uri(uri)
    try:
        return _backend(bucket).read_bytes(uri)
    except Exception as exc:
        if _is_missing(exc):
            raise FileNotFoundError(uri) from exc
        raise


@contextmanager
def open_text(uri: str) -> Iterator[TextIO]:
    """Stream a text object line by line; regional CSVs run to hundreds of MB."""
    if not uri.startswith("gs://"):
        _local_allowed(uri)
        path = _local_path(uri)
        if not path.is_file():
            raise FileNotFoundError(uri)
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            yield stream
        return
    bucket, key = parse_gs_uri(uri)
    backend = _backend(bucket)
    backend._ensure_client()  # noqa: SLF001 — same codebase; builds ADC-or-anonymous client
    blob = backend._bucket.blob(key)  # noqa: SLF001
    try:
        stream = blob.open("rb")
    except Exception as exc:
        if _is_missing(exc):
            raise FileNotFoundError(uri) from exc
        raise
    with io.TextIOWrapper(stream, encoding="utf-8-sig", newline="") as text:
        yield text


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def list_names(prefix_uri: str) -> set[str]:
    """File names (not paths) of the images directly under a prefix, e.g. gs://bucket/.../images/."""
    if not prefix_uri.startswith("gs://"):
        _local_allowed(prefix_uri)
        folder = _local_path(prefix_uri)
        return {p.name for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS} if folder.is_dir() else set()
    backend = GCSBackend(prefix_uri.rstrip("/"))
    return {ref.key.rsplit("/", 1)[-1] for ref in backend.list_objects(IMAGE_EXTENSIONS) if "/" not in ref.key}
