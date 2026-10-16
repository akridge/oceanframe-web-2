"""
Append-only vector store: one raw float32 file plus a JSON sidecar.

Rows are L2-normalised on write, so a dot product is a cosine similarity and
ranking is a single matrix multiply.  ``assets.embed_row`` is the row index.

Why not a vector database?  The catalog already has to run the structured
predicates (folder prefix, quality band, tag and detection joins) in SQLite, and
the planner scores *only the rows those predicates keep*.  A flat exact scan
over a filtered subset beats an approximate index that cannot see the filter,
and it has no service to deploy.  When the catalog outgrows that — an unfiltered
query over ``ANN_MIN_ROWS`` rows — ``hnswlib`` is used if it is installed.
"""
from __future__ import annotations

import json
import threading

import numpy as np

from library import settings


class VectorStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._mm: np.memmap | None = None
        self._ann = None
        self._ann_rows = 0
        self.dim = 0
        self.rows = 0
        self.model = ""
        self._load_meta()

    # ── metadata ──────────────────────────────────────────────────────────────

    def _load_meta(self) -> None:
        settings.ensure_dirs()
        if settings.VEC_META.exists():
            try:
                meta = json.loads(settings.VEC_META.read_text())
                self.dim = int(meta.get("dim", 0))
                self.rows = int(meta.get("rows", 0))
                self.model = str(meta.get("model", ""))
            except (ValueError, OSError):
                self.dim = self.rows = 0
                self.model = ""
        # Trust the file length over the sidecar: a crash mid-append leaves the
        # sidecar stale, and a short read is worse than dropping a partial row.
        if self.dim and settings.VEC_PATH.exists():
            on_disk = settings.VEC_PATH.stat().st_size // (4 * self.dim)
            self.rows = min(self.rows, on_disk)

    def _save_meta(self) -> None:
        settings.VEC_META.write_text(
            json.dumps({"dim": self.dim, "rows": self.rows, "model": self.model}, indent=2)
        )

    def stats(self) -> dict:
        return {
            "rows": self.rows,
            "dim": self.dim,
            "model": self.model,
            "bytes": self.rows * self.dim * 4,
            "ann": self._ann is not None,
        }

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def reset(self, dim: int, model: str) -> None:
        """Drop every vector.  Called when the embedding model or dim changes."""
        with self._lock:
            self._mm = None
            self._ann = None
            self._ann_rows = 0
            settings.ensure_dirs()
            settings.VEC_PATH.write_bytes(b"")
            self.dim, self.rows, self.model = dim, 0, model
            self._save_meta()

    def ensure_model(self, dim: int, model: str) -> bool:
        """
        Make the store compatible with the given embedder.

        Returns True when it had to wipe existing vectors (the caller then has
        to clear ``assets.embed_row``).
        """
        with self._lock:
            if self.dim and (self.dim != dim or (self.model and self.model != model)):
                self.reset(dim, model)
                return True
            if not self.dim:
                self.reset(dim, model)
            elif self.model != model:
                self.model = model
                self._save_meta()
            return False

    def _memmap(self) -> np.memmap | None:
        if self.rows == 0 or self.dim == 0:
            return None
        if self._mm is None or self._mm.shape[0] != self.rows:
            self._mm = np.memmap(
                settings.VEC_PATH, dtype=np.float32, mode="r", shape=(self.rows, self.dim)
            )
        return self._mm

    # ── writes ────────────────────────────────────────────────────────────────

    def add(self, vectors: np.ndarray) -> list[int]:
        """Append rows; returns the assigned row indices in order."""
        vectors = np.ascontiguousarray(np.atleast_2d(vectors), dtype=np.float32)
        if vectors.size == 0:
            return []
        if vectors.shape[1] != self.dim:
            raise ValueError(f"vector dim {vectors.shape[1]} != store dim {self.dim}")
        with self._lock:
            start = self.rows
            with open(settings.VEC_PATH, "ab") as fh:
                fh.write(vectors.tobytes())
            self.rows += vectors.shape[0]
            self._mm = None
            self._ann = None                       # index is stale once rows are added
            self._save_meta()
            return list(range(start, self.rows))

    def put(self, row: int, vector: np.ndarray) -> None:
        """Overwrite one existing row in place (used when re-embedding an asset)."""
        vector = np.ascontiguousarray(vector, dtype=np.float32).reshape(-1)
        if vector.shape[0] != self.dim:
            raise ValueError(f"vector dim {vector.shape[0]} != store dim {self.dim}")
        with self._lock:
            if not 0 <= row < self.rows:
                raise IndexError(f"row {row} out of range (rows={self.rows})")
            with open(settings.VEC_PATH, "r+b") as fh:
                fh.seek(row * self.dim * 4)
                fh.write(vector.tobytes())
            self._mm = None
            self._ann = None

    # ── reads ─────────────────────────────────────────────────────────────────

    def get(self, rows: list[int]) -> np.ndarray:
        mm = self._memmap()
        if mm is None or not rows:
            return np.zeros((0, self.dim or 1), dtype=np.float32)
        idx = np.asarray(rows, dtype=np.int64)
        idx = idx[(idx >= 0) & (idx < self.rows)]
        return np.asarray(mm[idx], dtype=np.float32)

    # ── search ────────────────────────────────────────────────────────────────

    def search(
        self,
        query: np.ndarray,
        top_k: int = 100,
        candidate_rows: list[int] | None = None,
    ) -> list[tuple[int, float]]:
        """
        Cosine ranking.  ``candidate_rows`` restricts scoring to the rows the SQL
        filter kept; None means the whole store.

        Returns [(row, score)] sorted by descending score.
        """
        mm = self._memmap()
        if mm is None:
            return []
        q = np.ascontiguousarray(query, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(q))
        if norm > 1e-8:
            q = q / norm

        if candidate_rows is not None:
            return self._search_subset(mm, q, top_k, candidate_rows)
        if self.rows >= settings.ANN_MIN_ROWS:
            ann = self._ensure_ann(mm)
            if ann is not None:
                labels, distances = ann.knn_query(q[None, :], k=min(top_k, self.rows))
                return [(int(r), 1.0 - float(d)) for r, d in zip(labels[0], distances[0])]
        return self._search_full(mm, q, top_k)

    def _search_subset(self, mm, q: np.ndarray, top_k: int, rows: list[int]) -> list[tuple[int, float]]:
        idx = np.asarray(rows, dtype=np.int64)
        idx = idx[(idx >= 0) & (idx < self.rows)]
        if idx.size == 0:
            return []
        # Sorting keeps the memmap reads sequential, which matters a lot once the
        # matrix is larger than the page cache.
        order = np.argsort(idx, kind="stable")
        sorted_idx = idx[order]
        scores = np.asarray(mm[sorted_idx], dtype=np.float32) @ q
        keep = min(top_k, scores.shape[0])
        top = np.argpartition(-scores, keep - 1)[:keep]
        top = top[np.argsort(-scores[top], kind="stable")]
        return [(int(sorted_idx[i]), float(scores[i])) for i in top]

    def _search_full(self, mm, q: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        keep = min(top_k, self.rows)
        best_rows = np.empty(0, dtype=np.int64)
        best_scores = np.empty(0, dtype=np.float32)
        chunk = max(settings.VEC_CHUNK_ROWS, 1024)
        for start in range(0, self.rows, chunk):
            end = min(start + chunk, self.rows)
            scores = np.asarray(mm[start:end], dtype=np.float32) @ q
            local_keep = min(keep, scores.shape[0])
            local = np.argpartition(-scores, local_keep - 1)[:local_keep]
            best_rows = np.concatenate([best_rows, local + start])
            best_scores = np.concatenate([best_scores, scores[local]])
            if best_scores.shape[0] > keep * 4:
                order = np.argsort(-best_scores, kind="stable")[:keep]
                best_rows, best_scores = best_rows[order], best_scores[order]
        order = np.argsort(-best_scores, kind="stable")[:keep]
        return [(int(best_rows[i]), float(best_scores[i])) for i in order]

    def _ensure_ann(self, mm):
        """Build an hnswlib index once, if the package is installed."""
        if self._ann is not None and self._ann_rows == self.rows:
            return self._ann
        try:
            import hnswlib  # noqa: PLC0415
        except ImportError:
            return None
        with self._lock:
            index = hnswlib.Index(space="cosine", dim=self.dim)
            index.init_index(max_elements=self.rows, ef_construction=200, M=16)
            chunk = max(settings.VEC_CHUNK_ROWS, 1024)
            for start in range(0, self.rows, chunk):
                end = min(start + chunk, self.rows)
                index.add_items(np.asarray(mm[start:end]), np.arange(start, end))
            index.set_ef(max(64, 0))
            self._ann = index
            self._ann_rows = self.rows
            return index


_store: VectorStore | None = None
_store_lock = threading.Lock()


def get_store() -> VectorStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = VectorStore()
    return _store


def reset_store() -> None:
    global _store
    with _store_lock:
        _store = None
