"""
Pinned model-evaluation artifacts: a fixed test-split confusion matrix.

The artifacts are produced offline (OASIS's ``evaluate_t3_v0.py``: torch,
ultralytics, scikit-learn) and dropped into ``$CURATION_EVAL_DIR/v0/``:
``confusion_matrix.json``, ``metrics.json``, ``provenance.json`` and
``predictions.csv``. They are checked against the exact model and dataset
revisions they claim, so a matrix is never shown against the wrong model.

When they are not installed the evaluation is reported as unavailable and the
page offers only the imported-suggestions matrix — rather than OASIS's 503.
"""
from __future__ import annotations

import base64
import csv
import json
import math
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode
from urllib.request import urlopen

from fastapi import HTTPException

from curation import settings

EVALUATION_ID = "t3-v0-test"
EVALUATION_NAME = "T3 v0 fixed test"
EXPECTED_MODEL_REVISION = "77e10ec10d33dfb7f0a9ebabc542b2008bb96637"
EXPECTED_DATASET_REVISION = "b47bff4ecdcdb026d31d1a154ff267227ff0f1b6"
REQUIRED = ("confusion_matrix.json", "metrics.json", "provenance.json", "predictions.csv")


def split_location() -> tuple[str, str]:
    """(bucket, prefix) of the train/val/test class folders."""
    root = settings.split_root()
    without_scheme = root.removeprefix("gs://")
    bucket, _, prefix = without_scheme.partition("/")
    return bucket, prefix.rstrip("/")


def _directory() -> Path:
    return settings.eval_dir().resolve() / "v0"


def status() -> dict:
    directory = _directory()
    missing = [name for name in REQUIRED if not (directory / name).is_file()]
    return {
        "id": EVALUATION_ID, "name": EVALUATION_NAME, "available": not missing,
        "missing": missing, "directory": str(directory),
        "splits": [split for split in ("train", "val", "test")
                   if all((directory if split == "test" else directory / split).joinpath(name).is_file()
                          for name in REQUIRED)],
    }


def _evaluation_dir(evaluation_id: str, split: str = "test") -> Path:
    if evaluation_id != EVALUATION_ID:
        raise HTTPException(status_code=404, detail="Evaluation not found")
    state = status()
    if not state["available"]:
        raise HTTPException(
            status_code=404,
            detail=f"The {EVALUATION_NAME} evaluation is not installed on this server "
                   f"(missing {', '.join(state['missing'])} in {state['directory']}).",
        )
    if split not in {"train", "val", "test"}:
        raise HTTPException(status_code=422, detail="Choose a dataset split")
    directory = _directory() if split == "test" else _directory() / split
    if any(not (directory / name).is_file() for name in REQUIRED):
        raise HTTPException(status_code=409, detail=f"The {split} split has no installed model predictions")
    return directory


@lru_cache(maxsize=4)
def _load_json(path: str, modified_ns: int):
    del modified_ns
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _json_file(directory: Path, name: str):
    path = directory / name
    return _load_json(str(path), path.stat().st_mtime_ns)


def load_evaluation_matrix(evaluation_id: str, normalization: str, split: str = "test") -> dict:
    if split == "all":
        parts = [load_evaluation_matrix(evaluation_id, normalization, selected) for selected in status()["splits"]]
        if not parts:
            _evaluation_dir(evaluation_id)
        labels = [row["label"] for row in parts[0]["rows"]]
        if any([row["label"] for row in part["rows"]] != labels or
               part["model"]["model_sha256"] != parts[0]["model"]["model_sha256"] for part in parts):
            raise HTTPException(409, "Benchmark splits use different models or class ordering")
        counts = Counter()
        row_totals, column_totals, diagonal = Counter(), Counter(), Counter()
        for part in parts:
            for cell in part["cells"]:
                counts[(cell["actual"], cell["predicted"])] += cell["count"]
                row_totals[cell["actual"]] += cell["count"]
                column_totals[cell["predicted"]] += cell["count"]
                if cell["actual"] == cell["predicted"]:
                    diagonal[cell["actual"]] += cell["count"]
        scored = sum(row_totals.values())
        result = {**parts[0], "split": "all", "scored_count": scored, "correct_count": sum(diagonal.values()),
                  "accuracy": sum(diagonal.values()) / scored if scored else 0.0,
                  "model": {**parts[0]["model"], "dataset_split": "all", "component_splits": status()["splits"]},
                  "coverage": {"expected": scored, "scored": scored, "mixed_splits": len(parts) > 1}}
        result["cells"] = [{"actual": actual, "predicted": predicted, "count": count,
                            "value": count / row_totals[actual] if normalization == "row" else
                                     count / column_totals[predicted] if normalization == "column" else float(count)}
                           for (actual, predicted), count in counts.items()]
        result["rows"] = [{"label": label, "total": row_totals[label], "correct": diagonal[label],
                           "metric": diagonal[label] / row_totals[label] if row_totals[label] else 0.0} for label in labels]
        result["columns"] = [{"label": label, "total": column_totals[label], "correct": diagonal[label],
                              "metric": diagonal[label] / column_totals[label] if column_totals[label] else 0.0} for label in labels]
        result["metrics"] = {"top1_accuracy": result["accuracy"], "top5_accuracy":
                             sum(part["metrics"]["top5_accuracy"] * part["scored_count"] for part in parts) / scored if scored else 0.0}
        return result
    directory = _evaluation_dir(evaluation_id, split)
    matrix = _json_file(directory, "confusion_matrix.json")
    metrics = _json_file(directory, "metrics.json")
    provenance = _json_file(directory, "provenance.json")
    labels = matrix.get("labels") or []
    counts = matrix.get("counts") or []

    if provenance.get("status") != "completed":
        raise HTTPException(status_code=409, detail="Evaluation is not complete")
    if provenance.get("model_revision") != EXPECTED_MODEL_REVISION:
        raise HTTPException(status_code=409, detail="Unexpected model revision")
    if provenance.get("dataset_revision") != EXPECTED_DATASET_REVISION:
        raise HTTPException(status_code=409, detail="Unexpected dataset revision")
    if provenance.get("dataset_split") != split:
        raise HTTPException(status_code=409, detail="Evaluation provenance names a different dataset split")
    if labels != provenance.get("class_names"):
        raise HTTPException(status_code=409, detail="Evaluation class names do not match provenance")
    if len(counts) != len(labels) or any(len(row) != len(labels) for row in counts):
        raise HTTPException(status_code=409, detail="Evaluation matrix dimensions are invalid")

    row_totals = [sum(int(v) for v in row) for row in counts]
    column_totals = [sum(int(counts[r][c]) for r in range(len(labels))) for c in range(len(labels))]
    scored = sum(row_totals)
    if scored != int(metrics.get("images") or 0):
        raise HTTPException(status_code=409, detail="Evaluation matrix total does not match metrics")

    cells = []
    for r, actual in enumerate(labels):
        for c, predicted in enumerate(labels):
            count = int(counts[r][c])
            if not count:
                continue
            value = count / row_totals[r] if normalization == "row" else \
                count / column_totals[c] if normalization == "column" else float(count)
            cells.append({"actual": actual, "predicted": predicted, "count": count, "value": value})

    diagonal = [int(counts[i][i]) for i in range(len(labels))]
    return {
        "split": split, "available_splits": status()["splits"],
        "coverage": {"expected": scored, "scored": scored, "mixed_splits": False},
        "matrix_source": "t3_v0_test", "evaluation_id": evaluation_id,
        "drilldown_kind": "evaluation_predictions", "actual_axis": "source",
        "normalization": normalization, "scored_count": scored, "unscored_count": 0,
        "correct_count": sum(diagonal), "accuracy": float(metrics["top1_accuracy"]),
        "model": {
            "name": "YOLO11m-cls NOAA Pacific T3 v0", "source": "pinned_test_evaluation",
            "provenance_complete": True,
            **{key: provenance[key] for key in (
                "model_repository", "model_revision", "model_sha256", "dataset_repository",
                "dataset_revision", "dataset_split", "evaluated_images", "completed_at",
            )},
        },
        "metrics": {key: metrics[key] for key in (
            "top1_accuracy", "top5_accuracy", "balanced_accuracy", "macro_precision", "macro_recall",
            "macro_f1", "weighted_precision", "weighted_recall", "weighted_f1",
        )},
        "rows": [{"label": label, "total": row_totals[i], "correct": diagonal[i],
                  "metric": diagonal[i] / row_totals[i] if row_totals[i] else 0.0} for i, label in enumerate(labels)],
        "columns": [{"label": label, "total": column_totals[i], "correct": diagonal[i],
                     "metric": diagonal[i] / column_totals[i] if column_totals[i] else 0.0} for i, label in enumerate(labels)],
        "cells": cells,
    }


def list_evaluation_predictions(evaluation_id: str, *, actual_label: Optional[str], predicted_label: Optional[str],
                                after_row: int, limit: int, sort_by: str = "dataset", split: str = "test") -> dict:
    if split == "all":
        matches = []
        for selected in status()["splits"]:
            page = list_evaluation_predictions(evaluation_id, actual_label=actual_label, predicted_label=predicted_label,
                                              after_row=0, limit=2147483647, sort_by="dataset", split=selected)
            matches.extend(page["items"])
        if sort_by in {"confidence_desc", "confidence_asc"}:
            matches.sort(key=lambda item: (item["top1_confidence"] * (-1 if sort_by == "confidence_desc" else 1),
                                           item["split"], item["relative_path"]))
        elif sort_by == "filename":
            matches.sort(key=lambda item: (item["relative_path"], item["split"]))
        page = matches[after_row:after_row + limit]
        return {"items": page, "next_cursor": after_row + len(page) if len(matches) > after_row + limit else None,
                "returned": len(page)}
    path = _evaluation_dir(evaluation_id, split) / "predictions.csv"
    matches = []
    with path.open("r", encoding="utf-8", newline="") as stream:
        for row_id, row in enumerate(csv.DictReader(stream), start=1):
            if actual_label and row["actual_label"] != actual_label:
                continue
            if predicted_label and row["predicted_label"] != predicted_label:
                continue
            predictions = [{"rank": 1, "label": row["predicted_label"], "confidence": float(row["top1_confidence"])}]
            for rank in range(2, 6):
                label = row.get(f"top{rank}_label") or ""
                if label:
                    predictions.append({"rank": rank, "label": label,
                                        "confidence": float(row.get(f"top{rank}_confidence") or 0)})
            matches.append({
                "split": split,
                "row_id": row_id, "relative_path": row["relative_path"], "gcs_uri": row["gcs_uri"],
                "actual_label": row["actual_label"], "predicted_label": row["predicted_label"],
                "top1_confidence": float(row["top1_confidence"]),
                "top5_correct": row["top5_correct"].lower() == "true", "top_predictions": predictions,
            })
    if sort_by == "confidence_desc":
        matches.sort(key=lambda item: (-item["top1_confidence"], item["relative_path"]))
    elif sort_by == "confidence_asc":
        matches.sort(key=lambda item: (item["top1_confidence"], item["relative_path"]))
    elif sort_by == "filename":
        matches.sort(key=lambda item: item["relative_path"])
    elif sort_by != "dataset":
        raise HTTPException(status_code=422, detail="Invalid evaluation sort")
    window = matches[after_row: after_row + limit + 1]
    page = window[:limit]
    return {"items": page, "next_cursor": after_row + len(page) if len(window) > limit and page else None,
            "returned": len(page)}


def validate_artifacts(directory: Path, split: str, *, require_benchmark_shape: bool = True) -> dict:
    missing = [name for name in REQUIRED if not (directory / name).is_file()]
    if missing:
        raise ValueError(f"Missing evaluation files: {', '.join(missing)}")
    matrix = json.loads((directory / "confusion_matrix.json").read_text(encoding="utf-8"))
    metrics = json.loads((directory / "metrics.json").read_text(encoding="utf-8"))
    provenance = json.loads((directory / "provenance.json").read_text(encoding="utf-8"))
    labels = matrix["labels"]
    if not labels or len(set(labels)) != len(labels) or labels != provenance.get("class_names"):
        raise ValueError("Evaluation class names do not match provenance")
    if provenance.get("status") != "completed" or provenance.get("model_revision") != EXPECTED_MODEL_REVISION or \
            provenance.get("dataset_revision") != EXPECTED_DATASET_REVISION or provenance.get("dataset_split") != split:
        raise ValueError("Evaluation is incomplete or does not match the pinned model, dataset revision and split")
    digest = provenance.get("model_sha256", "")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("Model SHA256 is missing or invalid")
    index = {label: position for position, label in enumerate(labels)}
    counts = [[0] * len(labels) for _ in labels]
    seen, correct_top5 = set(), 0
    with (directory / "predictions.csv").open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            path = row["relative_path"]
            if not path or path in seen or row["actual_label"] not in index or row["predicted_label"] not in index:
                raise ValueError("Prediction identities or labels are invalid")
            seen.add(path)
            confidence = float(row["top1_confidence"])
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError("Prediction confidence is invalid")
            if row["top5_correct"].lower() not in {"true", "false"}:
                raise ValueError("Prediction top-five correctness is invalid")
            counts[index[row["actual_label"]]][index[row["predicted_label"]]] += 1
            correct_top5 += row["top5_correct"].lower() == "true"
    total = len(seen)
    if not total or counts != matrix["counts"] or total != metrics["images"] or total != provenance.get("evaluated_images"):
        raise ValueError("Prediction CSV, confusion matrix and provenance counts disagree")
    if not math.isclose(sum(counts[position][position] for position in range(len(labels))) / total,
                        float(metrics["top1_accuracy"]), abs_tol=1e-8) or \
            not math.isclose(correct_top5 / total, float(metrics["top5_accuracy"]), abs_tol=1e-8):
        raise ValueError("Prediction-derived accuracy does not match metrics")
    if require_benchmark_shape and (len(labels) != 67 or (split == "test" and total != 56864)):
        raise ValueError("The original T3 benchmark must have 67 classes and 56,864 test images")
    return provenance


def _encode_cursor(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: Optional[str]) -> dict:
    if not cursor:
        return {"class_index": 0, "page_token": None}
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Invalid split cursor") from exc


def _list_public_objects(bucket: str, prefix: str, page_token: Optional[str], limit: int) -> tuple[list[str], Optional[str]]:
    query = {"prefix": prefix, "maxResults": max(1, min(limit, 1000))}
    if page_token:
        query["pageToken"] = page_token
    url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o?{urlencode(query)}"
    with urlopen(url, timeout=60) as response:  # noqa: S310 — fixed Google API host
        payload = json.load(response)
    return [item["name"] for item in payload.get("items", [])], payload.get("nextPageToken")


def list_split_crops(*, split: str, labels: list[str], cursor: Optional[str], limit: int) -> dict:
    if split not in {"train", "val"}:
        raise HTTPException(status_code=422, detail="Split must be train or val")
    selected = sorted(set(labels))
    if not selected:
        raise HTTPException(status_code=422, detail="Choose at least one matrix class")
    bucket, prefix_root = split_location()
    state = _decode_cursor(cursor)
    class_index = int(state.get("class_index") or 0)
    page_token = state.get("page_token")
    items, next_cursor = [], None

    while class_index < len(selected) and len(items) < limit:
        label = selected[class_index]
        names, next_token = _list_public_objects(bucket, f"{prefix_root}/{split}/{label}/", page_token, limit - len(items))
        for name in names:
            if name.endswith("/"):
                continue
            file_name = Path(name).name
            items.append({
                "asset_id": f"{split}:{label}:{file_name}", "split": split, "gcs_uri": f"gs://{bucket}/{name}",
                "file_name": file_name, "actual_label": label, "predicted_label": None,
                "top1_confidence": None, "top_predictions": [],
            })
        if next_token:
            next_cursor = _encode_cursor({"class_index": class_index, "page_token": next_token})
            break
        class_index += 1
        page_token = None
        if class_index < len(selected):
            next_cursor = _encode_cursor({"class_index": class_index, "page_token": None})
    return {"items": items, "next_cursor": next_cursor, "returned": len(items)}
