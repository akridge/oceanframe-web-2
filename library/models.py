"""
Plain data carriers shared between the pipeline, the API, and the exporters.

Rows come out of SQLite as ``sqlite3.Row``; these helpers turn them into the
JSON shapes the UI consumes, so the field naming lives in exactly one place.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass
class ObjectRef:
    """One object discovered by a storage backend, before it is indexed."""
    uri:    str
    key:    str        # path relative to the source root
    size:   int
    mtime:  float
    etag:   str


@dataclass
class Detection:
    """A single model output.  Box is normalised cx, cy, w, h (YOLO order)."""
    label: str
    conf:  float
    x:     float = 0.0
    y:     float = 0.0
    w:     float = 0.0
    h:     float = 0.0
    model: str = ""
    # Normalised polygon(s) as JSON, e.g. "[[x1,y1,x2,y2,...]]" — the exact
    # shape YOLO segmentation labels want, so export is a straight copy.
    mask: str = ""

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "conf":  round(self.conf, 4),
            "box":   [round(self.x, 5), round(self.y, 5), round(self.w, 5), round(self.h, 5)],
            "model": self.model,
            "has_mask": bool(self.mask),
        }


@dataclass
class QualityMetrics:
    blur:       float = 0.0
    brightness: float = 0.0
    contrast:   float = 0.0
    color_cast: float = 1.0
    quality:    float = 0.0
    phash:      str = ""
    width:      int = 0
    height:     int = 0


@dataclass
class AnnotatorStatus:
    name:      str
    available: bool
    detail:    str = ""
    labels:    list[str] = field(default_factory=list)
    # Weights are still loading in the background: not known to be usable yet,
    # but not known to be broken either.
    loading:   bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "available": self.available,
            "detail": self.detail,
            "labels": self.labels[:200],
            "loading": self.loading,
        }


def asset_to_dict(row: sqlite3.Row, score: float | None = None) -> dict:
    """Serialise an asset row for the grid/detail views."""
    out = {
        "id":         row["id"],
        "uri":        row["uri"],
        "folder":     row["folder"],
        "name":       row["name"],
        "ext":        row["ext"],
        "size":       row["size"],
        "mtime":      row["mtime"],
        "width":      row["width"],
        "height":     row["height"],
        "phash":      row["phash"],
        "blur":       round(row["blur"], 2),
        "brightness": round(row["brightness"], 1),
        "contrast":   round(row["contrast"], 1),
        "color_cast": round(row["color_cast"], 3),
        "quality":    round(row["quality"], 1),
        "status":     row["status"],
        "thumb":      f"/api/library/thumb/{row['id']}",
        "preview":    f"/api/library/preview/{row['id']}",
        "full":       f"/api/library/image/{row['id']}",
        "has_embed":  row["embed_row"] >= 0,
    }
    keys = row.keys()
    if "source_label" in keys:
        out["source"] = row["source_label"]
    if "tags" in keys:
        out["tags"] = [t for t in (row["tags"] or "").split("\x1f") if t]
    if "det_labels" in keys:
        # GROUP_CONCAT(DISTINCT ...) cannot take a custom separator, so detection
        # labels arrive comma-joined while tags use the unit separator.
        out["labels"] = sorted({t.strip() for t in (row["det_labels"] or "").split(",") if t.strip()})
    if "error" in keys and row["error"]:
        out["error"] = row["error"]
    if score is not None:
        out["score"] = round(float(score), 4)
    return out


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"
