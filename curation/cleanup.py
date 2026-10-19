"""
Clean-up from the confusion matrix: act on a whole cell of a saved evaluation at once.

A cell is the patches of one class (actual) the model called another (predicted). The curator
previews it (how many patches per split, how many are already cleaned, and an event watermark),
then applies one action to all of them:

  relabel   the label becomes the predicted class
  exclude   the patch leaves the draft (included = False)
  flag      review state "flagged", marked for a later re-split (metadata ``resplit``)

Apply leaves alone a patch changed after the preview's watermark (reported back as a conflict, as a
stale bulk edit is) and one already cleaned (below) by any action: a patch a curator relabelled to
a third class stays there. So it changes exactly the "patches − cleaned" the preview showed, less
conflicts. Train and val are the default; a test patch needs ``confirm_test``, like a single
test-informed correction, and is marked test-informed. Patches that no longer match the evaluated
item (source_key changed) are not part of the cell.

"Cleaned" for the tally is state, not history: a patch of the cell whose label is no longer the
actual class, or that is excluded, removed or flagged, no longer counts against the cell.
"""
from __future__ import annotations

import uuid
from typing import Iterable, Optional

from fastapi import HTTPException
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from auth.service import CurrentUser
from curation import claims, service
from curation.models import CurationEvaluationPrediction, CurationEvaluationRun, CurationEvent, CurationItem
from curation.service import in_chunks

ACTIONS = ("relabel", "exclude", "flag")
DEFAULT_SPLITS = ("train", "val")
CHUNK = 500
REASONS = {
    "relabel": "Matrix clean-up: relabelled {actual} → {predicted} as the model predicted ({run})",
    "exclude": "Matrix clean-up: excluded {actual} patches the model called {predicted} ({run})",
    "flag": "Matrix clean-up: flagged {actual} patches the model called {predicted} for a re-split ({run})",
}


def _cleaned():
    """SQL: this cell's patch no longer counts as it was evaluated."""
    return or_(CurationItem.current_label != CurationEvaluationPrediction.actual_label,
               CurationItem.included == False, CurationItem.removed == True,  # noqa: E712
               CurationItem.review_state == "flagged")


def _cell(run: CurationEvaluationRun, actual: str, predicted: str, splits: Iterable[str]):
    return (select(CurationEvaluationPrediction.id, CurationEvaluationPrediction.split, CurationItem)
            .join(CurationItem, and_(CurationItem.id == CurationEvaluationPrediction.item_id,
                                     CurationItem.source_key == CurationEvaluationPrediction.source_key,
                                     CurationItem.project_id == run.project_id))
            .where(CurationEvaluationPrediction.run_id == run.id,
                   CurationEvaluationPrediction.actual_label == actual,
                   CurationEvaluationPrediction.predicted_label == predicted,
                   CurationEvaluationPrediction.split.in_(list(splits))))


def _check(run: CurationEvaluationRun, actual: str, predicted: str, splits: list[str]) -> None:
    if run.status != "completed":
        raise HTTPException(409, "Only a completed evaluation can be cleaned up from")
    if actual == predicted:
        raise HTTPException(422, "Choose a cell off the diagonal: the model and the label disagree there")
    if not splits or any(split not in ("train", "val", "test") for split in splits):
        raise HTTPException(422, "Splits must be train, val or test")


def _handled(item: CurationItem, actual: str) -> bool:
    """Python twin of _cleaned(): someone already dealt with this patch (any way), so it is left as it is."""
    return item.current_label != actual or not item.included or bool(item.removed) or item.review_state == "flagged"


def preview(db: Session, run: CurationEvaluationRun, actual: str, predicted: str,
            splits: Optional[list[str]] = None) -> dict:
    splits = list(splits or DEFAULT_SPLITS)
    _check(run, actual, predicted, splits)
    rows = db.execute(select(CurationEvaluationPrediction.split, func.count(),
                             func.sum(case((_cleaned(), 1), else_=0)))
                      .join(CurationItem, and_(CurationItem.id == CurationEvaluationPrediction.item_id,
                                               CurationItem.source_key == CurationEvaluationPrediction.source_key))
                      .where(CurationEvaluationPrediction.run_id == run.id,
                             CurationEvaluationPrediction.actual_label == actual,
                             CurationEvaluationPrediction.predicted_label == predicted,
                             CurationEvaluationPrediction.split.in_(splits))
                      .group_by(CurationEvaluationPrediction.split)).all()
    by_split = {split: {"patches": int(count), "cleaned": int(cleaned or 0)} for split, count, cleaned in rows}
    return {"actual": actual, "predicted": predicted, "splits": splits, "by_split": by_split,
            "patches": sum(entry["patches"] for entry in by_split.values()),
            "cleaned": sum(entry["cleaned"] for entry in by_split.values()),
            "watermark": claims.watermark(db, run.project_id), "needs_confirm_test": "test" in splits}


def apply(db: Session, run: CurationEvaluationRun, *, actual: str, predicted: str, action: str, splits: list[str],
          watermark: int, user: CurrentUser, confirm_test: bool = False, reason: Optional[str] = None) -> dict:
    _check(run, actual, predicted, splits)
    if action not in ACTIONS:
        raise HTTPException(422, f"Action must be one of {', '.join(ACTIONS)}")
    if "test" in splits and not confirm_test:
        raise HTTPException(409, "Confirm that this test-informed clean-up changes only a future dataset version")
    project = service.get_project_or_404(db, run.project_id)
    label = service._clean_label(project, predicted) if action == "relabel" else None  # noqa: SLF001
    reason = reason or REASONS[action].format(actual=actual, predicted=predicted, run=run.id[:8])
    operation_id = str(uuid.uuid4())
    applied, already, conflicts = 0, 0, []
    after = 0
    while True:
        rows = db.execute(_cell(run, actual, predicted, splits).where(CurationEvaluationPrediction.id > after)
                          .order_by(CurationEvaluationPrediction.id).limit(CHUNK)).all()
        if not rows:
            break
        after = rows[-1][0]
        ids = [item.id for _, _, item in rows]
        changed = set(db.scalars(select(CurationEvent.item_id).where(
            CurationEvent.project_id == run.project_id, CurationEvent.id > watermark,
            in_chunks(CurationEvent.item_id, ids))).all())
        for _, split, item in rows:
            if item.id in changed:
                conflicts.append({"id": item.id, "revision": item.revision, "updated_by_name": item.updated_by_name})
                continue
            # Already relabelled (to any class), excluded, removed or flagged: a curator's decision stands.
            if _handled(item, actual):
                already += 1
                continue
            previous = service._state(item)  # noqa: SLF001
            if action == "relabel":
                item.current_label = label
                item.review_state = "reviewed"
            elif action == "exclude":
                item.included = False
                item.review_state = "reviewed"
            else:
                item.review_state = "flagged"
                item.metadata_json = {**(item.metadata_json or {}), "resplit": True}
            if split == "test":  # every test action, flag too (_record_change marks only label and inclusion)
                item.metadata_json = {**(item.metadata_json or {}), "test_informed": True}
            service._record_change(db, item, user, "bulk_item_updated", previous, reason=reason,  # noqa: SLF001
                                   operation_id=operation_id, extra_new={"evaluation": {
                                       "run_id": run.id, "version_id": run.version_id, "split": split,
                                       "actual_label": actual, "predicted_label": predicted, "cleanup": action}})
            applied += 1
        db.commit()
    return {"operation_id": operation_id, "applied": applied, "already": already,
            "conflicts": conflicts[:200], "conflict_count": len(conflicts)}


def tally(db: Session, run: CurationEvaluationRun, splits: Optional[list[str]] = None) -> dict:
    """Cleaned N of M per off-diagonal cell and per actual class."""
    splits = list(splits or DEFAULT_SPLITS)
    rows = db.execute(select(CurationEvaluationPrediction.actual_label, CurationEvaluationPrediction.predicted_label,
                             func.count(), func.sum(case((_cleaned(), 1), else_=0)))
                      .join(CurationItem, and_(CurationItem.id == CurationEvaluationPrediction.item_id,
                                               CurationItem.source_key == CurationEvaluationPrediction.source_key))
                      .where(CurationEvaluationPrediction.run_id == run.id,
                             CurationEvaluationPrediction.actual_label != CurationEvaluationPrediction.predicted_label,
                             CurationEvaluationPrediction.split.in_(splits))
                      .group_by(CurationEvaluationPrediction.actual_label,
                                CurationEvaluationPrediction.predicted_label)).all()
    cells, classes = [], {}
    for actual, predicted, count, cleaned in rows:
        cells.append({"actual": actual, "predicted": predicted, "patches": int(count), "cleaned": int(cleaned or 0)})
        entry = classes.setdefault(actual, {"actual": actual, "patches": 0, "cleaned": 0})
        entry["patches"] += int(count)
        entry["cleaned"] += int(cleaned or 0)
    return {"splits": splits, "cells": sorted(cells, key=lambda c: (-c["patches"], c["actual"], c["predicted"])),
            "classes": sorted(classes.values(), key=lambda c: (-c["patches"], c["actual"])),
            "patches": sum(c["patches"] for c in cells), "cleaned": sum(c["cleaned"] for c in cells)}
