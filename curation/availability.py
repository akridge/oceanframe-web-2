"""
Which source images are missing from their bucket.

One listing per images/ prefix the project uses (about a minute for 93k images),
then a column flag per item. That makes the source queue able to hide missing
images and sort them last, and lets Dataset status say which surveys are gone.
"""
from __future__ import annotations

import csv
import io
import threading
from collections import defaultdict
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from curation import storage
from curation.models import CurationImportOperation, CurationItem
from curation.service import in_chunks

SOURCE = {"kind": "availability_check", "name": "Check source images"}


def is_check(operation: CurationImportOperation) -> bool:
    """Is this operation an availability check (every source of it), not an import?"""
    sources = operation.sources or []
    return bool(sources) and all(source.get("kind") == SOURCE["kind"] for source in sources)


def queue_check(db: Session, project_id: str, user=None) -> CurationImportOperation:
    """Queue a background availability check, the way an import operation is queued.

    A check for this project that is still queued or running is returned instead of a second one."""
    active = db.scalars(select(CurationImportOperation).where(
        CurationImportOperation.project_id == project_id,
        CurationImportOperation.status.in_(("queued", "starting", "running")),
    ).order_by(CurationImportOperation.id)).all()
    for existing in active:
        if is_check(existing):
            return existing
    operation = CurationImportOperation(
        project_id=project_id, sources=[dict(SOURCE)], batch_size=1000,
        created_by_user_id=getattr(user, "id", None),
        created_by_name=getattr(user, "label", None) or "availability check",
    )
    db.add(operation)
    db.commit()
    db.refresh(operation)
    return operation


def _prefix(uri: str) -> str:
    return uri.rsplit("/", 1)[0] + "/"


def check(db: Session, project_id: str, cancel: Optional[threading.Event] = None) -> dict:
    uris = [u for u in db.scalars(select(CurationItem.source_image_uri).where(
        CurationItem.project_id == project_id).distinct())]
    by_prefix: dict[str, list[str]] = defaultdict(list)
    for uri in uris:
        by_prefix[_prefix(uri)].append(uri)
    missing: list[str] = []
    for prefix, members in by_prefix.items():
        if cancel is not None and cancel.is_set():
            raise InterruptedError("Availability check canceled")
        try:
            present = storage.list_names(prefix)
        except PermissionError:
            continue  # local paths not allowed here: leave those images unflagged
        missing.extend(uri for uri in members if uri.rsplit("/", 1)[-1] not in present)
    db.execute(update(CurationItem).where(CurationItem.project_id == project_id, CurationItem.source_missing == True)  # noqa: E712
               .values(source_missing=False).execution_options(synchronize_session=False))
    for start in range(0, len(missing), 1000):
        db.execute(update(CurationItem).where(CurationItem.project_id == project_id,
                                              in_chunks(CurationItem.source_image_uri, missing[start:start + 1000]))
                   .values(source_missing=True).execution_options(synchronize_session=False))
    db.commit()
    return {"checked": len(uris), "missing": len(missing), "prefixes": len(by_prefix)}


def summary(db: Session, project_id: str) -> dict:
    where = [CurationItem.project_id == project_id, CurationItem.source_missing == True]  # noqa: E712
    images = db.scalar(select(func.count(func.distinct(CurationItem.source_image_uri))).where(*where)) or 0
    items = db.scalar(select(func.count()).select_from(CurationItem).where(*where)) or 0
    rows = db.execute(select(CurationItem.region, CurationItem.survey_year,
                             func.count(func.distinct(CurationItem.source_image_uri)))
                      .where(*where).group_by(CurationItem.region, CurationItem.survey_year)).all()
    return {"missing_images": images, "missing_items": items,
            "by_region_year": [{"region": r, "year": y, "images": n} for r, y, n in
                               sorted(rows, key=lambda row: (-row[2], str(row[0]), row[1] or 0))]}


def missing_csv(db: Session, project_id: str) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["source_image_uri", "region", "survey_year", "points"])
    for uri, region, year, count in db.execute(
            select(CurationItem.source_image_uri, CurationItem.region, CurationItem.survey_year, func.count())
            .where(CurationItem.project_id == project_id, CurationItem.source_missing == True)  # noqa: E712
            .group_by(CurationItem.source_image_uri, CurationItem.region, CurationItem.survey_year)
            .order_by(CurationItem.region, CurationItem.survey_year, CurationItem.source_image_uri)):
        writer.writerow([uri, region, year, count])
    return output.getvalue()
