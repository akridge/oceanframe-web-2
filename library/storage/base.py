"""Storage backend interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator

from library.models import ObjectRef


class StorageBackend(ABC):
    """
    Read-only access to a tree of objects rooted at ``root``.

    Implementations must be safe to call from several threads at once — the
    indexer fans out ``read_bytes`` across a thread pool.
    """

    kind: str = "base"

    def __init__(self, root: str) -> None:
        self.root = root

    @abstractmethod
    def list_objects(self, extensions: set[str]) -> Iterator[ObjectRef]:
        """Yield every object under the root whose extension matches."""

    @abstractmethod
    def read_bytes(self, uri: str) -> bytes:
        """Fetch one object's full contents."""

    def signed_url(self, uri: str, ttl: int) -> str | None:
        """Direct browser-fetchable URL, or None if the backend cannot mint one."""
        return None

    @abstractmethod
    def exists(self) -> bool:
        """True when the root is reachable and readable."""

    def describe(self) -> str:
        return f"{self.kind}:{self.root}"
