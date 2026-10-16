"""
Serving curation images to the page.

The proxy only fetches from buckets this deployment curates — the configured
import sources, the evaluation split bucket, anything named in
``CURATION_IMAGE_BUCKETS`` — so it cannot be used to read arbitrary objects with
the server's credentials.
"""
from __future__ import annotations

import io
import mimetypes
from typing import Iterable, Optional

import piexif
from PIL import Image
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from curation import evaluation, image_cache, settings, storage
from curation.models import CurationImportOperation, CurationItem, CurationProject

ORIENTATION_TAG = piexif.ImageIFD.Orientation


def _bucket(uri: str) -> Optional[str]:
    if not uri.startswith("gs://"):
        return None
    return uri[5:].split("/", 1)[0] or None


def allowed_buckets(db: Session) -> set[str]:
    buckets = set(settings.extra_image_buckets())
    for source in settings.import_sources():
        for key in ("root_uri", "csv_uri"):
            bucket = _bucket(str(source.get(key) or ""))
            if bucket:
                buckets.add(bucket)
    buckets.add(evaluation.split_location()[0])
    for uri in (settings.t1_crop_root(), settings.t3_all_root()):
        bucket = _bucket(uri)
        if bucket:
            buckets.add(bucket)
    # Buckets of any source somebody has actually imported from.
    for sources in db.scalars(select(CurationImportOperation.sources)):
        for source in sources or []:
            bucket = _bucket(str(source.get("root_uri") or ""))
            if bucket:
                buckets.add(bucket)
    return buckets


def is_allowed_in(buckets: set[str], uri: str) -> bool:
    """is_allowed against a bucket set fetched once, for loops and worker threads."""
    bucket = _bucket(uri)
    if bucket is None:
        return settings.allow_local_sources() and bool(uri.strip())
    return bucket in buckets


def is_allowed(db: Session, uri: str) -> bool:
    """May the server read this URI on a user's behalf?

    Only gs:// objects in a curated bucket. Never "whatever an item points at":
    curators create items, so that would let any curator read any object the
    service account can see — or, with a local path, the server's own files
    (its environment holds the database password).
    """
    return is_allowed_in(allowed_buckets(db), uri)


def require_allowed(db: Session, *uris: str) -> None:
    for uri in uris:
        if not is_allowed(db, uri):
            raise HTTPException(
                status_code=422,
                detail=f"{uri!r} is not in a bucket this deployment curates "
                       "(add it to CURATION_IMAGE_BUCKETS, or import from it)",
            )


def media_type(uri: str) -> str:
    return mimetypes.guess_type(uri)[0] or "application/octet-stream"


def remove_exif_orientation(payload: bytes) -> bytes:
    """Drop the EXIF orientation flag without re-encoding the pixels.

    Annotation coordinates are in the sensor's raw pixel grid. If the browser
    honoured an orientation flag it would rotate the photo under the points, so
    the source view asks for the raw orientation.
    """
    try:
        exif = piexif.load(payload)
    except Exception:  # noqa: BLE001 — not a JPEG/TIFF with EXIF
        return payload
    if ORIENTATION_TAG not in exif.get("0th", {}):
        return payload
    del exif["0th"][ORIENTATION_TAG]
    exif.pop("thumbnail", None)
    exif.get("1st", {}).clear()
    output = io.BytesIO()
    try:
        piexif.insert(piexif.dump(exif), payload, output)
    except Exception:  # noqa: BLE001 — EXIF we cannot rewrite: drop it all instead
        output = io.BytesIO()
        piexif.remove(payload, output)
    return output.getvalue()


def needs_fresh_crop(item) -> bool:
    """Edited or added points have no stored crop that matches them: cut from the source."""
    metadata = getattr(item, "metadata_json", None) or {}
    return bool(metadata.get("geometry_edited") or metadata.get("manual_point"))


def crop_from_source(item: CurationItem) -> bytes:
    """Rebuild a missing crop by cutting it out of the source image."""
    return image_cache.patch(item.source_image_uri, item.row_px, item.column_px, item.crop_size)


def crop_candidates(item: CurationItem, project: CurationProject) -> Iterable[str]:
    """Where an item's crop might live, best first."""
    yield item.crop_uri
    if project.dataset_mode != "T3":
        return
    yield f"{settings.t3_all_root()}{item.source_label}/{item.file_name}"
    bucket, prefix = evaluation.split_location()
    order = [item.split] if item.split in {"test", "train", "val"} else []
    order.extend(split for split in ("test", "train", "val") if split not in order)
    for split in order:
        yield f"gs://{bucket}/{prefix}/{split}/{item.source_label}/{item.file_name}"
