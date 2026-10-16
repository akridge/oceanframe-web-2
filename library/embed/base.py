"""Embedder interface."""
from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np
from PIL import Image


class Embedder(ABC):
    """
    Maps images (and optionally text) into one shared unit-norm vector space.

    Every implementation must return **L2-normalised float32** rows so the
    vector store can treat a dot product as cosine similarity.
    """

    name: str = "base"
    dim: int = 0
    supports_text: bool = False

    @abstractmethod
    def embed_images(self, images: list[Image.Image]) -> np.ndarray:
        """(n, dim) float32, unit norm."""

    def embed_text(self, texts: list[str]) -> np.ndarray:
        raise NotImplementedError(f"{self.name} does not support text queries")

    def describe(self) -> str:
        return f"{self.name}[{self.dim}d]"


def l2_normalise(mat: np.ndarray) -> np.ndarray:
    mat = np.asarray(mat, dtype=np.float32)
    if mat.ndim == 1:
        mat = mat[None, :]
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    np.maximum(norms, 1e-8, out=norms)
    return (mat / norms).astype(np.float32, copy=False)
