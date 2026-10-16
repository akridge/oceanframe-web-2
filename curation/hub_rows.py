"""
Rows of a Hugging Face dataset, as curation sees them.

Pure logic plus one reader. Which parquet file holds which split, which
column is the image, label, file name and split, and where each patch lives.
v1 covers datasets whose rows *point at* images (gs:// or
storage.googleapis.com); datasets that embed image bytes are refused.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath
from typing import Iterator

from fastapi import HTTPException

from curation import huggingface, settings
from curation.splits import normalize_split

IMAGE_COLUMNS = ("gcs_uri", "image_uri", "image_url", "image_path", "file_path", "path")
LABEL_COLUMNS = ("label_name", "label", "class_name", "class", "category")
FILE_COLUMNS = ("file_name", "filename")
SPLIT_COLUMNS = ("split",)
_GCS_HTTPS = re.compile(r"^https://storage\.googleapis\.com/([^/]+)/(.+)$")
_SPLIT_IN_NAME = re.compile(r"(?:^|[/_-])(train|training|validation|valid|val|test)(?:[/_.-]|$)")


def _split(name: str) -> str:
    # Splits outside train/val/test (e.g. "unlabeled") keep their own name.
    try:
        return normalize_split(name) or name
    except ValueError:
        return name.lower()


def parquet_by_split(card: dict, files: list[dict]) -> dict[str, list[str]]:
    """split -> parquet paths: the card's configs (globs allowed), else the file names.

    A split the card names but that resolves to no file falls back to file names.
    """
    parquet = sorted(entry["path"] for entry in files if entry["path"].endswith(".parquet"))
    result: dict[str, list[str]] = {}
    declared: set[str] = set()
    configs = card.get("configs") or []
    # The "default" config, else the first one (cards may name theirs anything).
    chosen = [config for config in configs if config.get("config_name", "default") == "default"] or configs[:1]
    for config in chosen:
        data_files = config.get("data_files") or []
        if isinstance(data_files, dict):
            data_files = [{"split": key, "path": value} for key, value in data_files.items()]
        for spec in data_files:
            if not isinstance(spec, dict):
                continue
            split = _split(spec.get("split", "train"))
            declared.add(split)
            patterns = spec.get("path")
            for pattern in [patterns] if isinstance(patterns, str) else (patterns or []):
                if not isinstance(pattern, str):
                    continue
                for path in parquet:
                    if fnmatch.fnmatchcase(path, pattern) and path not in result.get(split, []):
                        result.setdefault(split, []).append(path)
    taken = {path for paths in result.values() for path in paths}
    unresolved = declared - set(result)
    nothing_resolved = not result
    for path in parquet:
        if path in taken:
            continue
        match = _SPLIT_IN_NAME.search(path.lower())
        inferred = _split(match.group(1)) if match else "train"
        if nothing_resolved or inferred in unresolved:
            result.setdefault(inferred, []).append(path)
    return result


def features(card: dict) -> dict[str, str]:
    """column -> dtype from the card; 'image' for an embedded image, 'class_label' for ClassLabel."""
    info = card.get("dataset_info") or {}
    if isinstance(info, list):
        info = info[0] if info else {}
    columns = {}
    for feature in info.get("features") or []:
        dtype = feature.get("dtype")
        if dtype is None and "_type" in feature:
            dtype = feature["_type"]
        if isinstance(dtype, dict):
            dtype = "class_label" if "class_label" in dtype else "struct"
        if "class_label" in feature:
            dtype = "class_label"
        columns[feature["name"]] = str(dtype or "unknown").lower()
    return columns


def class_names(card: dict, label_column: str) -> list[str]:
    info = card.get("dataset_info") or {}
    if isinstance(info, list):
        info = info[0] if info else {}
    for feature in info.get("features") or []:
        if feature.get("name") == label_column:
            names = ((feature.get("class_label") or {}).get("names")
                     or ((feature.get("dtype") or {}) if isinstance(feature.get("dtype"), dict) else {})
                     .get("class_label", {}).get("names"))
            if isinstance(names, dict):
                # Hub dicts are keyed by index: place each name at its index, gaps keep the index.
                indexed = {int(key): name for key, name in names.items() if str(key).isdigit()}
                return [indexed.get(i, str(i)) for i in range(max(indexed) + 1)] if indexed else []
            return list(names or [])
    return []


def _first(columns: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    return next((name for name in candidates if name in columns), None)


def detect_columns(columns: dict[str, str]) -> dict[str, str | None]:
    if "image" in columns.values() or columns.get("image") == "struct":
        raise HTTPException(422, "This dataset embeds image bytes; only datasets whose rows point at "
                                 "gs:// images are supported for now")
    mapping = {"image": _first(columns, IMAGE_COLUMNS), "label": _first(columns, LABEL_COLUMNS),
               "file_name": _first(columns, FILE_COLUMNS), "split": _first(columns, SPLIT_COLUMNS)}
    missing = [role for role in ("image", "label") if not mapping[role]]
    if missing:
        raise HTTPException(422, f"Could not find a {' or '.join(missing)} column; the dataset has "
                                 f"{', '.join(sorted(columns)) or 'no described columns'}")
    return mapping


def row_fields(row: dict, split: str, column_map: dict, names: list[str]) -> tuple[str, str, str]:
    """(split, label, file name) for one row."""
    value = row.get(column_map["label"])
    if isinstance(value, int) and not isinstance(value, bool) and names:
        value = names[value] if 0 <= value < len(names) else str(value)
    label = str(value if value is not None else "").strip().lstrip("*").strip()
    image = str(row.get(column_map["image"]) or "")
    file_name = str(row.get(column_map["file_name"]) or "") if column_map.get("file_name") else ""
    file_name = file_name or PurePosixPath(image).name
    try:
        row_split = normalize_split(row.get(column_map["split"])) if column_map.get("split") else None
    except ValueError:
        row_split = None  # an unrecognised split in the row: the file's split stands
    if not label or not file_name:
        raise ValueError("label and file name are required")
    return row_split or split, label, file_name


def crop_uri(row: dict, split: str, label: str, file_name: str, column_map: dict, location: dict) -> str:
    mode = location.get("mode", "column")
    if mode == "column":
        value = str(row.get(column_map["image"]) or "").strip()
        https = _GCS_HTTPS.match(value)
        if https:
            return f"gs://{https.group(1)}/{https.group(2)}"
        if not value.startswith("gs://"):
            raise HTTPException(422, f"Image column {column_map['image']!r} holds {value[:80]!r}; "
                                     "choose a gs:// root for the patches instead")
        return value
    root = settings.split_root() if mode == "env_root" else str(location.get("root") or "")
    if not root.startswith("gs://"):
        raise HTTPException(422, "The patch root must be a gs:// location")
    return f"{root.rstrip('/')}/{split}/{label}/{file_name}"


def source_image_stem(label: str, file_name: str) -> str | None:
    """'ACAS_AGU-641_2025_A_09_9.png' -> 'AGU-641_2025_A_09' (curation/importer.py names crops this way)."""
    stem = PurePosixPath(file_name).stem
    if not stem.startswith(f"{label}_"):
        return None
    rest, _, ordinal = stem[len(label) + 1:].rpartition("_")
    return rest if rest and ordinal.isdigit() else None


ROW_KEY = "__hf_row__"  # zero-based row index within its parquet file, added to each row read


def _read_file(entry, path: str, columns: list[str], batch_size: int) -> Iterator[dict]:
    import pyarrow.parquet as pq  # noqa: PLC0415

    size = next((item.get("size") or 0 for item in entry.files if item["path"] == path), 0)
    if size > settings.hub_max_bytes():
        raise HTTPException(413, f"{path} exceeds CURATION_HUB_MAX_BYTES")
    local = huggingface.download(entry.kind, entry.repo_id, entry.revision, path)
    reader = pq.ParquetFile(local)
    # No columns named (a card without features): read them all.
    wanted = [name for name in dict.fromkeys(columns) if name in reader.schema_arrow.names] \
        if columns else reader.schema_arrow.names
    index = 0
    for batch in reader.iter_batches(batch_size=batch_size, columns=wanted):
        for row in batch.to_pylist():
            row[ROW_KEY] = index
            index += 1
            yield row


def iter_rows(entry, splits: list[str] | None, columns: list[str],
              batch_size: int = 2000) -> Iterator[tuple[str, dict]]:
    for split, paths in parquet_by_split(entry.card, entry.files).items():
        if splits and split not in splits:
            continue
        for path in paths:
            for row in _read_file(entry, path, columns, batch_size):
                yield split, row


def sample_rows(entry, n: int = 5) -> list[tuple[str, dict]]:
    """The first n rows, smallest parquet files first: a check should not download the biggest shard."""
    sizes = {item["path"]: item.get("size") or 0 for item in entry.files}
    files = sorted(((sizes.get(path, 0), path, split)
                    for split, paths in parquet_by_split(entry.card, entry.files).items() for path in paths))
    rows = []
    for _, path, split in files:
        for row in _read_file(entry, path, list(features(entry.card)) or [], n):
            rows.append((split, row))
            if len(rows) >= n:
                return rows
    return rows
