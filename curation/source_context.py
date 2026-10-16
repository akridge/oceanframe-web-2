"""
Source context for a patch-only project, copied from the project its patches were cut from.

A Hugging Face patch dataset holds only the 224 px patches, so its import (curation/hub_import.py)
leaves each item patch-only: the patch is its own source image, with no point position, site,
region, island or year. The regional project those patches came from has all of that, and both
name a patch the same way (``{label}_{image stem}_{ordinal}.png``, curation/importer.py), so a
patch-only item takes its source image and point from the regional item with the same label and
file name. Labels, splits, review state and ``crop_uri`` (the patch scored and exported) are left
as they are, and the item's revision is not bumped: nothing a curator edits changes. Whether the newly
linked source images are in their bucket is left to an availability check (curation/availability.py).
"""
from __future__ import annotations

import logging

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from curation.models import CurationItem
from curation.service import in_chunks

logger = logging.getLogger(__name__)

BATCH = 2000
FIELDS = ("source_image_uri", "row_px", "column_px", "region", "island", "site", "survey_year", "annotator")


def link(db: Session, project_id: str, from_project_id: str) -> dict:
    """Give the project's patch-only items the source image and point of their match in *from_project_id*.
    Idempotent and resumable: only items still without a point position are looked at, a batch at a time,
    each batch committed. Returns how many were linked and how many had no match."""
    linked = unmatched = 0
    after = 0
    while True:
        items = db.execute(
            select(CurationItem.id, CurationItem.source_label, CurationItem.file_name, CurationItem.metadata_json)
            .where(CurationItem.project_id == project_id, CurationItem.row_px.is_(None), CurationItem.id > after)
            .order_by(CurationItem.id).limit(BATCH)).all()
        if not items:
            break
        after = items[-1].id
        matches = {
            (row.source_label, row.file_name): row
            for row in db.execute(
                select(CurationItem.source_label, CurationItem.file_name, *(getattr(CurationItem, f) for f in FIELDS))
                .where(CurationItem.project_id == from_project_id,
                       in_chunks(CurationItem.source_label, {item.source_label for item in items}),
                       in_chunks(CurationItem.file_name, [item.file_name for item in items]),
                       CurationItem.row_px.is_not(None)))
        }
        rows = []
        for item in items:
            match = matches.get((item.source_label, item.file_name))
            if match is None:
                unmatched += 1
                continue
            metadata = {**(item.metadata_json or {}), "patch_only": False, "context_from": from_project_id}
            rows.append({"id": item.id, "metadata_json": metadata, **{field: getattr(match, field) for field in FIELDS}})
        if rows:
            db.execute(update(CurationItem), rows)
            linked += len(rows)
        db.commit()
    if linked or unmatched:
        logger.info("Source context for project %s from %s: %d linked, %d without a match",
                    project_id, from_project_id, linked, unmatched)
    return {"linked": linked, "unmatched": unmatched}
