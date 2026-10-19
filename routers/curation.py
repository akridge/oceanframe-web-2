"""
Classification curation API — /api/curation/*

Same surface as OASIS's /api/v1/classification-curation, with roles:
viewers read and export; curators label, include, review and add points; leads
create projects and run imports; deleting a project needs its owner or an admin.
"""
from __future__ import annotations

import csv
import json
import re
from io import StringIO
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from appdb.engine import get_db
from auth.deps import current_user, require_lead, require_role_from
from auth.service import CurrentUser
from curation import breakdown as split_breakdown
from curation import members
from curation import availability, claims, cleanup, editing, evaluation,evaluation_runner, evaluation_runs, image_cache, images, model_registry, presence, quality, scoring, service, settings, storage, taxonomy, versions
from curation.importer import curation_import_pool
from curation.models import (
    CurationClassifier, CurationDatasetVersion, CurationEvaluationPrediction, CurationEvaluationRun,
    CurationEvent, CurationImportOperation, CurationItem, CurationProject, CurationVersionInput,
)
from curation.schemas import (
    CurationActivity, CurationBulkUpdate, CurationBulkUpdateResponse, CurationClassifyRequest,
    CurationClassifyResponse, CurationConfusionMatrix, CurationDatasetStatus, CurationFilterFacets,
    CurationImportCreate, CurationImportResponse, CurationItemCreate, CurationItemPage,
    CurationItemResponse, CurationItemUpdate, CurationPointCreate, CurationProjectCreate,
    CurationProjectResponse, CurationQualityBulkRequest, CurationQualityBulkResponse, CurationQualityReviewRequest,
    CurationSourceImageDetail, SourceEditBatch, SuggestRequest,
    CurationSourceImagePage, CurationStatsResponse, EvaluationPredictionPage, MatrixAssetPage,
    TaxonomyNodeResponse,
)

router = APIRouter(prefix="/curation", tags=["curation"])
# Creating projects and registering, uploading and validating evaluation models
# is open to app-wide curators by default; CURATION_CREATE_ROLE moves the bar.
# Work inside a project (imports, evaluations, publishing) follows project roles.
require_creator = require_role_from(settings.create_role)

ORIGIN = Query(None, pattern="^(source|classifier|curated)$")
GROUP_BY = Query("label", pattern="^(label|region|island|survey_year|annotator)$")
PNG_CACHE = {"Cache-Control": "private, max-age=3600"}


def _parse_labels(labels: Optional[str]) -> list[str]:
    return list(dict.fromkeys(v.strip().lstrip("*") for v in (labels or "").split(",") if v.strip()))


def _parse_item_ids(item_ids: Optional[str]) -> list[int]:
    try:
        return list(dict.fromkeys(int(v.strip()) for v in (item_ids or "").split(",") if v.strip()))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="item_ids must be comma-separated integers") from exc


def _filters(
    item_ids: Optional[str] = None, labels: Optional[str] = None, review_state: Optional[str] = None,
    quality_decision: Optional[str] = None, included: Optional[bool] = None, region: Optional[str] = None,
    island: Optional[str] = None, year: Optional[int] = None, site: Optional[str] = None,
    image_search: Optional[str] = None, annotator: Optional[str] = None, label_origin: Optional[str] = ORIGIN,
    source_labels: Optional[str] = None, model_labels: Optional[str] = None,
    removed: str = Query("hide", pattern="^(hide|only|any)$"),
    source_missing: str = Query("any", pattern="^(hide|only|any)$"),
    # Named apart from the matrix endpoints' own split / evaluation_id / actual_label parameters.
    item_split: Optional[str] = Query(None, pattern="^(train|val|test|unassigned)$"),
    eval_run: Optional[str] = None, eval_actual: Optional[str] = None, eval_predicted: Optional[str] = None,
    eval_split: Optional[str] = Query(None, pattern="^(train|val|test|all)$"),
    # Server quality scores (curation/quality.py): only patches the job has scored match these.
    quality_suggestion: Optional[str] = Query(None, pattern="^(accept|manual_review|reject|attention)$"),
    quality_flag: Optional[str] = Query(None, pattern="^[a-z_]{1,30}$"),
    quality_max_score: Optional[int] = Query(None, ge=0, le=100),
) -> dict:
    """The shared filter set, as a dependency, so every view filters identically."""
    return {
        "item_ids": _parse_item_ids(item_ids), "labels": _parse_labels(labels), "review_state": review_state,
        "quality_decision": quality_decision, "included": included, "region": region, "island": island,
        "year": year, "site": site, "image_search": image_search, "annotator": annotator,
        "label_origin": label_origin, "source_labels": _parse_labels(source_labels),
        "model_labels": _parse_labels(model_labels), "removed": removed, "source_missing": source_missing,
        "split": item_split, "evaluation_id": eval_run, "evaluation_actual": eval_actual,
        "evaluation_predicted": eval_predicted, "evaluation_split": eval_split,
        "quality_suggestion": quality_suggestion, "quality_flag": quality_flag, "quality_max_score": quality_max_score,
    }


# ── Projects and reference data ─────────────────────────────────────────────────

@router.post("/projects", response_model=CurationProjectResponse, status_code=201)
def create_project(request: CurationProjectCreate, user: CurrentUser = Depends(require_creator),
                   db: Session = Depends(get_db)):
    project = service.create_project(db, request, user)
    return {**CurationProjectResponse.model_validate(project).model_dump(), "my_role": "lead"}


@router.get("/projects", response_model=list[CurationProjectResponse])
def list_projects(user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    roles = members.roles_for(db, user)
    rows = db.scalars(select(CurationProject).order_by(CurationProject.updated_at.desc())).all()
    return [{**CurationProjectResponse.model_validate(p).model_dump(), "my_role": roles.get(p.id)} for p in rows]


@router.get("/projects/{project_id}", response_model=CurationProjectResponse)
def get_project(project_id: str, user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    project = service.get_project_or_404(db, project_id)
    return {**CurationProjectResponse.model_validate(project).model_dump(),
            "my_role": members.project_role(db, project_id, user)}


@router.delete("/projects/{project_id}")
def delete_project(project_id: str, confirm_name: str = Query(..., min_length=1),
                   user: CurrentUser = Depends(require_lead), db: Session = Depends(get_db)):
    service.delete_project(db, service.get_project_or_404(db, project_id), confirm_name, user)
    return {"deleted": True, "project_id": project_id}


@router.get("/import-sources")
def import_sources():
    return {"sources": settings.import_sources()}


@router.get("/taxonomy", response_model=list[TaxonomyNodeResponse])
def get_taxonomy():
    return taxonomy.load_taxonomy_nodes()


@router.get("/classification-models")
def classification_models():
    return {"models": scoring.available_models()}


@router.get("/evaluations")
def list_evaluations():
    return {"evaluations": [evaluation.status()]}


class RegisterClassifierIn(BaseModel):
    ref: str = Field(min_length=1, max_length=1000)
    name: str = Field(min_length=1, max_length=200)
    trusted: bool = False


class EvaluationRunIn(BaseModel):
    version: int = Field(ge=1)
    classifier_id: str = Field(min_length=36, max_length=36)
    splits: list[str] = Field(default_factory=lambda: ["train", "val", "test"], min_length=1, max_length=3)
    image_size: int = Field(default=224, ge=32, le=1024)
    batch_size: int = Field(default=32, ge=1, le=128)
    device: str = Field(default="cpu", pattern="^(cpu|[0-9])$")


class EvaluationCorrectionIn(CurationItemUpdate):
    confirm_test: bool = False
    split: str = Field(default="test", pattern="^(train|val|test)$")


@router.get("/evaluation-models")
def list_evaluation_models(db: Session = Depends(get_db)):
    from library import modelcache
    return {"models": [model_registry.summary(model) for model in db.scalars(
                select(CurationClassifier).order_by(CurationClassifier.created_at.desc()))],
            "configured": [{"ref": entry["ref"], "name": entry["label"]} for entry in modelcache.catalog()],
            "worker_available": model_registry.worker_available(),
            "trusted_checkpoints_enabled": settings.trusted_checkpoints()}


@router.post("/evaluation-models", status_code=201)
def register_evaluation_model(body: RegisterClassifierIn, user: CurrentUser = Depends(require_creator),
                              db: Session = Depends(get_db)):
    return model_registry.summary(model_registry.register_reference(db, body.ref, name=body.name,
                                                                   trusted=body.trusted, user=user))


@router.post("/evaluation-models/upload", status_code=201)
def upload_evaluation_model(file: UploadFile = File(...), name: str = Form(..., min_length=1, max_length=200),
                            trusted: bool = Form(False), user: CurrentUser = Depends(require_creator),
                            db: Session = Depends(get_db)):
    try:
        return model_registry.summary(model_registry.register_stream(db, file.file, name=name,
            filename=file.filename or "", trusted=trusted, user=user))
    finally:
        file.file.close()


@router.post("/evaluation-models/{model_id}/validate", status_code=202)
def validate_evaluation_model(model_id: str, user: CurrentUser = Depends(require_creator), db: Session = Depends(get_db)):
    model = db.get(CurationClassifier, model_id)
    if model is None:
        raise HTTPException(404, "Registered classifier not found")
    model_registry.submit_validation(db, model)
    return model_registry.summary(model)


@router.get("/projects/{project_id}/evaluations")
def project_evaluations(project_id: str, db: Session = Depends(get_db)):
    project = service.get_project_or_404(db, project_id)
    runs = db.scalars(select(CurationEvaluationRun).where(CurationEvaluationRun.project_id == project_id)
                      .order_by(CurationEvaluationRun.created_at.desc())).all()
    pinned = [{**evaluation.status(), "external_benchmark": True}] if project.dataset_mode == "T3" else []
    return {"evaluations": [evaluation_runs.summary(run) for run in runs], "benchmarks": pinned,
            "worker_available": model_registry.worker_available()}


@router.post("/projects/{project_id}/evaluations", status_code=202)
def create_evaluation(project_id: str, body: EvaluationRunIn, user: CurrentUser = Depends(members.require_project_curator),
                      db: Session = Depends(get_db)):
    if len(set(body.splits)) != len(body.splits) or set(body.splits) - set(evaluation_runs.SPLITS):
        raise HTTPException(422, "Choose unique train, val or test splits")
    run = evaluation_runner.start_run(db, project_id, version_number=body.version, classifier_id=body.classifier_id,
        splits=body.splits, image_size=body.image_size, batch_size=body.batch_size, device=body.device, user=user)
    return evaluation_runs.summary(run)


@router.get("/projects/{project_id}/evaluations/{run_id}")
def get_project_evaluation(project_id: str, run_id: str, db: Session = Depends(get_db)):
    return evaluation_runs.summary(evaluation_runs.get_run(db, project_id, run_id))


@router.get("/projects/{project_id}/evaluations/{run_id}/missing.csv")
def evaluation_missing(project_id: str, run_id: str, db: Session = Depends(get_db)):
    run = evaluation_runs.get_run(db, project_id, run_id)
    path = evaluation_runs.directory(run.id) / "missing.json"
    rows = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["item_id", "split", "crop_uri", "reason"])
    # A leading = + - @ would be run as a formula by a spreadsheet; quote it with '.
    safe = lambda value: f"'{value}" if str(value).startswith(("=", "+", "-", "@")) else value  # noqa: E731
    writer.writerows([safe(row["item_id"]), safe(row["split"]), safe(row["crop_uri"]), safe(row.get("reason", ""))]
                     for row in rows)
    return Response(buffer.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="missing-{run.id}.csv"'})


@router.post("/projects/{project_id}/evaluations/{run_id}/cancel")
def cancel_evaluation(project_id: str, run_id: str, user: CurrentUser = Depends(members.require_project_curator), db: Session = Depends(get_db)):
    run = evaluation_runs.get_run(db, project_id, run_id)
    if run.status not in evaluation_runs.ACTIVE:
        raise HTTPException(409, "This evaluation is already finished")
    run.cancel_requested = True
    db.commit()
    evaluation_runner.evaluation_pool.wake()
    return evaluation_runs.summary(run)


@router.patch("/projects/{project_id}/evaluations/{run_id}/predictions/{prediction_id}/item", response_model=CurationItemResponse)
def correct_evaluated_item(project_id: str, run_id: str, prediction_id: int, body: EvaluationCorrectionIn,
                           user: CurrentUser = Depends(members.require_project_curator), db: Session = Depends(get_db)):
    if run_id == evaluation.EVALUATION_ID:
        project = service.get_project_or_404(db, project_id)
        if project.dataset_mode != "T3":
            raise HTTPException(409, "The T3 benchmark cannot curate a T1 project")
        if body.split == "test" and not body.confirm_test:
            raise HTTPException(409, "Confirm this test-informed draft correction")
        page = evaluation.list_evaluation_predictions(run_id, actual_label=None, predicted_label=None,
            after_row=prediction_id - 1, limit=1, split=body.split)
        if prediction_id < 1 or not page["items"] or page["items"][0]["row_id"] != prediction_id:
            raise HTTPException(404, "Benchmark prediction not found")
        row = page["items"][0]
        linked = service.attach_curation_links(db, project_id, [{"actual_label": row["actual_label"],
            "file_name": Path(row["relative_path"]).name}], mutable=True)[0]
        if not linked["mutable"]:
            raise HTTPException(409, "Benchmark example has no unique project match")
        return service.update_item(db, project_id, linked["curation_link"]["item_id"], body, user,
            evaluation_context={"evaluation_id": run_id, "split": body.split, "actual_label": row["actual_label"],
                                "predicted_label": row["predicted_label"], "dataset_revision": evaluation.EXPECTED_DATASET_REVISION})
    return evaluation_runs.correct_draft(db, evaluation_runs.get_run(db, project_id, run_id), prediction_id,
                                         body, user, body.confirm_test)


class CleanupIn(BaseModel):
    actual: str = Field(min_length=1, max_length=100)
    predicted: str = Field(min_length=1, max_length=100)
    action: str = Field(pattern="^(relabel|exclude|flag)$")
    splits: list[str] = Field(default_factory=lambda: ["train", "val"], min_length=1, max_length=3)
    watermark: int = Field(ge=0)
    confirm_test: bool = False
    reason: Optional[str] = Field(default=None, max_length=4000)


@router.get("/projects/{project_id}/evaluations/{run_id}/cleanup")
def cleanup_preview(project_id: str, run_id: str, actual: str, predicted: str,
                    splits: str = Query("train,val", pattern="^(train|val|test)(,(train|val|test)){0,2}$"),
                    db: Session = Depends(get_db)):
    """One matrix cell: patches per split, how many are already cleaned, and the watermark to apply against."""
    return cleanup.preview(db, evaluation_runs.get_run(db, project_id, run_id), actual, predicted, splits.split(","))


@router.post("/projects/{project_id}/evaluations/{run_id}/cleanup")
def cleanup_apply(project_id: str, run_id: str, body: CleanupIn,
                  user: CurrentUser = Depends(members.require_project_curator), db: Session = Depends(get_db)):
    """Relabel, exclude or flag every patch of the cell; patches changed since the preview are left alone."""
    return cleanup.apply(db, evaluation_runs.get_run(db, project_id, run_id), actual=body.actual,
                         predicted=body.predicted, action=body.action, splits=body.splits, watermark=body.watermark,
                         user=user, confirm_test=body.confirm_test, reason=body.reason)


@router.get("/projects/{project_id}/evaluations/{run_id}/cleanup/tally")
def cleanup_tally(project_id: str, run_id: str,
                  splits: str = Query("train,val", pattern="^(train|val|test)(,(train|val|test)){0,2}$"),
                  db: Session = Depends(get_db)):
    """Cleaned N of M per off-diagonal cell and per class."""
    return cleanup.tally(db, evaluation_runs.get_run(db, project_id, run_id), splits.split(","))


@router.patch("/projects/{project_id}/evaluations/{run_id}/inputs/{item_id}/item", response_model=CurationItemResponse)
def correct_dataset_member(project_id: str, run_id: str, item_id: int, body: EvaluationCorrectionIn,
                            user: CurrentUser = Depends(members.require_project_curator), db: Session = Depends(get_db)):
    return evaluation_runs.correct_input(db, evaluation_runs.get_run(db, project_id, run_id), item_id,
                                         body, user, body.confirm_test)


@router.get("/projects/{project_id}/evaluations/{run_id}/predictions/{prediction_id}/image")
def evaluated_crop(project_id: str, run_id: str, prediction_id: int, db: Session = Depends(get_db)):
    run = evaluation_runs.get_run(db, project_id, run_id)
    prediction = db.scalar(select(CurationEvaluationPrediction).where(
        CurationEvaluationPrediction.run_id == run.id, CurationEvaluationPrediction.id == prediction_id))
    if run.status != "completed" or prediction is None:
        raise HTTPException(404, "Evaluation image not found")
    path = evaluation_runs.directory(run.id) / "images" / f"{prediction.item_id}.png"
    if not path.is_file() or evaluation_runner.file_hash(path) != prediction.image_sha256:
        raise HTTPException(409, "Saved evaluation image is missing or changed")
    return FileResponse(path, media_type="image/png", headers=PNG_CACHE)


@router.get("/projects/{project_id}/evaluations/{run_id}/inputs/{item_id}/image")
def evaluated_input_crop(project_id: str, run_id: str, item_id: int, db: Session = Depends(get_db)):
    """A moved, resized or added member of the run's version, cut from its source at the frozen geometry."""
    run = evaluation_runs.get_run(db, project_id, run_id)
    frozen = db.get(CurationVersionInput, (run.version_id, item_id))
    if frozen is None or frozen.row_px is None or frozen.column_px is None:
        raise HTTPException(404, "Input is not a member of this evaluation's dataset version")
    images.require_allowed(db, frozen.source_image_uri)
    try:
        payload = image_cache.patch(frozen.source_image_uri, frozen.row_px, frozen.column_px, frozen.crop_size)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Source image not in the bucket") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail="Source image could not be read") from exc
    return Response(payload, media_type="image/png", headers=PNG_CACHE)


# ── Imports ─────────────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/imports", response_model=CurationImportResponse, status_code=202)
def create_import(project_id: str, request: CurationImportCreate, user: CurrentUser = Depends(members.require_project_curator),
                  db: Session = Depends(get_db)):
    service.get_project_or_404(db, project_id)
    if not settings.allow_local_sources():
        for source in request.sources:
            if not (source.csv_uri.startswith("gs://") and source.root_uri.startswith("gs://")):
                raise HTTPException(status_code=422, detail="Import sources must be gs:// locations")
    operation = CurationImportOperation(
        project_id=project_id, sources=[source.model_dump() for source in request.sources],
        batch_size=request.batch_size, created_by_user_id=user.id, created_by_name=user.label,
    )
    db.add(operation)
    db.commit()
    db.refresh(operation)
    return operation


@router.get("/projects/{project_id}/imports", response_model=list[CurationImportResponse])
def list_imports(project_id: str, db: Session = Depends(get_db)):
    """The project's recent imports, newest first. Availability checks are run as
    operations too, but they are not imports: Dataset status owns them, so they
    are left out here (sources is JSON text, so filtered in Python)."""
    service.get_project_or_404(db, project_id)
    query = (select(CurationImportOperation).where(CurationImportOperation.project_id == project_id)
             .order_by(CurationImportOperation.created_at.desc(), CurationImportOperation.id))
    found: list[CurationImportOperation] = []
    offset = 0
    while len(found) < 20:
        window = db.scalars(query.offset(offset).limit(100)).all()
        found += [op for op in window if not availability.is_check(op) and not quality.is_job(op)]
        if len(window) < 100:
            break
        offset += 100
    return found[:20]


def _operation_or_404(db: Session, project_id: str, operation_id: str) -> CurationImportOperation:
    operation = db.scalar(select(CurationImportOperation).where(
        CurationImportOperation.project_id == project_id, CurationImportOperation.id == operation_id,
    ))
    if operation is None:
        raise HTTPException(status_code=404, detail="Import operation not found")
    return operation


@router.get("/projects/{project_id}/imports/{operation_id}", response_model=CurationImportResponse)
def get_import(project_id: str, operation_id: str, db: Session = Depends(get_db)):
    return _operation_or_404(db, project_id, operation_id)


@router.post("/projects/{project_id}/imports/{operation_id}/cancel", response_model=CurationImportResponse,
             dependencies=[Depends(members.require_project_curator)])
def cancel_import(project_id: str, operation_id: str, db: Session = Depends(get_db)):
    operation = _operation_or_404(db, project_id, operation_id)
    if operation.status == "queued":
        operation.status = "canceled"
        db.commit()
        db.refresh(operation)
    elif operation.status in {"starting", "running"}:
        curation_import_pool.cancel(operation.id)
    return operation


# ── Items ───────────────────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/items", response_model=CurationItemResponse, status_code=201)
def add_item(project_id: str, request: CurationItemCreate, user: CurrentUser = Depends(members.require_project_curator),
             db: Session = Depends(get_db)):
    images.require_allowed(db, request.source_image_uri, request.crop_uri)
    return service.create_item(db, project_id, request, user)


@router.get("/projects/{project_id}/items", response_model=CurationItemPage)
def list_items(project_id: str, after_id: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
               label: Optional[str] = None, with_total: bool = False, hide_claimed: bool = False,
               filters: dict = Depends(_filters), user: CurrentUser = Depends(current_user),
               db: Session = Depends(get_db)):
    service.get_project_or_404(db, project_id)
    query = service.item_query(db, project_id, label=label, **filters)
    if hide_claimed:  # items another curator has open (curation/claims.py)
        query = query.filter(CurationItem.id.not_in(claims.others_hold(project_id, user.id)))
    rows = query.filter(CurationItem.id > after_id).order_by(CurationItem.id).limit(limit + 1).all()
    page = rows[:limit]
    return {"items": page, "next_cursor": page[-1].id if len(rows) > limit and page else None,
            "returned": len(page), "total": query.order_by(None).count() if with_total else None}


@router.patch("/projects/{project_id}/items/{item_id}", response_model=CurationItemResponse)
def update_item(project_id: str, item_id: int, request: CurationItemUpdate,
                user: CurrentUser = Depends(members.require_project_curator), db: Session = Depends(get_db)):
    return service.update_item(db, project_id, item_id, request, user)


@router.post("/projects/{project_id}/items/bulk", response_model=CurationBulkUpdateResponse)
def bulk_update(project_id: str, request: CurationBulkUpdate, user: CurrentUser = Depends(members.require_project_curator),
                db: Session = Depends(get_db)):
    operation_id, updated, conflicts = service.bulk_update_items(db, project_id, request, user)
    return {"operation_id": operation_id, "updated": updated, "conflicts": conflicts}


@router.post("/projects/{project_id}/items/{item_id}/quality", response_model=CurationItemResponse)
def review_item_quality(project_id: str, item_id: int, request: CurationQualityReviewRequest,
                        user: CurrentUser = Depends(members.require_project_curator), db: Session = Depends(get_db)):
    return service.review_quality(db, project_id, item_id, request, user, whole_image=False)


@router.post("/projects/{project_id}/quality/bulk", response_model=CurationQualityBulkResponse)
def bulk_review_quality(project_id: str, request: CurationQualityBulkRequest,
                        user: CurrentUser = Depends(members.require_project_curator), db: Session = Depends(get_db)):
    operation_id, applied, conflicts = service.bulk_review_quality(db, project_id, request, user)
    return {"operation_id": operation_id, "applied": applied, "conflicts": conflicts}


@router.get("/projects/{project_id}/items/{item_id}/crop-preview")
def crop_preview(project_id: str, item_id: int, db: Session = Depends(get_db)):
    project = service.get_project_or_404(db, project_id)
    item = service.get_item_or_404(db, project_id, item_id)
    if images.needs_fresh_crop(item) and item.row_px is not None:
        images.require_allowed(db, item.source_image_uri)
        try:
            return Response(images.crop_from_source(item), media_type="image/png", headers=PNG_CACHE)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Source image not in the bucket") from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=422, detail="Source image could not be read") from exc
    for uri in images.crop_candidates(item, project):
        if not images.is_allowed(db, uri):
            continue  # an item written before the rules, or by a direct DB edit
        try:
            return Response(storage.read_bytes(uri), media_type="image/png", headers=PNG_CACHE)
        except FileNotFoundError:
            continue
    if item.row_px is None or item.column_px is None:
        raise HTTPException(status_code=404, detail="Crop is missing and item has no source coordinates")
    images.require_allowed(db, item.source_image_uri)
    try:
        return Response(images.crop_from_source(item), media_type="image/png", headers=PNG_CACHE)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Source image not in the bucket") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail="Source image could not be decoded") from exc


# ── Source images ───────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/source-images", response_model=CurationSourceImagePage)
def list_source_images(project_id: str, after_id: int = Query(0, ge=0), after_missing: int = Query(0, ge=0, le=1),
                       limit: int = Query(30, ge=1, le=100),
                       handled: str = Query("any", pattern="^(any|hide)$"), hide_claimed: bool = False,
                       filters: dict = Depends(_filters), user: CurrentUser = Depends(current_user),
                       db: Session = Depends(get_db)):
    """*handled=hide* leaves out images with every point reviewed or flagged; *hide_claimed* those
    with a point another curator has open."""
    service.get_project_or_404(db, project_id)
    return service.source_image_page(
        service.item_query(db, project_id, **filters), after_id, limit, after_missing,
        unhandled_only=handled == "hide",
        held_by_others=claims.others_hold(project_id, user.id) if hide_claimed else None)


# ── Claims and the change feed (curation/claims.py) ─────────────────────────────

class ClaimRequest(BaseModel):
    holder: str = Field(min_length=8, max_length=64)
    item_ids: list[int] = Field(default_factory=list, max_length=5000)


@router.post("/projects/{project_id}/claims")
def set_claims(project_id: str, request: ClaimRequest,
               user: CurrentUser = Depends(members.require_project_curator), db: Session = Depends(get_db)):
    """The calling tab's claims become exactly these items; an empty list lets go of all of them."""
    service.get_project_or_404(db, project_id)
    return claims.set_claims(db, project_id, request.holder, request.item_ids, user)


class PresenceRequest(BaseModel):
    holder: str = Field(min_length=8, max_length=64)
    view: str = Field("source", max_length=20)
    detail: Optional[str] = Field(None, max_length=300)
    leaving: bool = False


@router.post("/projects/{project_id}/presence")
def post_presence(project_id: str, request: PresenceRequest, user: CurrentUser = Depends(current_user),
                  db: Session = Depends(get_db)):
    """This tab is here (or, with *leaving*, gone); answers with everyone online, for the Activity tab."""
    service.get_project_or_404(db, project_id)
    if request.leaving:
        presence.leave(db, request.holder, user)
        return {"online": []}
    presence.heartbeat(db, project_id, request.holder, user, request.view, request.detail)
    return {"online": presence.online(db)}


@router.get("/projects/{project_id}/changes")
def get_changes(project_id: str, after_id: Optional[int] = Query(None, ge=0),
                user: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    """Items other users changed after event *after_id*, and the items they have open."""
    service.get_project_or_404(db, project_id)
    return claims.changes(db, project_id, after_id, user)


@router.post("/projects/{project_id}/availability", response_model=CurationImportResponse, status_code=202)
def check_availability(project_id: str, user: CurrentUser = Depends(members.require_project_curator),
                       db: Session = Depends(get_db)):
    service.get_project_or_404(db, project_id)
    return availability.queue_check(db, project_id, user)


@router.get("/projects/{project_id}/availability")
def availability_summary(project_id: str, db: Session = Depends(get_db)):
    service.get_project_or_404(db, project_id)
    return availability.summary(db, project_id)


@router.get("/projects/{project_id}/availability.csv")
def availability_csv(project_id: str, db: Session = Depends(get_db)):
    project = service.get_project_or_404(db, project_id)
    return Response(availability.missing_csv(db, project_id), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{versions._slug(project.name)}_missing_images.csv"'})  # noqa: SLF001


@router.post("/projects/{project_id}/quality-scores", response_model=CurationImportResponse, status_code=202)
def prepare_quality_scores(project_id: str, user: CurrentUser = Depends(members.require_project_lead),
                           db: Session = Depends(get_db)):
    """Score every patch once, in the background (resumable: run it again and it does what is left)."""
    return quality.queue(db, project_id, user)


@router.get("/projects/{project_id}/quality-scores")
def quality_scores(project_id: str, item_ids: Optional[str] = None, db: Session = Depends(get_db)):
    """With item_ids, those patches' scores; without, how far scoring has got."""
    service.get_project_or_404(db, project_id)
    ids = _parse_item_ids(item_ids)
    if ids:
        return {"scores": {str(k): v for k, v in quality.scores_for(db, project_id, ids[:1000]).items()}}
    return quality.summary(db, project_id)


@router.get("/projects/{project_id}/source-images/{first_item_id}", response_model=CurationSourceImageDetail)
def get_source_image(project_id: str, first_item_id: int, db: Session = Depends(get_db)):
    anchor = service.get_item_or_404(db, project_id, first_item_id, "Source image not found")
    items = db.scalars(select(CurationItem).where(
        CurationItem.project_id == project_id, CurationItem.source_image_uri == anchor.source_image_uri,
    ).order_by(CurationItem.id)).all()
    width = height = None
    missing = False
    # Read the image only from a curated bucket; one outside them lists its points without dimensions.
    if images.is_allowed(db, anchor.source_image_uri):
        try:
            width, height = image_cache.dimensions(anchor.source_image_uri)
        except FileNotFoundError:
            missing = True
        except Exception as exc:  # noqa: BLE001 — corrupt object, storage refusal: say so, not a 500
            raise HTTPException(status_code=422, detail="Source image could not be read") from exc
    return {
        "first_item_id": items[0].id, "source_image_uri": anchor.source_image_uri,
        "image_name": Path(anchor.source_image_uri).name, "region": anchor.region, "site": anchor.site,
        "survey_year": anchor.survey_year, "width": width, "height": height, "source_missing": missing,
        "items": items,
    }


@router.post("/projects/{project_id}/source-images/{anchor_item_id}/quality", response_model=CurationItemResponse)
def review_source_image_quality(project_id: str, anchor_item_id: int, request: CurationQualityReviewRequest,
                                user: CurrentUser = Depends(members.require_project_curator), db: Session = Depends(get_db)):
    return service.review_quality(db, project_id, anchor_item_id, request, user, whole_image=True)


@router.post("/projects/{project_id}/source-images/{anchor_item_id}/edits")
def edit_source_image(project_id: str, anchor_item_id: int, batch: SourceEditBatch,
                      user: CurrentUser = Depends(members.require_project_curator), db: Session = Depends(get_db)):
    result = editing.apply_batch(db, project_id, anchor_item_id, batch, user)
    return {**result, "added": [CurationItemResponse.model_validate(i).model_dump(mode="json") for i in result["added"]]}


@router.post("/projects/{project_id}/source-images/{anchor_item_id}/suggest")
def suggest_points(project_id: str, anchor_item_id: int, request: SuggestRequest,
                   user: CurrentUser = Depends(members.require_project_curator), db: Session = Depends(get_db)):
    return editing.suggest(db, project_id, anchor_item_id, request)


@router.post("/projects/{project_id}/source-images/{anchor_item_id}/points",
             response_model=CurationItemResponse, status_code=201)
def add_point(project_id: str, anchor_item_id: int, request: CurationPointCreate,
              user: CurrentUser = Depends(members.require_project_curator), db: Session = Depends(get_db)):
    return service.create_manual_point(db, project_id, anchor_item_id, request, user)


@router.post("/projects/{project_id}/source-images/{anchor_item_id}/classify",
             response_model=CurationClassifyResponse, dependencies=[Depends(members.require_project_curator)])
def classify_neighborhood(project_id: str, anchor_item_id: int, request: CurationClassifyRequest,
                          db: Session = Depends(get_db)):
    anchor = service.get_item_or_404(db, project_id, anchor_item_id, "Source image not found")
    images.require_allowed(db, anchor.source_image_uri)
    return {"model_name": request.model_name,
            "points": scoring.classify_neighborhood(anchor.source_image_uri, request)}


@router.get("/image")
def proxy_image(uri: str = Query(..., min_length=6, max_length=1000), raw_orientation: bool = False,
                db: Session = Depends(get_db)):
    if not images.is_allowed(db, uri):
        raise HTTPException(status_code=403, detail="That image is outside the buckets this deployment curates")
    try:
        payload = image_cache.read_source(uri)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Image not found") from exc
    if raw_orientation:
        payload = images.remove_exif_orientation(payload)
    return Response(payload, media_type=images.media_type(uri), headers=PNG_CACHE)


# ── Facets, status, matrix ──────────────────────────────────────────────────────

@router.get("/projects/{project_id}/facets", response_model=CurationFilterFacets)
def get_facets(project_id: str, filters: dict = Depends(_filters), db: Session = Depends(get_db)):
    service.get_project_or_404(db, project_id)
    filters.pop("item_ids")
    return service.facets(db, project_id, filters)


@router.get("/projects/{project_id}/stats", response_model=CurationStatsResponse)
def get_stats(project_id: str, db: Session = Depends(get_db)):
    return service.project_stats(db, project_id)


@router.get("/projects/{project_id}/status", response_model=CurationDatasetStatus)
def get_status(project_id: str, db: Session = Depends(get_db)):
    return service.dataset_status(db, project_id)


@router.get("/projects/{project_id}/breakdown")
def get_breakdown(project_id: str, by: str = GROUP_BY, db: Session = Depends(get_db)):
    """Included items per class (or region, island, year, annotator) and split, as of now."""
    service.get_project_or_404(db, project_id)
    return split_breakdown.breakdown(db, project_id, by=by)


@router.get("/projects/{project_id}/activity", response_model=CurationActivity)
def get_activity(project_id: str, limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db)):
    return service.activity(db, project_id, limit)


@router.get("/projects/{project_id}/confusion-matrix", response_model=CurationConfusionMatrix)
def get_confusion_matrix(
    project_id: str,
    matrix_source: str = Query("imported_csv", pattern="^(imported_csv|t3_v0_test|evaluation)$"),
    evaluation_id: Optional[str] = None,
    split: str = Query("test", pattern="^(train|val|test|all)$"),
    actual_axis: str = Query("current", pattern="^(current|source)$"),
    normalization: str = Query("row", pattern="^(count|row|column)$"),
    filters: dict = Depends(_filters), db: Session = Depends(get_db),
):
    project = service.get_project_or_404(db, project_id)
    if matrix_source == "evaluation":
        return evaluation_runs.matrix(db, evaluation_runs.get_run(db, project_id, evaluation_id or ""), split, normalization)
    if matrix_source == "t3_v0_test":
        if project.dataset_mode != "T3":
            raise HTTPException(409, "The external T3 benchmark is not a result for a T1 project")
        return evaluation.load_evaluation_matrix(evaluation.EVALUATION_ID, normalization, split)
    # The matrix *is* the source-vs-model breakdown; filtering it on those
    # axes (or on one drilldown's items) would make it describe itself.
    for key in ("item_ids", "source_labels", "model_labels", "label_origin"):
        filters.pop(key)
    return service.confusion_matrix(db, project, filters, actual_axis, normalization)


@router.get("/evaluations/{evaluation_id}/predictions", response_model=EvaluationPredictionPage)
def get_evaluation_predictions(
    evaluation_id: str, actual_label: Optional[str] = None, predicted_label: Optional[str] = None,
    after_row: int = Query(0, ge=0), limit: int = Query(60, ge=1, le=200),
    sort_by: str = Query("dataset", pattern="^(dataset|confidence_desc|confidence_asc|filename)$"),
    removed: str = Query("hide", pattern="^(hide|any)$"),
    split: str = Query("test", pattern="^(train|val|test|all)$"),
):
    return evaluation.list_evaluation_predictions(
        evaluation_id, actual_label=actual_label, predicted_label=predicted_label,
        after_row=after_row, limit=limit, sort_by=sort_by, split=split,
    )


def _hide_removed(assets: list[dict], removed: str) -> list[dict]:
    """Evidence linked to a removed point is hidden unless Show removed asks for it."""
    if removed == "any":
        return assets
    return [asset for asset in assets if not asset["curation_link"].get("removed")]


@router.get("/projects/{project_id}/matrix-assets", response_model=MatrixAssetPage)
def get_matrix_assets(
    project_id: str, split: str = Query(..., pattern="^(test|train|val|all)$"),
    evaluation_id: Optional[str] = None,
    evidence_kind: str = Query("predictions", pattern="^(predictions|inventory)$"),
    actual_label: Optional[str] = None, predicted_label: Optional[str] = None, cursor: Optional[str] = None,
    limit: int = Query(60, ge=1, le=200),
    sort_by: str = Query("dataset", pattern="^(dataset|confidence_desc|confidence_asc|filename)$"),
    removed: str = Query("hide", pattern="^(hide|any)$"),
    db: Session = Depends(get_db),
):
    service.get_project_or_404(db, project_id)
    if evaluation_id and evaluation_id != evaluation.EVALUATION_ID:
        try:
            offset = int(cursor or 0)
            if offset < 0:
                raise ValueError()
        except ValueError as exc:
            raise HTTPException(422, "Invalid evaluation cursor") from exc
        run = evaluation_runs.get_run(db, project_id, evaluation_id)
        if evidence_kind == "inventory":
            return evaluation_runs.inventory_page(db, run, split=split,
                labels=[label for label in (actual_label, predicted_label) if label], offset=offset, limit=limit,
                include_removed=removed == "any")
        return evaluation_runs.prediction_page(db, run, split=split,
            actual_label=actual_label, predicted_label=predicted_label, offset=offset, limit=limit, sort_by=sort_by,
            include_removed=removed == "any")
    scored_benchmark = evaluation_id == evaluation.EVALUATION_ID and evidence_kind == "predictions"
    if evaluation_id == evaluation.EVALUATION_ID and evidence_kind == "inventory" and split not in {"train", "val"}:
        raise HTTPException(422, "Unscored benchmark inventory is available for train and val only")
    if scored_benchmark or split in {"test", "all"}:
        try:
            after_row = int(cursor or 0)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid test cursor") from exc
        page = evaluation.list_evaluation_predictions(
            evaluation.EVALUATION_ID, actual_label=actual_label, predicted_label=predicted_label,
            after_row=after_row, limit=limit, sort_by=sort_by, split=split,
        )
        assets = [{
            "asset_id": f"{item.get('split', split)}:{item['row_id']}", "split": item.get("split", split), "gcs_uri": item["gcs_uri"],
            "file_name": Path(item["relative_path"]).name, "actual_label": item["actual_label"],
            "predicted_label": item["predicted_label"], "top1_confidence": item["top1_confidence"],
            "top_predictions": item["top_predictions"],
            "prediction_id": item["row_id"],
        } for item in page["items"]]
        linked = _hide_removed(service.attach_curation_links(db, project_id, assets, mutable=scored_benchmark), removed)
        return {"items": linked,
                "next_cursor": str(page["next_cursor"]) if page["next_cursor"] is not None else None,
                "returned": len(linked)}

    labels = [label for label in (actual_label, predicted_label) if label]
    page = evaluation.list_split_crops(split=split, labels=labels, cursor=cursor, limit=limit)
    linked = _hide_removed(service.attach_curation_links(db, project_id, page["items"], mutable=True), removed)
    return {"items": linked,
            "next_cursor": page["next_cursor"], "returned": len(linked)}


# ── Export ──────────────────────────────────────────────────────────────────────

EXPORT_COLUMNS = [
    "id", "source_key", "source_image_uri", "crop_uri", "source_label", "current_label", "included",
    "review_state", "row", "column", "region", "island", "site", "year", "split", "annotator",
    "model_label", "model_confidence", "quality_decision", "quality_score", "quality_flags",
    "quality_reviewed_by", "revision", "updated_by", "updated_at",
    # What cutting the patch again needs, and whether it was added or moved here.
    "crop_size", "file_name", "origin", "parent_item_id",
]


def _export_record(item: CurationItem) -> dict:
    return {
        "id": item.id, "source_key": item.source_key, "source_image_uri": item.source_image_uri,
        "crop_uri": item.crop_uri, "source_label": item.source_label, "current_label": item.current_label,
        "included": bool(item.included), "review_state": item.review_state, "row": item.row_px,
        "column": item.column_px, "region": item.region, "island": item.island, "site": item.site,
        "year": item.survey_year, "split": item.split, "annotator": item.annotator,
        "model_label": item.model_label, "model_confidence": item.model_confidence,
        "quality_decision": item.quality_decision, "quality_score": item.quality_score,
        "quality_flags": ";".join(item.quality_flags or []), "quality_reviewed_by": item.quality_reviewed_by,
        "revision": item.revision, "updated_by": item.updated_by_name,
        "updated_at": item.updated_at.isoformat() + "Z" if item.updated_at else None,
        "crop_size": item.crop_size, "file_name": item.file_name, "origin": versions.point_origin(item.metadata_json),
        "parent_item_id": (item.metadata_json or {}).get("parent_item_id"),
    }


def _export_rows(db: Session, project_id: str, export_format: str, filters: dict):
    try:
        query = service.item_query(db, project_id, **filters).order_by(CurationItem.id)
        if export_format == "jsonl":
            for item in query.yield_per(2000):
                yield json.dumps(_export_record(item), separators=(",", ":")) + "\n"
            return
        buffer = StringIO()
        writer = csv.DictWriter(buffer, fieldnames=EXPORT_COLUMNS, lineterminator="\n")
        writer.writeheader()
        yield buffer.getvalue()
        for item in query.yield_per(2000):
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow(_export_record(item))
            yield buffer.getvalue()
    finally:
        db.close()


def _safe_name(project: CurationProject) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", project.name).strip("_") or "curation"


@router.get("/projects/{project_id}/export")
def export_items(project_id: str, export_format: str = Query("csv", pattern="^(csv|jsonl)$"),
                 filters: dict = Depends(_filters), db: Session = Depends(get_db)):
    project = service.get_project_or_404(db, project_id)
    extension = "jsonl" if export_format == "jsonl" else "csv"
    return StreamingResponse(
        _export_rows(db, project_id, export_format, filters),
        media_type="application/x-ndjson" if export_format == "jsonl" else "text/csv",
        headers={"Content-Disposition": f'attachment; filename="{_safe_name(project)}_filtered.{extension}"'},
    )


@router.get("/projects/{project_id}/events/export")
def export_events(project_id: str, db: Session = Depends(get_db)):
    """The whole audit trail: who changed what, when, from what to what, and why."""
    project = service.get_project_or_404(db, project_id)

    def rows():
        buffer = StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["id", "created_at", "event_type", "item_id", "operation_id", "user_id",
                         "curator", "legacy_actor", "reason", "previous", "new"])
        yield buffer.getvalue()
        query = db.query(CurationEvent).filter(CurationEvent.project_id == project_id).order_by(CurationEvent.id)
        for event in query.yield_per(2000):
            buffer.seek(0)
            buffer.truncate(0)
            writer.writerow([
                event.id, event.created_at.isoformat() + "Z", event.event_type, event.item_id,
                event.operation_id, event.user_id, event.curator_name, event.legacy_actor, event.reason,
                json.dumps(event.previous_value) if event.previous_value is not None else "",
                json.dumps(event.new_value) if event.new_value is not None else "",
            ])
            yield buffer.getvalue()
        db.close()

    return StreamingResponse(rows(), media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{_safe_name(project)}_audit.csv"',
    })



# ── Dataset versions ────────────────────────────────────────────────────────────
# Publishing freezes what curators have agreed on as v1, v2, … with a change log:
# what came in, what went out, what was relabelled, and who did it.

class PublishIn(BaseModel):
    notes: Optional[str] = Field(default=None, max_length=4000)


VERSION_FILES = {"manifest.csv": "text/csv", "manifest.json": "application/json", "CHANGELOG.md": "text/markdown"}


def _version_summary(version: CurationDatasetVersion) -> dict:
    changes = version.changes or {}
    return {
        "id": version.id, "version": version.version, "status": version.status, "notes": version.notes,
        "item_count": version.item_count, "statistics": version.statistics or {},
        "changes": changes, "contributors": version.contributors or [],
        "created_by_name": version.created_by_name, "created_at": version.created_at,
        "published_at": version.published_at, "output_uri": version.output_uri,
        "images": (version.artifacts or {}).get("images"), "error_message": version.error_message,
        "files": list(VERSION_FILES) if version.status == "published" else [],
    }


def _version_or_404(db: Session, project_id: str, number: int) -> CurationDatasetVersion:
    version = db.scalar(select(CurationDatasetVersion).where(
        CurationDatasetVersion.project_id == project_id, CurationDatasetVersion.version == number))
    if version is None:
        raise HTTPException(status_code=404, detail=f"No version v{number}")
    return version


@router.get("/projects/{project_id}/versions")
def list_versions(project_id: str, db: Session = Depends(get_db)):
    service.get_project_or_404(db, project_id)
    rows = db.scalars(select(CurationDatasetVersion).where(CurationDatasetVersion.project_id == project_id)
                      .order_by(CurationDatasetVersion.version.desc())).all()
    return {"versions": [_version_summary(v) for v in rows]}


@router.get("/projects/{project_id}/versions/preview")
def preview_version(project_id: str, db: Session = Depends(get_db)):
    """What publishing now would put in and take out, compared with the latest version."""
    service.get_project_or_404(db, project_id)
    return versions.preview(db, project_id)


@router.get("/projects/{project_id}/versions/compare")
def compare_versions(project_id: str, base: str = Query(..., max_length=12), target: str = Query(..., max_length=12),
                     db: Session = Depends(get_db)):
    """What changed between two published versions, or a version and the live draft."""
    service.get_project_or_404(db, project_id)
    if base == target:
        raise HTTPException(status_code=422, detail="Choose two different versions to compare")
    try:
        base_set, base_name = versions.resolve_snapshot(db, project_id, base)
        target_set, target_name = versions.resolve_snapshot(db, project_id, target)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"base": base_name, "target": target_name, "changes": versions.compare(db, base_set, target_set)}


@router.post("/projects/{project_id}/versions", status_code=202)
def publish_version(project_id: str, body: PublishIn, user: CurrentUser = Depends(members.require_project_lead),
                    db: Session = Depends(get_db)):
    project = service.get_project_or_404(db, project_id)
    try:
        version = versions.start_publish(db, project, user, body.notes)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _version_summary(version)


@router.get("/projects/{project_id}/versions/{number}")
def get_version(project_id: str, number: int, db: Session = Depends(get_db)):
    return _version_summary(_version_or_404(db, project_id, number))


@router.get("/projects/{project_id}/versions/{number}/breakdown")
def get_version_breakdown(project_id: str, number: int, by: str = GROUP_BY, db: Session = Depends(get_db)):
    version = _version_or_404(db, project_id, number)
    if version.status != "published":
        raise HTTPException(status_code=409, detail=f"v{number} is not published")
    return split_breakdown.breakdown(db, project_id, by=by, version=version)


@router.get("/projects/{project_id}/versions/{number}/files/{name}")
def download_version_file(project_id: str, number: int, name: str, db: Session = Depends(get_db)):
    if name not in VERSION_FILES:
        raise HTTPException(status_code=404, detail="No such file")
    project = service.get_project_or_404(db, project_id)
    version = _version_or_404(db, project_id, number)
    path = versions.version_dir(project, version.version) / name
    if version.status != "published" or not path.is_file():
        raise HTTPException(status_code=404, detail="This version's files are not available")
    filename = f"{versions._slug(project.name)}_v{version.version}_{name}"  # noqa: SLF001
    return FileResponse(path, media_type=VERSION_FILES[name], filename=filename)


@router.post("/projects/{project_id}/versions/{number}/images", status_code=202)
def export_version_images(project_id: str, number: int, user: CurrentUser = Depends(members.require_project_lead),
                          db: Session = Depends(get_db)):
    """Write train/val/test class folders of crops for a published version (slow: every crop)."""
    version = _version_or_404(db, project_id, number)
    try:
        versions.start_image_export(db, version)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _version_summary(version)
