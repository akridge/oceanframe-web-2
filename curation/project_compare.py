"""
Comparing two projects — each live (included items) or a published version —
matched by crop (file_name, source_label), the rule that matched every HF row.

Counts come from EXISTS, not from the join's row product, so a crop held twice
on one side cannot inflate them. Splits are grouped raw and mapped in Python:
a bound default in both SELECT and GROUP BY fails on Oracle.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

from curation.models import CurationDatasetVersion, CurationItem, CurationProject, CurationVersionInput

MAX_IDS = 5000


def _side(db: Session, project_id: str, ref: str):
    project = db.get(CurationProject, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Curation project not found")
    if ref == "live":
        rows = (select(CurationItem.id.label("item_id"), CurationItem.file_name, CurationItem.source_label,
                       CurationItem.current_label.label("label"), CurationItem.split)
                .where(CurationItem.project_id == project_id, CurationItem.included == True)  # noqa: E712
                .subquery())
        return project, rows, "live"
    if not (ref.isascii() and ref.isdigit()):   # "²".isdigit() is True, but int("²") raises
        raise HTTPException(status_code=422, detail=f"'{ref}' is neither 'live' nor a version number")
    version = db.scalar(select(CurationDatasetVersion).where(
        CurationDatasetVersion.project_id == project_id, CurationDatasetVersion.version == int(ref)))
    if version is None:
        raise HTTPException(status_code=404, detail=f"No version v{ref} of {project.name}")
    if version.status != "published":
        raise HTTPException(status_code=422, detail=f"v{ref} of {project.name} is not published")
    has_inputs = db.scalar(select(CurationVersionInput.item_id)
                           .where(CurationVersionInput.version_id == version.id).limit(1)) is not None
    if version.item_count and not has_inputs:
        raise HTTPException(status_code=422, detail=f"v{version.version} of {project.name} was published before "
                                                    "releases were supported — publish a new version first")
    rows = (select(CurationVersionInput.item_id, CurationVersionInput.file_name, CurationVersionInput.source_label,
                   CurationVersionInput.label, CurationVersionInput.split)
            .where(CurationVersionInput.version_id == version.id).subquery())
    return project, rows, f"v{ref}"


def compare(db: Session, a_id: str, a_ref: str, b_id: str, b_ref: str) -> dict:
    if a_id == b_id and a_ref == b_ref:
        raise HTTPException(status_code=422, detail="Choose two different sides to compare")
    project_a, a, a_name = _side(db, a_id, a_ref)
    project_b, b, b_name = _side(db, b_id, b_ref)
    if project_a.dataset_mode != project_b.dataset_mode:
        raise HTTPException(status_code=422, detail="Both projects must use the same dataset mode (T1 or T3)")
    same = and_(a.c.file_name == b.c.file_name, a.c.source_label == b.c.source_label)

    a_count = db.scalar(select(func.count()).select_from(a)) or 0
    b_count = db.scalar(select(func.count()).select_from(b)) or 0
    matched_a = db.scalar(select(func.count()).select_from(a).where(exists().where(same))) or 0
    matched_b = db.scalar(select(func.count()).select_from(b).where(exists().where(same))) or 0

    per_a = dict(db.execute(select(a.c.label, func.count()).group_by(a.c.label)).all())
    per_b = dict(db.execute(select(b.c.label, func.count()).group_by(b.c.label)).all())
    classes = sorted(({"label": label, "a": per_a.get(label, 0), "b": per_b.get(label, 0),
                       "diff": per_b.get(label, 0) - per_a.get(label, 0)} for label in set(per_a) | set(per_b)),
                     key=lambda row: (-abs(row["diff"]), row["label"]))

    joined = a.join(b, same)
    label_pairs = db.execute(select(a.c.label, b.c.label, func.count()).select_from(joined)
                             .where(a.c.label != b.c.label).group_by(a.c.label, b.c.label)
                             .order_by(func.count().desc())).all()
    a_item_ids = [i for i in db.scalars(select(a.c.item_id).select_from(joined).where(a.c.label != b.c.label)
                                        .distinct().order_by(a.c.item_id).limit(MAX_IDS))]
    split_rows = db.execute(select(a.c.split, b.c.split, func.count()).select_from(joined)
                            .group_by(a.c.split, b.c.split)).all()
    split_pairs: dict[tuple[str, str], int] = {}
    for a_split, b_split, count in split_rows:
        pair = (a_split or "unassigned", b_split or "unassigned")
        if pair[0] != pair[1]:
            split_pairs[pair] = split_pairs.get(pair, 0) + count

    return {
        "a": {"project_id": project_a.id, "project": project_a.name, "ref": a_name, "count": a_count},
        "b": {"project_id": project_b.id, "project": project_b.name, "ref": b_name, "count": b_count},
        "matched": matched_a, "only_a": a_count - matched_a, "only_b": b_count - matched_b,
        "classes": classes,
        "label_disagreements": {"count": sum(c for _, _, c in label_pairs),
                                "pairs": [{"a_label": x, "b_label": y, "count": c} for x, y, c in label_pairs[:50]],
                                "a_item_ids": a_item_ids},
        "split_disagreements": {"count": sum(split_pairs.values()),
                                "pairs": [{"a_split": x, "b_split": y, "count": c}
                                          for (x, y), c in sorted(split_pairs.items(), key=lambda kv: -kv[1])]},
    }
