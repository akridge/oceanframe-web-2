from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from curation import images, service, settings
from curation.models import CurationEvaluationPrediction, CurationEvaluationRun, CurationItem, CurationVersionInput

SPLITS = ("train", "val", "test")
ACTIVE = ("queued", "staging", "running")


def directory(run_id: str) -> Path:
    return settings.eval_dir() / "runs" / run_id


def get_run(db: Session, project_id: str, run_id: str) -> CurationEvaluationRun:
    run = db.scalar(select(CurationEvaluationRun).where(
        CurationEvaluationRun.id == run_id, CurationEvaluationRun.project_id == project_id))
    if run is None:
        raise HTTPException(404, "Evaluation not found in this project")
    return run


def _unscored_count(run: CurationEvaluationRun) -> int:
    """Patches left unscored. A run that failed while staging has no provenance yet, only missing.json."""
    recorded = (run.provenance or {}).get("unscored")
    if recorded is not None:
        return recorded.get("count", 0)
    path = directory(run.id) / "missing.json"
    try:
        return len(json.loads(path.read_text(encoding="utf-8"))) if path.is_file() else 0
    except (OSError, ValueError):
        return 0


def summary(run: CurationEvaluationRun) -> dict:
    unscored = _unscored_count(run)
    return {
        "id": run.id, "name": run.specification.get("name", run.id), "project_id": run.project_id,
        "version_id": run.version_id, "classifier_id": run.classifier_id, "status": run.status,
        "available": run.status == "completed", "splits": run.specification.get("splits", []),
        "expected_count": run.expected_count, "scored_count": run.scored_count,
        "unscored_count": unscored, "has_missing": unscored > 0,
        "cancel_requested": run.cancel_requested, "error_message": run.error_message,
        "specification": run.specification, "provenance": run.provenance, "results": run.results,
        "created_at": run.created_at, "completed_at": run.completed_at,
    }


def matrix(db: Session, run: CurationEvaluationRun, split: str, normalization: str) -> dict:
    if run.status != "completed":
        raise HTTPException(409, "Only completed evaluations have a confusion matrix")
    selected = run.specification["splits"]
    if split != "all" and split not in selected:
        raise HTTPException(409, "This split has not been evaluated")
    labels = run.specification["class_names"]
    query = select(CurationEvaluationPrediction.actual_label, CurationEvaluationPrediction.predicted_label,
                   func.count()).where(CurationEvaluationPrediction.run_id == run.id)
    if split != "all":
        query = query.where(CurationEvaluationPrediction.split == split)
    grouped = db.execute(query.group_by(CurationEvaluationPrediction.actual_label,
                                       CurationEvaluationPrediction.predicted_label)).all()
    rows = {label: 0 for label in labels}
    columns = dict(rows)
    correct = dict(rows)
    for actual, predicted, count in grouped:
        rows[actual] += count
        columns[predicted] += count
        if actual == predicted:
            correct[actual] += count
    scored = sum(rows.values())
    cells = [{"actual": actual, "predicted": predicted, "count": count,
              "value": count / rows[actual] if normalization == "row" else
                       count / columns[predicted] if normalization == "column" else float(count)}
             for actual, predicted, count in grouped]
    unscored_by_split = (run.provenance or {}).get("unscored", {}).get("by_split", {})
    unscored = sum(unscored_by_split.values()) if split == "all" else unscored_by_split.get(split, 0)
    requested = run.specification.get("requested_split_counts", run.specification["split_counts"])
    expected = sum(requested.values()) if split == "all" else requested.get(split, scored)
    return {
        "matrix_source": "evaluation", "evaluation_id": run.id, "drilldown_kind": "evaluation_predictions",
        "actual_axis": "source", "normalization": normalization, "split": split, "available_splits": selected,
        "scored_count": scored, "unscored_count": unscored, "correct_count": sum(correct.values()),
        "accuracy": sum(correct.values()) / scored if scored else 0.0,
        "model": {**run.provenance, "name": run.specification["model_name"], "model_sha256": run.specification["model_sha256"],
                  "dataset_version": run.specification["version"], "dataset_split": split,
                  "source": "published_version_evaluation", "provenance_complete": unscored == 0},
        "coverage": {"expected": expected, "scored": scored,
                     "mixed_splits": split == "all"},
        "metrics": run.results.get(split, {}), "cells": cells,
        "rows": [{"label": label, "total": rows[label], "correct": correct[label],
                  "metric": correct[label] / rows[label] if rows[label] else 0.0} for label in labels],
        "columns": [{"label": label, "total": columns[label], "correct": correct[label],
                     "metric": correct[label] / columns[label] if columns[label] else 0.0} for label in labels],
    }


def _removed_ids(run: CurationEvaluationRun):
    """The run's project items that were removed since: hidden from evidence unless asked for.

    Filtered in the query, not afterwards, so the offset cursors stay exact."""
    return select(CurationItem.id).where(CurationItem.project_id == run.project_id,
                                         CurationItem.removed == True)  # noqa: E712


def prediction_page(db: Session, run: CurationEvaluationRun, *, split: str, actual_label: str | None,
                    predicted_label: str | None, offset: int, limit: int, sort_by: str,
                    include_removed: bool = False) -> dict:
    if run.status != "completed":
        raise HTTPException(409, "Evaluation evidence is not complete")
    if split != "all" and split not in run.specification["splits"]:
        raise HTTPException(409, "This split has not been evaluated")
    query = select(CurationEvaluationPrediction).where(CurationEvaluationPrediction.run_id == run.id)
    if not include_removed:
        query = query.where(CurationEvaluationPrediction.item_id.not_in(_removed_ids(run)))
    for field, value in (("split", split if split != "all" else None), ("actual_label", actual_label),
                         ("predicted_label", predicted_label)):
        if value:
            query = query.where(getattr(CurationEvaluationPrediction, field) == value)
    order = {"confidence_desc": CurationEvaluationPrediction.confidence.desc(),
             "confidence_asc": CurationEvaluationPrediction.confidence.asc(),
             "filename": CurationEvaluationPrediction.file_name, "dataset": CurationEvaluationPrediction.item_id}[sort_by]
    found = db.scalars(query.order_by(order, CurationEvaluationPrediction.id).offset(offset).limit(limit + 1)).all()
    ids = [row.item_id for row in found[:limit]]
    current = {item.id: item for item in db.scalars(select(CurationItem).where(
        CurationItem.project_id == run.project_id, CurationItem.id.in_(ids)))} if ids else {}
    items = []
    for row in found[:limit]:
        item = current.get(row.item_id)
        linked = item is not None and item.source_key == row.source_key
        items.append({
            "asset_id": f"{run.id}:{row.id}", "prediction_id": row.id, "split": row.split,
            "file_name": row.file_name, "actual_label": row.actual_label, "predicted_label": row.predicted_label,
            "top1_confidence": row.confidence, "top_predictions": row.top_predictions,
            "image_sha256": row.image_sha256,
            "preview_url": f"/api/curation/projects/{run.project_id}/evaluations/{run.id}/predictions/{row.id}/image",
            "mutable": linked, "curation_link": {"item_id": item.id if linked else None,
                "revision": item.revision if linked else None, "match_status": "unique" if linked else "unmatched",
                "current_label": item.current_label if linked else None, "included": item.included if linked else None,
                "source_key": row.source_key},
        })
    return {"items": items, "returned": len(items),
            "next_cursor": str(offset + len(items)) if len(found) > limit else None}


def correct_draft(db: Session, run: CurationEvaluationRun, prediction_id: int, request, user, confirm_test: bool):
    prediction = db.scalar(select(CurationEvaluationPrediction).where(
        CurationEvaluationPrediction.run_id == run.id, CurationEvaluationPrediction.id == prediction_id))
    if run.status != "completed" or prediction is None:
        raise HTTPException(404, "Completed evaluation prediction not found")
    if prediction.split == "test" and not confirm_test:
        raise HTTPException(409, "Confirm that this test-informed correction changes only a future dataset version")
    item = service.get_item_or_404(db, run.project_id, prediction.item_id)
    if item.source_key != prediction.source_key:
        raise HTTPException(409, "Evaluation evidence no longer matches this project item")
    return service.update_item(db, run.project_id, item.id, request, user, evaluation_context={
        "run_id": run.id, "version_id": run.version_id, "split": prediction.split,
        "actual_label": prediction.actual_label, "predicted_label": prediction.predicted_label,
    })


def inventory_page(db: Session, run: CurationEvaluationRun, *, split: str, labels: list[str], offset: int, limit: int,
                   include_removed: bool = False) -> dict:
    query = select(CurationVersionInput).where(CurationVersionInput.version_id == run.version_id)
    if not include_removed:
        query = query.where(CurationVersionInput.item_id.not_in(_removed_ids(run)))
    if split != "all":
        query = query.where(CurationVersionInput.split == split)
    if labels:
        query = query.where(CurationVersionInput.label.in_(labels))
    found = db.scalars(query.order_by(CurationVersionInput.item_id).offset(offset).limit(limit + 1)).all()
    ids = [row.item_id for row in found[:limit]]
    current = {item.id: item for item in db.scalars(select(CurationItem).where(
        CurationItem.project_id == run.project_id, CurationItem.id.in_(ids)))} if ids else {}
    items = []
    for row in found[:limit]:
        item = current.get(row.item_id)
        linked = item is not None and item.source_key == row.source_key
        items.append({"asset_id": f"input:{run.version_id}:{row.item_id}", "input_item_id": row.item_id,
            "split": row.split, "file_name": row.file_name, "actual_label": row.label, "predicted_label": None,
            "top1_confidence": None, "top_predictions": [], "mutable": linked,
            "preview_url": (f"/api/curation/projects/{run.project_id}/evaluations/{run.id}/inputs/{row.item_id}/image"
                            if images.needs_fresh_crop(row) and row.row_px is not None
                            else "/api/curation/image?" + urlencode({"uri": row.crop_uri})),
            "curation_link": {"item_id": row.item_id if linked else None, "revision": item.revision if linked else None,
                "match_status": "unique" if linked else "unmatched", "current_label": item.current_label if linked else None}})
    return {"items": items, "returned": len(items), "next_cursor": str(offset + len(items)) if len(found) > limit else None}


def correct_input(db: Session, run: CurationEvaluationRun, item_id: int, request, user, confirm_test: bool):
    frozen = db.get(CurationVersionInput, (run.version_id, item_id))
    if frozen is None:
        raise HTTPException(404, "Input is not a member of this evaluation's dataset version")
    if frozen.split == "test" and not confirm_test:
        raise HTTPException(409, "Confirm that this test-informed correction changes only a future dataset version")
    item = service.get_item_or_404(db, run.project_id, item_id)
    if item.source_key != frozen.source_key:
        raise HTTPException(409, "Dataset input no longer matches this project item")
    return service.update_item(db, run.project_id, item_id, request, user, evaluation_context={
        "run_id": run.id, "version_id": run.version_id, "split": frozen.split, "actual_label": frozen.label,
        "evidence_kind": "dataset_member",
    })