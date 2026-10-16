"""
Background import of CoralNet regional annotation CSVs into a project.

One worker thread claims queued operations from the database, so an import
survives page reloads and is visible to everyone. Re-importing the same CSV is
safe: each row's ``source_key`` (source, image, ordinal, row, column) is unique
per project, and rows already present are counted as skipped.

OASIS inserted with SQLite's ``INSERT … ON CONFLICT DO NOTHING``. Here each
batch first asks which of its keys already exist and inserts the rest — the
same result on Oracle and SQLite — with the unique constraint as a backstop.
Hugging Face datasets ride the same worker (`kind: hub_dataset | hub_link`, see
`curation/hub_import.py`).
"""
from __future__ import annotations

import csv
import logging
import threading
from pathlib import PurePosixPath
from typing import Dict, Iterable, Iterator, Mapping, Optional

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from appdb.base import blank_to_none, utcnow
from appdb.engine import new_session, session_scope
from curation import settings, storage, taxonomy
from curation.models import (
    LABEL_LEN, NAME_LEN, PLACE_LEN, URI_LEN,
    CurationEvent, CurationImportOperation, CurationItem, CurationProject,
)
from curation.service import in_chunks, mark_interrupted_imports
from curation.splits import normalize_split, split_for

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = {"Name", "Date", "Region", "Island", "Site", "Row", "Column", "Label code"}


def _clean_label(value: object) -> str:
    return str(value or "").strip().lstrip("*").strip()


def _integer(value: object) -> int:
    return int(float(str(value or "").strip()))


def _year(value: object) -> Optional[int]:
    text = str(value or "").strip()
    return int(text[:4]) if len(text) >= 4 and text[:4].isdigit() else None


def _text(value: object, limit: int) -> Optional[str]:
    text = blank_to_none(str(value or ""))
    if text is not None and len(text) > limit:
        raise ValueError(f"Value longer than {limit} characters")
    return text


def _predictions(row: Mapping[str, object]) -> list[dict]:
    values = []
    for rank in range(1, 6):
        label = _clean_label(row.get(f"Machine suggestion {rank}"))
        if not label:
            continue
        confidence_text = str(row.get(f"Machine confidence {rank}") or "").strip()
        confidence = None
        if confidence_text:
            confidence = max(0.0, min(1.0, float(confidence_text) / 100.0))
        values.append({"label": label, "confidence": confidence, "rank": rank})
    return values


def iter_source_records(
    rows: Iterable[Mapping[str, object]], *, project_id: str, source_name: str, root_uri: str,
    dataset_mode: str = "T3", image_ordinals: Optional[Dict[str, int]] = None,
) -> Iterator[dict]:
    """Normalise regional CSV rows into ``curation_items`` rows."""
    root_uri = root_uri.rstrip("/") + "/"
    image_ordinals = image_ordinals if image_ordinals is not None else {}
    t1 = dataset_mode.upper() == "T1"
    for row in rows:
        image_name = str(row.get("Name") or "").strip()
        label = _clean_label(row.get("Label code"))
        if not image_name or not label:
            raise ValueError("Name and Label code are required")
        if len(label) > LABEL_LEN:
            raise ValueError("Label code too long")
        row_px = _integer(row.get("Row"))
        column_px = _integer(row.get("Column"))
        ordinal = image_ordinals.get(image_name, 0) + 1
        image_ordinals[image_name] = ordinal
        crop_name = f"{label}_{PurePosixPath(image_name).stem}_{ordinal}.png"
        predictions = _predictions(row)
        current_label = taxonomy.effective_label(label, dataset_mode)
        crop_uri = f"{settings.t1_crop_root()}{current_label}/{crop_name}" if t1 \
            else f"{root_uri}crops/{label}/{crop_name}"
        source_image_uri = f"{root_uri}images/{image_name}"
        if len(source_image_uri) > URI_LEN or len(crop_uri) > URI_LEN:
            raise ValueError("Image path too long")
        island = _text(row.get("Island"), PLACE_LEN)
        annotator = _text(row.get("Annotator"), NAME_LEN)
        original_split = normalize_split(row.get("Split", row.get("split")))
        yield {
            "project_id": project_id,
            "source_key": f"{source_name}:{image_name}:{ordinal}:{row_px}:{column_px}"[:400],
            "source_image_uri": source_image_uri,
            "crop_uri": crop_uri,
            "file_name": crop_name[:300],
            "source_label": label,
            "current_label": current_label,
            "included": True,
            "review_state": "unreviewed",
            "row_px": row_px,
            "column_px": column_px,
            "crop_size": 224,
            "region": _text(row.get("Region"), PLACE_LEN) or source_name[:PLACE_LEN],
            "island": island,
            "site": _text(row.get("Site"), PLACE_LEN),
            "survey_year": _year(row.get("Date")),
            "split": original_split or split_for(source_image_uri, settings.split_seed(), settings.split_ratios()),
            "annotator": annotator,
            "model_label": predictions[0]["label"] if predictions else None,
            "model_confidence": predictions[0]["confidence"] if predictions else None,
            "top_predictions": predictions,
            "quality_flags": [],
            "metadata_json": {
                "split_origin": "imported" if original_split else "generated",
                "source": source_name,
                "date": str(row.get("Date") or ""),
                "island": island or "",
                "label_id": str(row.get("Label ID") or ""),
                "annotator": annotator or "",
                "date_annotated": str(row.get("Date annotated") or ""),
                "latitude": str(row.get("Latitude") or ""),
                "longitude": str(row.get("Longitude") or ""),
            },
            "revision": 1,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        }


def _insert_batch(db: Session, records: list[dict]) -> tuple[int, int]:
    """Insert the records whose source_key is new; return (imported, skipped)."""
    if not records:
        return 0, 0
    project_id = records[0]["project_id"]
    keys = [record["source_key"] for record in records]
    existing = set(db.scalars(select(CurationItem.source_key).where(
        CurationItem.project_id == project_id, in_chunks(CurationItem.source_key, keys),
    )))
    fresh = [record for record in records if record["source_key"] not in existing]
    if fresh:
        try:
            db.execute(insert(CurationItem), fresh)
            db.commit()
        except IntegrityError:
            # Someone inserted the same keys between our check and our insert;
            # fall back to one row at a time and count what actually landed.
            db.rollback()
            landed = 0
            for record in fresh:
                try:
                    db.execute(insert(CurationItem), [record])
                    db.commit()
                    landed += 1
                except IntegrityError:
                    db.rollback()
            return landed, len(records) - landed
    return len(fresh), len(records) - len(fresh)


def import_csv_stream(db: Session, operation: CurationImportOperation, source: Mapping[str, str], stream,
                      cancel_event: threading.Event, dataset_mode: str = "T3") -> None:
    reader = csv.DictReader(stream)
    missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
    if missing:
        raise ValueError(f"Missing required CSV columns: {', '.join(sorted(missing))}")

    batch: list[dict] = []
    image_ordinals: Dict[str, int] = {}
    for raw_row in reader:
        if cancel_event.is_set():
            break
        operation.processed_rows += 1
        try:
            batch.append(next(iter_source_records(
                [raw_row], project_id=operation.project_id, source_name=source["name"],
                root_uri=source["root_uri"], dataset_mode=dataset_mode, image_ordinals=image_ordinals,
            )))
        except (TypeError, ValueError):
            operation.rejected_rows += 1
        if len(batch) >= operation.batch_size:
            imported, skipped = _insert_batch(db, batch)
            operation.imported_rows += imported
            operation.skipped_rows += skipped
            batch.clear()
            db.add(operation)
            db.commit()

    imported, skipped = _insert_batch(db, batch)
    operation.imported_rows += imported
    operation.skipped_rows += skipped
    db.add(operation)
    db.commit()


def run_import(operation_id: str, cancel_event: threading.Event) -> None:
    db = new_session()
    try:
        operation = db.get(CurationImportOperation, operation_id)
        if operation is None:
            return
        operation.status = "running"
        operation.started_at = utcnow()
        db.commit()
        project = db.get(CurationProject, operation.project_id)
        dataset_mode = project.dataset_mode if project is not None else "T3"
        for source in operation.sources:
            if cancel_event.is_set():
                break
            operation.current_source = source["name"][:NAME_LEN]
            db.commit()
            kind = source.get("kind", "regional_csv")
            if kind == "availability_check":
                from curation import availability  # noqa: PLC0415

                try:
                    result = availability.check(db, operation.project_id, cancel_event)
                except InterruptedError:
                    break
                operation.processed_rows = result["checked"]
                operation.rejected_rows = result["missing"]
                db.commit()
                continue
            if kind in {"hub_dataset", "hub_link"}:
                from curation import hub_import  # noqa: PLC0415 — hub_import imports this module

                if kind == "hub_dataset":
                    hub_import.import_rows(db, operation, source, cancel_event, dataset_mode)
                else:
                    hub_import.link_rows(db, operation, source, cancel_event)
                continue
            with storage.open_text(source["csv_uri"]) as stream:
                import_csv_stream(db, operation, source, stream, cancel_event, dataset_mode)

        # Stopped by a shutdown (the pool marks its events) is not a curator's cancel: it resumes on the next start.
        shutdown = cancel_event.is_set() and getattr(cancel_event, "shutdown", False)
        operation.status = "interrupted" if shutdown else "canceled" if cancel_event.is_set() else "completed"
        if shutdown:
            operation.error_message = "The server stopped while this import was running. Rows already imported are kept."
        operation.completed_at = utcnow()
        operation.current_source = None
        # A preview, or a check that imports nothing, leaves no trace on the project.
        dry_run = all((s.get("kind") == "hub_link" and s.get("dry_run")) or s.get("kind") == "availability_check"
                      for s in operation.sources)
        project = db.get(CurationProject, operation.project_id)
        if project is not None and not dry_run:  # a preview leaves no trace on the project
            project.item_count = db.scalar(
                select(func.count()).select_from(CurationItem).where(CurationItem.project_id == project.id)
            ) or 0
            db.add(CurationEvent(
                project_id=project.id, operation_id=operation.id, event_type="source_import_completed",
                user_id=operation.created_by_user_id,
                curator_name=operation.created_by_name or project.curator_name or "import",
                new_value={"processed": operation.processed_rows, "imported": operation.imported_rows,
                           "skipped": operation.skipped_rows, "rejected": operation.rejected_rows},
            ))
        db.commit()
        refresh_statistics(db)
        if operation.status == "completed" and any(
                s.get("kind", "regional_csv") in {"regional_csv", "hub_dataset"} for s in operation.sources):
            try:
                from curation import availability  # noqa: PLC0415

                availability.queue_check(db, operation.project_id)
            except Exception:  # noqa: BLE001 — a follow-up check never fails the import
                logger.exception("Could not queue the availability check after import %s", operation_id)
                db.rollback()
    except Exception as exc:  # noqa: BLE001 — recorded on the operation for the UI to show
        logger.exception("Curation import %s failed", operation_id)
        db.rollback()
        operation = db.get(CurationImportOperation, operation_id)
        if operation is not None:
            operation.status = "failed"
            detail = getattr(exc, "detail", None)  # HTTPException prints without it on some Starlette versions
            operation.error_message = f"{type(exc).__name__}: {detail or exc}"[:4000]
            operation.completed_at = utcnow()
            project = db.get(CurationProject, operation.project_id)
            if project is not None:  # a partial import is an expected outcome
                project.item_count = db.scalar(
                    select(func.count()).select_from(CurationItem).where(CurationItem.project_id == project.id)
                ) or 0
            db.commit()
    finally:
        db.close()


def refresh_statistics(db: Session) -> None:
    """Tell Oracle's optimizer how big the tables are now.

    Measured on the full NOAA import (941,550 points): straight after loading,
    with the tables' statistics still describing them as empty, the unfiltered
    source-image page took 27 s and facets up to 79 s; with fresh statistics
    every page call is under 1.4 s. Oracle's own job would only catch up in
    its nightly window. SQLite plans without this, so it is Oracle-only.
    """
    if db.get_bind().dialect.name != "oracle":
        return
    try:
        for table in ("CURATION_ITEMS", "CURATION_EVENTS"):
            db.execute(text("BEGIN DBMS_STATS.GATHER_TABLE_STATS(ownname => USER, tabname => :t, cascade => TRUE); END;"),
                       {"t": table})
        db.commit()
    except Exception:  # noqa: BLE001 — a performance aid, never a reason to fail an import
        db.rollback()
        logger.exception("Could not refresh optimizer statistics after an import")


class CurationImportPool:
    """One worker thread draining queued imports in order."""

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._cancel_events: Dict[str, threading.Event] = {}
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._worker_loop, name="curation-import", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        self._stop_event.set()
        for event in list(self._cancel_events.values()):
            event.shutdown = True
            event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._thread = None

    def cancel(self, operation_id: str) -> bool:
        event = self._cancel_events.get(operation_id)
        if event:
            event.set()
            return True
        with session_scope() as db:
            changed = db.execute(
                update(CurationImportOperation)
                .where(CurationImportOperation.id == operation_id, CurationImportOperation.status == "queued")
                .values(status="canceled", completed_at=utcnow())
            ).rowcount
        return bool(changed)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                operation_id = self._claim_next()
            except Exception:  # noqa: BLE001 — database briefly unavailable; try again
                logger.exception("Could not poll for curation imports")
                operation_id = None
            if not operation_id:
                self._stop_event.wait(2.0)
                continue
            cancel_event = threading.Event()
            self._cancel_events[operation_id] = cancel_event
            try:
                run_import(operation_id, cancel_event)
            finally:
                self._cancel_events.pop(operation_id, None)

    def _claim_next(self) -> Optional[str]:
        with session_scope() as db:
            operation_id = db.scalar(
                select(CurationImportOperation.id).where(CurationImportOperation.status == "queued")
                .order_by(CurationImportOperation.created_at).limit(1)
            )
            if operation_id is None:
                return None
            # Conditional update: if another worker got there first, rowcount is 0.
            claimed = db.execute(
                update(CurationImportOperation)
                .where(CurationImportOperation.id == operation_id, CurationImportOperation.status == "queued")
                .values(status="starting")
            ).rowcount
        return operation_id if claimed else None


curation_import_pool = CurationImportPool()


SEED_ID = "seed-noaa-pacific"
SEED_PROJECT = "NOAA Pacific T3"


REGIONAL_MARKER = "noaa-t3-regional"


def _starter_owner(db: Session):
    """The first active non-local admin, else the local user (None before either exists)."""
    from auth.models import LOCAL_USERNAME, User  # noqa: PLC0415

    return db.scalar(
        select(User).where(User.role == "admin", User.is_active == True,  # noqa: E712
                           User.username != LOCAL_USERNAME).order_by(User.id).limit(1)
    ) or db.scalar(select(User).where(User.username == LOCAL_USERNAME))


def create_regional_project(db: Session, owner) -> CurationProject:
    """The NOAA Pacific T3 project with its three-region import queued (marked as starter data)."""
    project = CurationProject(
        name=SEED_PROJECT,
        description="NOAA PIFSC CoralNet point annotations for Hawaii, the Mariana Islands and "
                    "Samoa/PRIA, imported automatically on first start.",
        source_config={"dataset_mode": "T3", "baseline_evaluation": "t3-v0-test", "starter": REGIONAL_MARKER},
        curator_name=owner.label if owner else None, owner_user_id=owner.id if owner else None,
    )
    db.add(project)
    db.flush()
    if owner is not None:
        from curation import members  # noqa: PLC0415

        members.ensure_member(db, project.id, owner.id, "lead", "first start")
    queue_regional_import(db, project, owner)
    return project


def queue_regional_import(db: Session, project: CurationProject, owner) -> CurationImportOperation:
    operation = CurationImportOperation(
        project_id=project.id, sources=settings.import_sources(), batch_size=2000,
        created_by_user_id=owner.id if owner else None, created_by_name="first start",
    )
    db.add(operation)
    return operation


def seed_noaa_pacific(db: Session) -> Optional[str]:
    """Create the NOAA Pacific T3 project and queue the three regions, once per database.

    Recorded in the migrations ledger, so deleting the project afterwards does
    not bring it back. Start-up now goes through curation.starter_data, which
    keeps the same project (marked as starter data); this stays for callers
    that seed on demand.
    """
    from appdb.bootstrap import _ledger  # noqa: PLC0415

    if db.execute(select(_ledger.c.migration_id).where(_ledger.c.migration_id == SEED_ID)).first():
        return None
    owner = _starter_owner(db)
    project = db.scalar(select(CurationProject).where(CurationProject.name == SEED_PROJECT))
    if project is None:
        project = create_regional_project(db, owner)
    db.execute(_ledger.insert().values(migration_id=SEED_ID, description=f"Seeded {SEED_PROJECT}",
                                       applied_at=utcnow()))
    return project.id


def on_bootstrap() -> None:
    from curation import releases, versions  # noqa: PLC0415

    if not settings.background_workers():
        # Another process coordinates this database: its running imports and
        # builds are alive, and seeding is its job.
        logger.info("Background workers are off: skipping curation recovery")
        return
    with session_scope() as db:
        count = mark_interrupted_imports(db) + versions.mark_interrupted(db) + releases.mark_interrupted(db)
    if count:
        logger.warning("Marked %d interrupted curation import(s), version build(s) or release build(s)", count)
