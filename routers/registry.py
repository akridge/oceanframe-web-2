"""
Hugging Face registry API — /api/curation/hub/*

Everyone signed in can read the registry; leads look repositories up,
register them and start what follows from them.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from appdb.engine import get_db
from auth.deps import require_role_from
from auth.service import CurrentUser
from curation import members, registry
from curation.models import CurationClassifier, CurationHubEntry, CurationProject
from curation.schemas import CurationImportResponse, CurationProjectResponse, DatasetMode
from curation.settings import create_role

router = APIRouter(prefix="/curation/hub", tags=["curation-hub"])
# Registering and creating follow CURATION_CREATE_ROLE (curator by default).
require_creator = require_role_from(create_role)

KIND = Field(default=None, pattern="^(dataset|model)$")


class LookupIn(BaseModel):
    ref: str = Field(min_length=3, max_length=500)
    kind: Optional[str] = KIND


class Location(BaseModel):
    mode: str = Field(pattern="^(column|env_root|custom)$")
    root: Optional[str] = Field(default=None, max_length=900)


class RegisterDatasetIn(LookupIn):
    column_map: Optional[dict[str, Optional[str]]] = None
    location: Location = Field(default_factory=lambda: Location(mode="column"))


class RegisterModelIn(LookupIn):
    weight_file: Optional[str] = Field(default=None, max_length=500)
    name: Optional[str] = Field(default=None, max_length=200)
    trusted: bool = False


@router.get("/entries")
def list_entries(kind: Optional[str] = Query(None, pattern="^(dataset|model)$"), db: Session = Depends(get_db)):
    query = select(CurationHubEntry).order_by(CurationHubEntry.created_at.desc())
    if kind:
        query = query.where(CurationHubEntry.kind == kind)
    projects = db.scalars(select(CurationProject)).all()
    return [registry.summary(db, entry, projects) for entry in db.scalars(query)]


@router.post("/lookup", dependencies=[Depends(require_creator)])
def lookup(body: LookupIn, db: Session = Depends(get_db)):
    return registry.lookup(db, body.ref, body.kind)


@router.post("/datasets", status_code=201)
def register_dataset(body: RegisterDatasetIn, user: CurrentUser = Depends(require_creator), db: Session = Depends(get_db)):
    entry, sample = registry.register_dataset(db, user, ref=body.ref, kind=body.kind or "dataset",
                                              column_map=body.column_map, location=body.location.model_dump())
    return {**registry.summary(db, entry), "sample": sample}


@router.post("/models", status_code=201)
def register_model(body: RegisterModelIn, response: Response, user: CurrentUser = Depends(require_creator),
                   db: Session = Depends(get_db)):
    entry = registry.register_model(db, user, ref=body.ref, kind=body.kind or "model",
                                    weight_file=body.weight_file, name=body.name, trusted=body.trusted)
    if entry.status in {"queued", "downloading"}:
        response.status_code = 202  # the weights download in the background
    return registry.summary(db, entry)


@router.post("/entries/{entry_id}/retry", status_code=202)
def retry_entry(entry_id: int, user: CurrentUser = Depends(require_creator), db: Session = Depends(get_db)):
    entry = registry.get_entry_or_404(db, entry_id, "model")
    if entry.status != "failed":
        raise HTTPException(status_code=409, detail="Only a failed registration can be retried")
    return registry.summary(db, registry.requeue_failed(db, user, entry))


class DatasetProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=4000)
    dataset_mode: DatasetMode = DatasetMode.T3
    splits: list[str] = Field(default_factory=list, max_length=10)


@router.post("/datasets/{entry_id}/projects", status_code=202)
def dataset_project(entry_id: int, body: DatasetProjectIn, user: CurrentUser = Depends(require_creator),
                    db: Session = Depends(get_db)):
    entry = registry.get_entry_or_404(db, entry_id, "dataset")
    project, operation = registry.create_project_from_dataset(
        db, user, entry, name=body.name, description=body.description,
        dataset_mode=body.dataset_mode.value, splits=body.splits)
    return {"project": {**CurationProjectResponse.model_validate(project).model_dump(), "my_role": "lead"},
            "operation": CurationImportResponse.model_validate(operation)}


@router.post("/datasets/{entry_id}/projects/{project_id}/imports", status_code=202, response_model=CurationImportResponse)
def resume_dataset_import(entry_id: int, project_id: str, user: CurrentUser = Depends(members.require_project_curator),
                          db: Session = Depends(get_db)):
    """Resume a failed, cancelled or interrupted import: the same splits again, landed rows skipped."""
    from curation import service  # noqa: PLC0415

    entry = registry.get_entry_or_404(db, entry_id, "dataset")
    return registry.resume_import(db, user, entry, service.get_project_or_404(db, project_id))


@router.get("/datasets/{entry_id}/labels")
def dataset_labels(entry_id: int, dataset_mode: DatasetMode = DatasetMode.T3, classifier_id: Optional[str] = None,
                   db: Session = Depends(get_db)):
    entry = registry.get_entry_or_404(db, entry_id, "dataset")
    names = None
    if classifier_id:
        model = db.get(CurationClassifier, classifier_id)
        names = model.class_names if model is not None and model.status == "ready" else None
    return registry.label_report(entry, dataset_mode.value, names)


class LinkIn(BaseModel):
    project_id: str = Field(min_length=1, max_length=36)
    adopt_splits: bool = False
    dry_run: bool = True


@router.post("/datasets/{entry_id}/links", status_code=202, response_model=CurationImportResponse)
def link_dataset(entry_id: int, body: LinkIn, user: CurrentUser = Depends(require_creator), db: Session = Depends(get_db)):
    from curation import service  # noqa: PLC0415

    # A dry run writes nothing. Applying rewrites splits and provenance on a
    # project others curate, so it needs the project lead role.
    if not body.dry_run and members.project_role(db, body.project_id, user) != "lead":
        raise HTTPException(status_code=403, detail="Applying a link needs the project lead role; a dry run does not")
    entry = registry.get_entry_or_404(db, entry_id, "dataset")
    project = service.get_project_or_404(db, body.project_id)
    return registry.queue_link(db, user, entry, project, adopt_splits=body.adopt_splits, dry_run=body.dry_run)
