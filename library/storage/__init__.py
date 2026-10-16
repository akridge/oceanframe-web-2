"""Storage backend factory."""
from __future__ import annotations

from pathlib import Path

from library.storage.base import StorageBackend
from library.storage.gcs import GCSBackend
from library.storage.local import LocalBackend, file_uri_to_path

__all__ = ["StorageBackend", "GCSBackend", "LocalBackend", "get_backend", "backend_kind", "normalise_root"]


def backend_kind(root: str) -> str:
    return "gcs" if root.startswith("gs://") else "local"


def normalise_root(root: str) -> str:
    """Canonical form used as the ``sources.root`` primary key."""
    root = root.strip()
    if root.startswith("gs://"):
        return root.rstrip("/")
    if root.startswith("file://"):
        root = file_uri_to_path(root)
    return str(Path(root).expanduser().resolve())


def get_backend(root: str) -> StorageBackend:
    return GCSBackend(root) if backend_kind(root) == "gcs" else LocalBackend(root)
