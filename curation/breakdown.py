"""
Included items per group and split, for the live draft or a published version.

One GROUP BY per call, so it stays quick on the 941k-item NOAA project. A
published version is read from its frozen ``curation_version_inputs`` rows, so
a past version's breakdown never drifts as items are edited.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from curation import settings
from curation.models import CurationDatasetVersion, CurationItem, CurationVersionInput

GROUPS = ("label", "region", "island", "survey_year", "annotator")
SPLITS = ("train", "val", "test", "unassigned")
NONE_KEY = "(none)"


def breakdown(db: Session, project_id: str, *, by: str,
              version: Optional[CurationDatasetVersion] = None) -> dict:
    if by not in GROUPS:
        raise ValueError(f"Group by one of {', '.join(GROUPS)}")
    if version is None:
        key = CurationItem.current_label if by == "label" else getattr(CurationItem, by)
        split = CurationItem.split
        where = [CurationItem.project_id == project_id, CurationItem.included == True]  # noqa: E712
    else:
        key = getattr(CurationVersionInput, by)
        split = CurationVersionInput.split
        where = [CurationVersionInput.version_id == version.id]
    counted = db.execute(select(key, split, func.count()).where(*where).group_by(key, split)).all()

    by_key: dict[str, dict[str, int]] = {}
    for value, split_name, count in counted:
        counts = by_key.setdefault(NONE_KEY if value is None else str(value), dict.fromkeys(SPLITS, 0))
        counts[split_name if split_name in counts else "unassigned"] += count
    rare_min = settings.rare_min()
    rows = [{"key": name, "counts": counts, "total": sum(counts.values()),
             "rare": counts["val"] < rare_min or counts["test"] < rare_min}
            for name, counts in by_key.items()]
    rows.sort(key=lambda row: (-row["total"], row["key"]))
    totals = {name: sum(row["counts"][name] for row in rows) for name in SPLITS}
    return {"by": by, "source": "live" if version is None else version.version, "splits": list(SPLITS),
            "rows": rows, "totals": totals, "total": sum(totals.values()), "rare_min": rare_min}
