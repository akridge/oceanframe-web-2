"""
Background bodies for Hugging Face dataset operations.

They run on the existing curation import worker (curation/importer.py), so
they get its queue, progress, cancel and restart handling. ``import_rows``
creates patch-only items; ``link_rows`` attaches a dataset to items
that already exist.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy.orm import Session

from appdb.base import utcnow
from curation import hub_rows, service, taxonomy
from curation.models import URI_LEN, CurationHubEntry, CurationImportOperation, CurationProject

logger = logging.getLogger(__name__)

# Item splits; a dataset's other splits (e.g. "unlabeled") import unassigned and get one at publish.
SPLITS = ("train", "val", "test")


def _entry(db: Session, source: dict) -> CurationHubEntry:
    entry = db.get(CurationHubEntry, source["entry_id"])
    if entry is None:
        raise ValueError("The registry entry for this import was deleted")
    return entry


def record_for(row: dict, split: str, *, project_id: str, entry: CurationHubEntry, dataset_mode: str,
               names: list[str]) -> dict:
    column_map, location = entry.options["column_map"], entry.options["location"]
    # The dataset's own split name places the patch (root/split/label/file) and keys the row,
    # so a resumed import recognises it; only the item's split is limited to train/val/test.
    split, label, file_name = hub_rows.row_fields(row, split, column_map, names)
    try:
        uri = hub_rows.crop_uri(row, split, label, file_name, column_map, location)
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from exc  # one bad row is rejected, not a crash
    if len(uri) > URI_LEN:
        raise ValueError("Image path too long")
    now = utcnow()
    return {
        "project_id": project_id, "source_key": f"hf:{split}:{label}:{file_name}"[:400],
        "source_image_uri": uri, "crop_uri": uri, "file_name": file_name[:300],
        "source_label": label, "current_label": taxonomy.effective_label(label, dataset_mode),
        "included": True, "review_state": "unreviewed", "row_px": None, "column_px": None, "crop_size": 224,
        "region": None, "island": None, "site": None, "survey_year": None,
        "split": split if split in SPLITS else None, "annotator": None,
        "model_label": None, "model_confidence": None, "top_predictions": [], "quality_flags": [],
        "metadata_json": {
            "split_origin": "hub" if split in SPLITS else "generated", "patch_only": True,
            "source_image_stem": hub_rows.source_image_stem(label, file_name),
            "hub": {"repo_id": entry.repo_id, "revision": entry.revision, "split": split,
                    "row": row.get(hub_rows.ROW_KEY)},
        },
        "revision": 1, "created_at": now, "updated_at": now,
    }


def import_rows(db: Session, operation: CurationImportOperation, source: dict,
                cancel_event: threading.Event, dataset_mode: str) -> None:
    from curation.importer import _insert_batch  # noqa: PLC0415 — importer imports this module

    entry = _entry(db, source)
    column_map = entry.options["column_map"]
    names = hub_rows.class_names(entry.card, column_map["label"])
    batch: list[dict] = []

    def flush() -> None:
        imported, skipped = _insert_batch(db, batch)
        operation.imported_rows += imported
        operation.skipped_rows += skipped
        batch.clear()
        db.add(operation)
        db.commit()

    columns = [name for name in column_map.values() if name]
    try:
        for split, row in hub_rows.iter_rows(entry, source.get("splits") or None, columns):
            if cancel_event.is_set():
                break
            operation.processed_rows += 1
            try:
                batch.append(record_for(row, split, project_id=operation.project_id, entry=entry,
                                        dataset_mode=dataset_mode, names=names))
            except (TypeError, ValueError):
                operation.rejected_rows += 1
            if len(batch) >= operation.batch_size:
                flush()
    except Exception:
        # A download or read failed: keep the rows read so far, then let the operation fail.
        try:
            flush()
        except Exception:  # noqa: BLE001 — must not mask the original error
            logger.exception("Could not save the rows read before the failure")
            db.rollback()
        raise
    flush()


@dataclass
class Actor:
    """Who a background change is attributed to: the lead who started it."""
    id: int | None
    label: str


def link_rows(db: Session, operation: CurationImportOperation, source: dict,
              cancel_event: threading.Event) -> None:
    entry = _entry(db, source)
    column_map = entry.options["column_map"]
    names = hub_rows.class_names(entry.card, column_map["label"])
    adopt, dry_run = bool(source.get("adopt_splits")), bool(source.get("dry_run"))
    actor = Actor(operation.created_by_user_id, operation.created_by_name or "import")
    hub = {"repo_id": entry.repo_id, "revision": entry.revision}
    result = {"matched": 0, "unmatched": 0, "ambiguous": 0, "split_changes": 0,
              "kept_csv_split": 0, "already_linked": 0}
    pending: list[tuple[str, str, str]] = []  # (split, label, file name)
    seen: set[str] = set()

    def flush() -> None:
        matches = service.match_items(db, operation.project_id, [(label, name) for _, label, name in pending])
        for split, label, file_name in pending:
            candidates = matches.get((label, file_name), [])
            if not candidates:
                result["unmatched"] += 1
                continue
            if len(candidates) > 1:
                result["ambiguous"] += 1
                continue
            item = candidates[0]
            result["matched"] += 1
            if item.id in seen:  # the same crop listed twice: change it once
                continue
            seen.add(item.id)
            metadata = dict(item.metadata_json or {})
            from_csv = metadata.get("split_origin") == "imported"
            # Only train/val/test are adopted; another split name is recorded on the link alone.
            differs = adopt and split in SPLITS and item.split != split
            change_split = differs and not from_csv
            if differs and from_csv:
                result["kept_csv_split"] += 1
            if change_split:
                result["split_changes"] += 1
            link = {**hub, "split": split}
            if metadata.get("hub") == link and not change_split:
                result["already_linked"] += 1
                continue
            if dry_run:
                continue
            previous = {**service._state(item), "split": item.split}  # noqa: SLF001 — same package
            metadata["hub"] = link
            if change_split:
                item.split = split
                metadata["split_origin"] = "hub"
            item.metadata_json = metadata
            service._record_change(  # noqa: SLF001
                db, item, actor, "split_adopted" if change_split else "hub_link", previous,
                operation_id=operation.id, extra_new={"split": item.split, "hub": link})
        operation.imported_rows = result["matched"]
        operation.skipped_rows = result["unmatched"]
        operation.rejected_rows = result["ambiguous"]
        pending.clear()
        db.add(operation)
        db.commit()

    def record(partial: bool) -> None:
        operation.sources = [{**source, "result": result}]
        if dry_run or not (result["matched"] or result["split_changes"]):
            return
        project = db.get(CurationProject, operation.project_id)
        config = dict(project.source_config or {})
        links = [link for link in config.get("hub_links", []) if link.get("entry_id") != entry.id]
        links.append({"entry_id": entry.id, **hub, "linked_at": utcnow().isoformat() + "Z",
                      "adopted_splits": adopt, **({"partial": True} if partial else {})})
        config["hub_links"] = links
        project.source_config = config

    try:
        for split, row in hub_rows.iter_rows(entry, None, [name for name in column_map.values() if name]):
            if cancel_event.is_set():
                break
            operation.processed_rows += 1
            try:
                pending.append(hub_rows.row_fields(row, split, column_map, names))
            except ValueError:
                continue
            if len(pending) >= operation.batch_size:
                flush()
    except Exception:
        # A download or read failed: keep the changes made so far, then let the operation fail.
        try:
            flush()
            record(True)
            db.commit()
        except Exception:  # noqa: BLE001 — must not mask the original error
            logger.exception("Could not save the changes made before the failure")
            db.rollback()
        raise
    flush()
    record(cancel_event.is_set())
    db.commit()
