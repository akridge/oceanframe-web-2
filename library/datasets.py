"""
Datasets: turn a search result into something a training run can consume.

A dataset is a named, frozen selection of assets plus a split assignment.  It is
built from a query (or an explicit selection), and exported as YOLO detection,
YOLO segmentation, YOLO classification, COCO, or a plain manifest.

Splitting deserves a note.  The default is ``by_folder``: every asset in one
folder lands in the same split.  Survey imagery is burst-shot, so a random split
puts near-identical frames on both sides of the train/val line and reports a
validation score that is really a memorisation score.  Grouping by folder — one
transect, one dive, one deployment — is the cheap fix, and ``by_duplicate``
goes further and groups on the perceptual hash.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import time
import zipfile
from pathlib import PurePosixPath

from library import db, search, settings
from library.jobs import Job
from library.models import asset_to_dict
from library.quality import phash_distance

SPLIT_MODES = ("by_folder", "by_duplicate", "random", "all_train")
EXPORT_KINDS = ("yolo-detect", "yolo-seg", "yolo-classify", "coco", "csv", "manifest")
DEFAULT_RATIOS = {"train": 0.7, "val": 0.2, "test": 0.1}


# ── Deterministic assignment ──────────────────────────────────────────────────

def _bucket(key: str) -> float:
    """Stable 0-1 position for a group key, so re-running keeps the same split."""
    digest = hashlib.sha1(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:6], "big") / float(1 << 48)


def _split_for(position: float, ratios: dict[str, float]) -> str:
    cumulative = 0.0
    for name in ("train", "val", "test"):
        cumulative += ratios.get(name, 0.0)
        if position < cumulative:
            return name
    return "train"


def _group_keys(rows: list, mode: str) -> dict[int, str]:
    """asset id -> group key, per the chosen split mode."""
    if mode == "by_folder":
        return {r["id"]: r["folder"] or "/" for r in rows}
    if mode == "random":
        return {r["id"]: r["uri"] for r in rows}
    if mode == "all_train":
        return {r["id"]: "" for r in rows}

    # by_duplicate: union near-identical phashes into one group.
    groups: dict[int, str] = {}
    representatives: list[tuple[str, str]] = []      # (phash, key)
    for row in rows:
        phash = row["phash"] or ""
        key = None
        if phash:
            for other, other_key in representatives:
                if phash_distance(phash, other) <= settings.DUPE_DISTANCE:
                    key = other_key
                    break
        if key is None:
            key = phash or row["uri"]
            if phash:
                representatives.append((phash, key))
        groups[row["id"]] = key
    return groups


def assign_splits(rows: list, mode: str = "by_folder", ratios: dict | None = None) -> dict[int, str]:
    ratios = {**DEFAULT_RATIOS, **(ratios or {})}
    if mode == "all_train":
        return {r["id"]: "train" for r in rows}
    keys = _group_keys(rows, mode if mode in SPLIT_MODES else "by_folder")
    return {asset_id: _split_for(_bucket(key), ratios) for asset_id, key in keys.items()}


# ── CRUD ──────────────────────────────────────────────────────────────────────

def create(
    name: str,
    *,
    query: dict | None = None,
    asset_ids: list[int] | None = None,
    notes: str = "",
    split_mode: str = "by_folder",
    ratios: dict | None = None,
) -> dict:
    """Freeze a selection into a dataset.  Re-creating the same name replaces it."""
    db.init_db()
    ids = list(asset_ids) if asset_ids else search.candidate_ids(query or {})
    if not ids:
        raise ValueError("Selection is empty — nothing to build a dataset from.")

    conn = db.connect()
    rows = []
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        marks = ",".join("?" * len(chunk))
        rows.extend(conn.execute(
            f"SELECT id, uri, folder, phash FROM assets WHERE id IN ({marks})", chunk
        ).fetchall())

    splits = assign_splits(rows, split_mode, ratios)
    spec = {
        "query": query or {}, "split_mode": split_mode,
        "ratios": {**DEFAULT_RATIOS, **(ratios or {})}, "size": len(rows),
    }

    with db.write() as write_conn:
        write_conn.execute(
            "INSERT INTO datasets(name, notes, spec_json, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET notes=excluded.notes, spec_json=excluded.spec_json, "
            "created_at=excluded.created_at",
            (name, notes, json.dumps(spec), time.time()),
        )
        dataset_id = int(write_conn.execute("SELECT id FROM datasets WHERE name = ?", (name,)).fetchone()["id"])
        write_conn.execute("DELETE FROM dataset_items WHERE dataset_id = ?", (dataset_id,))
        write_conn.executemany(
            "INSERT INTO dataset_items(dataset_id, asset_id, split) VALUES (?, ?, ?)",
            [(dataset_id, r["id"], splits[r["id"]]) for r in rows],
        )
    return describe(dataset_id)


def describe(dataset_id: int) -> dict:
    conn = db.connect()
    row = conn.execute("SELECT * FROM datasets WHERE id = ?", (dataset_id,)).fetchone()
    if not row:
        raise KeyError(f"No dataset {dataset_id}")
    counts = {
        r["split"]: r["n"] for r in conn.execute(
            "SELECT split, COUNT(*) AS n FROM dataset_items WHERE dataset_id = ? GROUP BY split",
            (dataset_id,),
        )
    }
    labels = [
        {"name": r["label"], "count": r["n"]} for r in conn.execute(
            "SELECT d.label, COUNT(*) AS n FROM detections d "
            "JOIN dataset_items di ON di.asset_id = d.asset_id "
            "WHERE di.dataset_id = ? GROUP BY d.label ORDER BY n DESC", (dataset_id,),
        )
    ]
    return {
        "id": row["id"], "name": row["name"], "notes": row["notes"],
        "spec": json.loads(row["spec_json"] or "{}"), "created_at": row["created_at"],
        "splits": counts, "size": sum(counts.values()), "labels": labels,
    }


def list_datasets() -> list[dict]:
    conn = db.connect()
    return [describe(int(r["id"])) for r in conn.execute("SELECT id FROM datasets ORDER BY created_at DESC")]


def delete(dataset_id: int) -> None:
    with db.write() as conn:
        conn.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))


def set_split(dataset_id: int, asset_ids: list[int], split: str) -> int:
    if split not in {"train", "val", "test"}:
        raise ValueError(f"Unknown split '{split}'")
    with db.write() as conn:
        conn.executemany(
            "UPDATE dataset_items SET split = ? WHERE dataset_id = ? AND asset_id = ?",
            [(split, dataset_id, i) for i in asset_ids],
        )
    return len(asset_ids)


def items(dataset_id: int, split: str = "", limit: int = 500, offset: int = 0) -> dict:
    conn = db.connect()
    clause = "di.dataset_id = ?"
    params: list = [dataset_id]
    if split:
        clause += " AND di.split = ?"
        params.append(split)
    rows = conn.execute(
        f"""
        SELECT a.*, di.split, s.label AS source_label,
               (SELECT GROUP_CONCAT(t.name, char(31)) FROM asset_tags at JOIN tags t ON t.id = at.tag_id
                 WHERE at.asset_id = a.id) AS tags,
               (SELECT GROUP_CONCAT(DISTINCT d.label) FROM detections d WHERE d.asset_id = a.id) AS det_labels
          FROM dataset_items di
          JOIN assets a  ON a.id = di.asset_id
          JOIN sources s ON s.id = a.source_id
         WHERE {clause} ORDER BY a.folder, a.name LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    ).fetchall()
    out = []
    for row in rows:
        item = asset_to_dict(row)
        item["split"] = row["split"]
        out.append(item)
    total = conn.execute(
        f"SELECT COUNT(*) FROM dataset_items di WHERE {clause}", params
    ).fetchone()[0]
    return {"items": out, "total": total}


# ── Export ────────────────────────────────────────────────────────────────────

def export(
    job: Job,
    dataset_id: int,
    *,
    kind: str = "yolo-detect",
    include_images: bool = True,
    conf: float = 0.25,
    labels: list[str] | None = None,
    tag_prefix: str = "",
) -> dict:
    """
    Build a zip in ``library_data/exports`` and return its path and stats.

    ``include_images=False`` produces a labels-and-manifest-only archive, which
    is what you want when the training job will read the images straight out of
    the bucket.
    """
    if kind not in EXPORT_KINDS:
        raise ValueError(f"Unknown export kind '{kind}'. One of: {', '.join(EXPORT_KINDS)}")

    meta = describe(dataset_id)
    conn = db.connect()
    rows = conn.execute(
        """
        SELECT a.id, a.uri, a.folder, a.name, a.ext, a.width, a.height, a.quality,
               a.phash, a.blur, a.brightness, a.contrast, a.color_cast, di.split, s.root
          FROM dataset_items di
          JOIN assets a  ON a.id = di.asset_id
          JOIN sources s ON s.id = a.source_id
         WHERE di.dataset_id = ? ORDER BY di.split, a.folder, a.name
        """,
        (dataset_id,),
    ).fetchall()

    settings.ensure_dirs()
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in meta["name"]).strip("-") or "dataset"
    out_path = settings.EXPORT_DIR / f"{slug}_{kind}_{int(time.time())}.zip"

    job.set_total(len(rows))
    job.log(f"Exporting {len(rows):,} assets as {kind}")

    classes = _class_list(conn, dataset_id, kind, conf, labels, tag_prefix)
    class_index = {name: i for i, name in enumerate(classes)}
    written = skipped = 0
    coco = _CocoBuilder(classes) if kind == "coco" else None
    manifest_rows: list[dict] = []

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            if job.cancelled:
                break
            split = row["split"]
            rel_name = _flat_name(row["folder"], row["name"])
            entry = {
                "id": row["id"], "uri": row["uri"], "split": split, "folder": row["folder"],
                "name": row["name"], "quality": round(row["quality"], 1),
                "width": row["width"], "height": row["height"],
            }

            if kind == "yolo-classify":
                cls = _classify_label(conn, row["id"], tag_prefix)
                if cls is None:
                    skipped += 1
                    job.advance(1)
                    continue
                entry["class"] = cls
                arc = f"{split}/{cls}/{rel_name}"
            else:
                arc = f"images/{split}/{rel_name}"

            if include_images:
                data = _read_asset(row)
                if data is None:
                    skipped += 1
                    job.advance(1)
                    continue
                archive.writestr(arc, data)
                entry["path"] = arc

            if kind in {"yolo-detect", "yolo-seg"}:
                lines = _yolo_lines(conn, row, class_index, conf, kind == "yolo-seg")
                archive.writestr(f"labels/{split}/{PurePosixPath(rel_name).stem}.txt", "\n".join(lines))
                entry["boxes"] = len(lines)
            elif coco is not None:
                coco.add(row, conn, conf, arc if include_images else row["uri"])

            manifest_rows.append(entry)
            written += 1
            job.advance(1, f"{written:,} written · {skipped:,} skipped")

        # Sidecars that make the archive self-describing.
        archive.writestr("manifest.json", json.dumps(
            {"dataset": meta, "kind": kind, "classes": classes, "include_images": include_images,
             "conf": conf, "count": written, "items": manifest_rows}, indent=2))
        archive.writestr("manifest.csv", _manifest_csv(manifest_rows))
        archive.writestr("README.md", _readme(meta, kind, classes, include_images))

        if kind in {"yolo-detect", "yolo-seg"}:
            archive.writestr("data.yaml", _data_yaml(classes, meta))
        if coco is not None:
            archive.writestr("annotations.json", json.dumps(coco.build(), indent=2))

    return {
        "path": str(out_path), "file": out_path.name, "kind": kind,
        "written": written, "skipped": skipped, "classes": classes,
        "bytes": out_path.stat().st_size, "dataset": meta["name"],
    }


def _flat_name(folder: str, name: str) -> str:
    """
    Flatten folder into the filename.

    YOLO expects a flat images/ tree, but two transects routinely both contain
    ``img_0001.jpg``, so the folder has to survive somewhere — and keeping it in
    the name means a prediction file still tells you which dive it came from.
    """
    prefix = folder.replace("/", "__")
    return f"{prefix}__{name}" if prefix else name


def _read_asset(row) -> bytes | None:
    from library.storage import get_backend  # noqa: PLC0415

    try:
        return get_backend(row["root"]).read_bytes(row["uri"])
    except Exception:
        return None


def _class_list(conn, dataset_id: int, kind: str, conf: float, labels: list[str] | None, tag_prefix: str) -> list[str]:
    if labels:
        return list(dict.fromkeys(labels))
    if kind == "yolo-classify":
        prefix = tag_prefix or "class:"
        rows = conn.execute(
            "SELECT DISTINCT t.name FROM asset_tags at JOIN tags t ON t.id = at.tag_id "
            "JOIN dataset_items di ON di.asset_id = at.asset_id "
            "WHERE di.dataset_id = ? AND t.name LIKE ? ORDER BY t.name",
            (dataset_id, f"{prefix}%"),
        ).fetchall()
        return [r["name"].split(":", 1)[1] for r in rows]
    rows = conn.execute(
        "SELECT DISTINCT d.label FROM detections d JOIN dataset_items di ON di.asset_id = d.asset_id "
        "WHERE di.dataset_id = ? AND d.conf >= ? ORDER BY d.label",
        (dataset_id, conf),
    ).fetchall()
    return [r["label"] for r in rows]


def _classify_label(conn, asset_id: int, tag_prefix: str) -> str | None:
    """Highest-scoring tag under the prefix becomes the class folder."""
    prefix = tag_prefix or "class:"
    row = conn.execute(
        "SELECT t.name FROM asset_tags at JOIN tags t ON t.id = at.tag_id "
        "WHERE at.asset_id = ? AND t.name LIKE ? ORDER BY at.score DESC, t.name LIMIT 1",
        (asset_id, f"{prefix}%"),
    ).fetchone()
    if not row:
        return None
    return row["name"].split(":", 1)[1].replace("/", "-").replace(" ", "_")


def _yolo_lines(conn, row, class_index: dict[str, int], conf: float, segmentation: bool) -> list[str]:
    detections = conn.execute(
        "SELECT label, conf, x, y, w, h, mask FROM detections WHERE asset_id = ? AND conf >= ?",
        (row["id"], conf),
    ).fetchall()
    lines: list[str] = []
    for det in detections:
        cls = class_index.get(det["label"])
        if cls is None:
            continue
        if segmentation and det["mask"]:
            try:
                polygons = json.loads(det["mask"])
                for polygon in polygons:
                    if len(polygon) >= 6:
                        coords = " ".join(f"{v:.6f}" for v in polygon)
                        lines.append(f"{cls} {coords}")
                continue
            except (ValueError, TypeError):
                pass    # fall through to the box form
        lines.append(f"{cls} {det['x']:.6f} {det['y']:.6f} {det['w']:.6f} {det['h']:.6f}")
    return lines


class _CocoBuilder:
    def __init__(self, classes: list[str]) -> None:
        self.classes = classes
        self.index = {name: i + 1 for i, name in enumerate(classes)}
        self.images: list[dict] = []
        self.annotations: list[dict] = []

    def add(self, row, conn, conf: float, file_name: str) -> None:
        image_id = int(row["id"])
        width, height = int(row["width"] or 0), int(row["height"] or 0)
        self.images.append({
            "id": image_id, "file_name": file_name, "width": width, "height": height,
            "oceanframe_quality": round(row["quality"], 1), "source_uri": row["uri"],
        })
        for det in conn.execute(
            "SELECT label, conf, x, y, w, h FROM detections WHERE asset_id = ? AND conf >= ?",
            (image_id, conf),
        ):
            category = self.index.get(det["label"])
            if category is None:
                continue
            bw, bh = det["w"] * width, det["h"] * height
            self.annotations.append({
                "id": len(self.annotations) + 1, "image_id": image_id, "category_id": category,
                "bbox": [round(det["x"] * width - bw / 2, 2), round(det["y"] * height - bh / 2, 2),
                         round(bw, 2), round(bh, 2)],
                "area": round(bw * bh, 2), "iscrowd": 0, "score": round(det["conf"], 4),
            })

    def build(self) -> dict:
        return {
            "info": {"description": "Exported from OceanFrame Image Library", "date_created": time.strftime("%Y-%m-%d")},
            "images": self.images,
            "annotations": self.annotations,
            "categories": [{"id": i + 1, "name": n, "supercategory": "object"} for i, n in enumerate(self.classes)],
        }


def _manifest_csv(rows: list[dict]) -> str:
    if not rows:
        return "id,uri,split\n"
    columns = list(dict.fromkeys(key for row in rows for key in row))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _data_yaml(classes: list[str], meta: dict) -> str:
    names = "\n".join(f"  {i}: {name}" for i, name in enumerate(classes))
    return (
        f"# {meta['name']} — exported from the OceanFrame image library\n"
        "path: .\n"
        "train: images/train\n"
        "val: images/val\n"
        "test: images/test\n\n"
        f"nc: {len(classes)}\n"
        f"names:\n{names or '  0: object'}\n"
    )


def _readme(meta: dict, kind: str, classes: list[str], include_images: bool) -> str:
    splits = ", ".join(f"{k}={v}" for k, v in sorted(meta["splits"].items()))
    body = [
        f"# {meta['name']}",
        "",
        f"Exported from the OceanFrame image library as `{kind}`.",
        "",
        f"- **Items**: {meta['size']} ({splits})",
        f"- **Split mode**: {meta['spec'].get('split_mode', 'by_folder')}",
        f"- **Classes**: {', '.join(classes) if classes else '(none)'}",
        f"- **Images included**: {'yes' if include_images else 'no — read them from the source URIs in manifest.csv'}",
        "",
        "`manifest.csv` carries the source URI, folder, split and OceanFrame quality",
        "score for every item, so the selection stays traceable back to the bucket.",
    ]
    if meta["spec"].get("query"):
        body += ["", "## Query", "", "```json", json.dumps(meta["spec"]["query"], indent=2), "```"]
    return "\n".join(body) + "\n"
