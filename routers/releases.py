"""Releases and project comparison — /api/curation/releases, /api/curation/compare-projects."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from appdb.engine import get_db
from auth.deps import current_user
from auth.service import CurrentUser
from curation import project_compare, releases
from curation.models import CurationRelease

router = APIRouter(prefix="/curation", tags=["curation-releases"])


@router.get("/compare-projects")
def compare_projects(a: str, b: str, a_version: str = Query("live", max_length=12),
                     b_version: str = Query("live", max_length=12), db: Session = Depends(get_db),
                     _me: CurrentUser = Depends(current_user)):
    return project_compare.compare(db, a, a_version, b, b_version)


class SourceIn(BaseModel):
    project_id: str = Field(min_length=1, max_length=36)
    version: int = Field(ge=1)


class ReleaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    dataset_mode: str = Field(pattern="^(T1|T3)$")
    sources: list[SourceIn] = Field(default_factory=list, max_length=50)
    notes: Optional[str] = Field(default=None, max_length=4000)


@router.get("/releases")
def list_releases(db: Session = Depends(get_db), _me: CurrentUser = Depends(current_user)):
    rows = db.scalars(select(CurationRelease).order_by(CurationRelease.id.desc())).all()
    return [releases.as_dict(r) for r in rows]


@router.post("/releases", status_code=202)
def build_release(body: ReleaseIn, me: CurrentUser = Depends(current_user), db: Session = Depends(get_db)):
    release = releases.start(db, me, body.name, body.dataset_mode, [s.model_dump() for s in body.sources], body.notes)
    return releases.as_dict(release)


@router.get("/releases/{release_id}")
def get_release(release_id: int, db: Session = Depends(get_db), _me: CurrentUser = Depends(current_user)):
    release = db.get(CurationRelease, release_id)
    if release is None:
        raise HTTPException(status_code=404, detail="Release not found")
    return releases.as_dict(release)


@router.get("/releases/{release_id}/files/{name}")
def release_file(release_id: int, name: str, db: Session = Depends(get_db), _me: CurrentUser = Depends(current_user)):
    release = db.get(CurationRelease, release_id)
    if release is None or name not in releases.FILES:
        raise HTTPException(status_code=404, detail="No such file")
    path = releases.release_dir(release) / name
    if release.status != "published" or not path.is_file():
        raise HTTPException(status_code=404, detail="This release's files are not available")
    media = "text/csv" if name.endswith(".csv") else "application/json" if name.endswith(".json") else "text/markdown"
    return FileResponse(path, media_type=media, filename=f"{releases._slug(release.name)}_r{release.number}_{name}")  # noqa: SLF001
