"""
Dataset versions: publish what a project's curators have agreed on, and say
exactly what changed since last time.

Publishing a version

1. gives every item that has no split yet a train/val/test split, by hashing
   its *source image* — so all points from one photo share a split (no
   leakage), and an item never changes split once it has one (models trained
   on v3 and v5 are evaluated on the same test set);
2. snapshots the included items — id, label, split — with one INSERT … SELECT,
   which the database runs read-consistently while people keep curating;
3. compares the snapshot with the previous version in SQL: what came in, what
   went out, what was relabelled, per class;
4. credits who changed what since the previous version;
5. writes ``manifest.csv``, ``manifest.json`` and ``CHANGELOG.md``.

The same comparison against the live project is the *preview*: what
publishing right now would put in and take out.

Turning a version into train/val/test image folders is a separate, optional
step (:func:`export_images`), because it moves every crop.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import re
import threading
from collections import defaultdict
from pathlib import Path
from typing import Optional

from sqlalchemy import bindparam, func, insert, literal, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from appdb.base import utcnow
from appdb.engine import new_session, session_scope
from auth.service import CurrentUser
from curation import settings, storage
from curation.splits import split_for
from curation.models import (
    CurationDatasetVersion, CurationEvent, CurationItem, CurationProject, CurationVersionItem, CurationVersionInput,
)

logger = logging.getLogger(__name__)

SPLITS = ("train", "val", "test")
_running: set[int] = set()
_running_lock = threading.Lock()


# ── Splits ────────────────────────────────────────────────────────────────────

def assign_missing_splits(db: Session, project_id: str, seed: int, ratios: tuple[float, float, float]) -> int:
    """Give a split to items that have none. Existing splits are never changed.

    One batched UPDATE per thousand images (executemany), not one round trip
    per image: a full NOAA project has tens of thousands of them.
    """
    uris = db.scalars(
        select(CurationItem.source_image_uri).distinct()
        .where(CurationItem.project_id == project_id, CurationItem.split.is_(None))
    ).all()
    statement = (
        update(CurationItem)
        .where(CurationItem.project_id == bindparam("b_project"),
               CurationItem.source_image_uri == bindparam("b_uri"),
               CurationItem.split.is_(None))
        .values(split=bindparam("b_split"))
        .execution_options(synchronize_session=False)
    )
    for start in range(0, len(uris), 1000):
        db.connection().execute(statement, [
            {"b_project": project_id, "b_uri": uri, "b_split": split_for(uri, seed, ratios)}
            for uri in uris[start:start + 1000]
        ])
        db.commit()
    return len(uris)


# ── Comparing two sets ────────────────────────────────────────────────────────

def _live(project_id: str):
    """What publishing now would contain: included items, label and split."""
    return (
        select(CurationItem.id.label("item_id"), CurationItem.current_label.label("label"),
               func.coalesce(CurationItem.split, "unassigned").label("split"))
        .where(CurationItem.project_id == project_id, CurationItem.included == True)  # noqa: E712
        .subquery()
    )


def _snapshot(version_id: int):
    return (
        select(CurationVersionItem.item_id, CurationVersionItem.label, CurationVersionItem.split)
        .where(CurationVersionItem.version_id == version_id)
        .subquery()
    )


def resolve_snapshot(db: Session, project_id: str, ref: str):
    """'live' or a published version number → (an item_id/label/split subquery, its name)."""
    if ref == "live":
        return _live(project_id), "live"
    try:
        number = int(ref)
    except ValueError:
        raise ValueError(f"'{ref}' is neither 'live' nor a version number") from None
    version = db.scalar(select(CurationDatasetVersion).where(
        CurationDatasetVersion.project_id == project_id, CurationDatasetVersion.version == number))
    if version is None:
        raise LookupError(f"No version v{number}")
    if version.status != "published":
        raise ValueError(f"v{number} is not published ({version.status})")
    return _snapshot(version.id), f"v{number}"


def compare(db: Session, base, target) -> dict:
    """In / out / relabelled / moved between two (item_id, label, split) sets, per class."""
    joined_in = target.outerjoin(base, base.c.item_id == target.c.item_id)
    came_in = dict(db.execute(
        select(target.c.label, func.count()).select_from(joined_in)
        .where(base.c.item_id.is_(None)).group_by(target.c.label)
    ).all())
    joined_out = base.outerjoin(target, target.c.item_id == base.c.item_id)
    went_out = dict(db.execute(
        select(base.c.label, func.count()).select_from(joined_out)
        .where(target.c.item_id.is_(None)).group_by(base.c.label)
    ).all())
    relabels = db.execute(
        select(base.c.label, target.c.label, func.count())
        .select_from(base.join(target, target.c.item_id == base.c.item_id))
        .where(base.c.label != target.c.label)
        .group_by(base.c.label, target.c.label)
        .order_by(func.count().desc())
    ).all()
    moves = db.execute(
        select(base.c.split, target.c.split, target.c.label, func.count())
        .select_from(base.join(target, target.c.item_id == base.c.item_id))
        .where(base.c.split != target.c.split)
        .group_by(base.c.split, target.c.split, target.c.label)
    ).all()
    moved_to: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    move_matrix: dict[tuple[str, str], int] = defaultdict(int)
    for old_split, new_split, label, count in moves:
        moved_to[label][new_split] += count
        move_matrix[(old_split, new_split)] += count
    before = dict(db.execute(select(base.c.label, func.count()).group_by(base.c.label)).all())
    after = dict(db.execute(select(target.c.label, func.count()).group_by(target.c.label)).all())

    relabel_out: dict[str, int] = defaultdict(int)
    relabel_in: dict[str, int] = defaultdict(int)
    for old, new, count in relabels:
        relabel_out[old] += count
        relabel_in[new] += count

    labels = sorted(set(before) | set(after))
    per_class = []
    for label in labels:
        row = {
            "label": label, "before": before.get(label, 0), "in": came_in.get(label, 0),
            "out": went_out.get(label, 0), "relabelled_in": relabel_in.get(label, 0),
            "relabelled_out": relabel_out.get(label, 0), "after": after.get(label, 0),
            "moved": sum(moved_to[label].values()) if label in moved_to else 0,
            "moved_to": dict(moved_to[label]) if label in moved_to else {},
        }
        row["net"] = row["after"] - row["before"]
        if row["net"] or row["in"] or row["out"] or row["relabelled_in"] or row["relabelled_out"] or row["moved"]:
            per_class.append(row)
    per_class.sort(key=lambda r: (-abs(r["net"]), -(r["in"] + r["out"]), r["label"]))
    return {
        "before": sum(before.values()), "after": sum(after.values()),
        "in": sum(came_in.values()), "out": sum(went_out.values()),
        "relabelled": sum(c for _, _, c in relabels),
        "moved": sum(move_matrix.values()),
        "split_moves": [{"from": old, "to": new, "count": count}
                        for (old, new), count in sorted(move_matrix.items(), key=lambda kv: -kv[1])],
        "per_class": per_class,
        "relabels": [{"from": old, "to": new, "count": count} for old, new, count in relabels[:200]],
    }


def _empty():
    return (
        select(CurationVersionItem.item_id, CurationVersionItem.label, CurationVersionItem.split)
        .where(CurationVersionItem.version_id == -1)  # an empty set, portably
        .subquery()
    )


def contributors(db: Session, project_id: str, after_event: int, up_to_event: Optional[int] = None) -> list[dict]:
    """Who changed items since a watermark, how much, and which classes they worked on."""
    where = [CurationEvent.project_id == project_id, CurationEvent.id > after_event,
             CurationEvent.item_id.is_not(None), CurationEvent.event_type != "item_created"]
    if up_to_event is not None:
        where.append(CurationEvent.id <= up_to_event)
    people = db.execute(
        select(CurationEvent.user_id, func.max(CurationEvent.curator_name), func.count(),
               func.count(func.distinct(CurationEvent.item_id)))
        .where(*where).group_by(CurationEvent.user_id).order_by(func.count().desc())
    ).all()
    focus = db.execute(
        select(CurationEvent.user_id, CurationItem.current_label, func.count(func.distinct(CurationItem.id)))
        .join(CurationItem, CurationItem.id == CurationEvent.item_id)
        .where(*where).group_by(CurationEvent.user_id, CurationItem.current_label)
    ).all()
    by_user: dict = defaultdict(list)
    for user_id, label, count in focus:
        by_user[user_id].append((count, label))
    return [{
        "user_id": user_id, "name": name, "changes": changes, "items": items,
        "classes": [{"label": label, "items": count} for count, label in sorted(by_user[user_id], reverse=True)[:5]],
    } for user_id, name, changes, items in people]


def latest_published(db: Session, project_id: str) -> Optional[CurationDatasetVersion]:
    return db.scalar(
        select(CurationDatasetVersion)
        .where(CurationDatasetVersion.project_id == project_id, CurationDatasetVersion.status == "published")
        .order_by(CurationDatasetVersion.version.desc()).limit(1)
    )


def preview(db: Session, project_id: str) -> dict:
    """What publishing now would change, relative to the latest published version."""
    from curation import change_sets  # noqa: PLC0415 - local import avoids a cycle

    base_version = latest_published(db, project_id)
    base = _snapshot(base_version.id) if base_version else _empty()
    watermark = base_version.event_watermark if base_version else 0
    return {
        "base_version": base_version.version if base_version else None,
        "changes": compare(db, base, _live(project_id)),
        "contributors": contributors(db, project_id, watermark),
        "unassigned_split": db.scalar(
            select(func.count()).select_from(CurationItem).where(
                CurationItem.project_id == project_id, CurationItem.included == True,  # noqa: E712
                CurationItem.split.is_(None))
        ) or 0,
        **change_sets.in_window(db, project_id, watermark, None),
    }


# ── Publishing ────────────────────────────────────────────────────────────────

def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "project"


def version_dir(project: CurationProject, version: int) -> Path:
    return settings.versions_dir() / _slug(project.name) / f"v{version}"


def start_publish(db: Session, project: CurationProject, user: CurrentUser, notes: Optional[str]) -> CurationDatasetVersion:
    """Reserve the next version number and build it in the background."""
    if db.scalar(select(CurationDatasetVersion.id).where(
            CurationDatasetVersion.project_id == project.id, CurationDatasetVersion.status == "building").limit(1)):
        raise ValueError("A version of this project is already being published")
    for _ in range(3):
        number = (db.scalar(select(func.max(CurationDatasetVersion.version))
                            .where(CurationDatasetVersion.project_id == project.id)) or 0) + 1
        version = CurationDatasetVersion(
            project_id=project.id, version=number, status="building", notes=(notes or "").strip() or None,
            split_seed=settings.split_seed(), created_by_user_id=user.id, created_by_name=user.label,
        )
        db.add(version)
        try:
            db.commit()
            break
        except IntegrityError:  # someone else took that number at the same moment
            db.rollback()
    else:
        raise ValueError("Could not reserve a version number; try again")
    threading.Thread(target=_build, args=(version.id,), name=f"curation-publish-{version.id}", daemon=True).start()
    return version


def _build(version_id: int) -> None:
    with _running_lock:
        _running.add(version_id)
    db = new_session()
    try:
        version = db.get(CurationDatasetVersion, version_id)
        project = db.get(CurationProject, version.project_id)
        previous = db.scalar(
            select(CurationDatasetVersion)
            .where(CurationDatasetVersion.project_id == project.id, CurationDatasetVersion.status == "published",
                   CurationDatasetVersion.version < version.version)
            .order_by(CurationDatasetVersion.version.desc()).limit(1)
        )

        assign_missing_splits(db, project.id, version.split_seed, settings.split_ratios())

        # The watermark is read before the snapshot: an edit landing in
        # between is credited to the next version, never lost.
        watermark = db.scalar(select(func.max(CurationEvent.id)).where(CurationEvent.project_id == project.id)) or 0
        fields = ["source_key", "source_label", "crop_uri", "source_image_uri", "file_name", "row_px", "column_px",
                  "crop_size", "region", "island", "site", "survey_year", "annotator", "updated_by_name", "metadata_json"]
        db.execute(insert(CurationVersionInput).from_select(
            ["version_id", "item_id", "label", "split", *fields],
            select(literal(version.id), CurationItem.id, CurationItem.current_label,
                   func.coalesce(CurationItem.split, "train"), *(getattr(CurationItem, field) for field in fields))
            .where(CurationItem.project_id == project.id, CurationItem.included == True),
        ))
        db.execute(insert(CurationVersionItem).from_select(
            ["version_id", "item_id", "label", "split"],
            select(CurationVersionInput.version_id, CurationVersionInput.item_id,
                   CurationVersionInput.label, CurationVersionInput.split)
            .where(CurationVersionInput.version_id == version.id),
        ))
        db.commit()

        target = _snapshot(version.id)
        base = _snapshot(previous.id) if previous else _empty()
        version.changes = {**compare(db, base, target), "previous_version": previous.version if previous else None}
        version.contributors = contributors(db, project.id, previous.event_watermark if previous else 0, watermark)
        from curation import change_sets  # noqa: PLC0415
        window = change_sets.in_window(db, project.id, previous.event_watermark if previous else 0, watermark)
        version.changes = {**version.changes, **window}
        by_class_split = db.execute(
            select(CurationVersionItem.label, CurationVersionItem.split, func.count())
            .where(CurationVersionItem.version_id == version.id)
            .group_by(CurationVersionItem.label, CurationVersionItem.split)
        ).all()
        classes: dict[str, dict[str, int]] = defaultdict(lambda: {s: 0 for s in SPLITS})
        for label, split, count in by_class_split:
            if split not in SPLITS:
                raise ValueError(f"Invalid original split: {split!r}")
            classes[label][split] = count
        leakage = db.execute(select(CurationVersionInput.source_image_uri)
                             .where(CurationVersionInput.version_id == version.id)
                             .group_by(CurationVersionInput.source_image_uri)
                             .having(func.count(func.distinct(CurationVersionInput.split)) > 1)).all()
        split_origins = {"imported": 0, "generated": 0, "hub": 0, "legacy": 0}
        test_informed = False
        for metadata in db.scalars(select(CurationVersionInput.metadata_json)
                                  .where(CurationVersionInput.version_id == version.id).execution_options(yield_per=5000)):
            origin = (metadata or {}).get("split_origin", "legacy")
            split_origins[origin if origin in split_origins else "legacy"] += 1
            test_informed = test_informed or bool((metadata or {}).get("test_informed"))
        version.statistics = {
            "test_informed": test_informed,
            "split_origins": split_origins,
            "cross_split_source_images": len(leakage),
            "classes": {label: dict(counts) for label, counts in sorted(classes.items())},
            "splits": {s: sum(c[s] for c in classes.values()) for s in SPLITS},
            "class_count": len(classes),
        }
        version.item_count = sum(version.statistics["splits"].values())
        version.event_watermark = watermark

        files = _write_files(db, project, version)
        version.artifacts = {"files": files}
        version.output_uri = str(version_dir(project, version.version))
        if settings.publish_uri():
            version.output_uri = _upload(project, version, files)
        version.status = "published"
        version.published_at = utcnow()
        db.commit()
    except Exception as exc:  # noqa: BLE001 — recorded on the version for the page to show
        logger.exception("Publishing curation version %s failed", version_id)
        db.rollback()
        version = db.get(CurationDatasetVersion, version_id)
        if version is not None:
            # A failed build leaves no half-snapshot behind to be diffed against.
            db.query(CurationVersionItem).filter_by(version_id=version_id).delete()
            version.status = "failed"
            version.error_message = f"{type(exc).__name__}: {exc}"[:4000]
            db.commit()
    finally:
        db.close()
        with _running_lock:
            _running.discard(version_id)


def hub_provenance(project: CurationProject) -> list[dict]:
    """Hugging Face datasets this project was built from or linked to (curation/registry.py).

    The config is user-reachable JSON, so anything malformed is skipped rather than
    failing the publish: a publish must never be blocked by its own provenance.
    """
    config = project.source_config if isinstance(project.source_config, dict) else {}
    found = []
    hub = config.get("hub")
    if _is_hub_ref(hub):
        found.append({"repo_id": hub["repo_id"], "revision": hub["revision"], "relation": "source"})
    links = config.get("hub_links")
    for link in links if isinstance(links, list) else []:
        if not _is_hub_ref(link):
            continue
        entry = {"repo_id": link["repo_id"], "revision": link["revision"], "relation": "linked"}
        if link.get("partial"):
            entry["partial"] = True
        found.append(entry)
    return found


def _is_hub_ref(value) -> bool:
    return (isinstance(value, dict) and isinstance(value.get("repo_id"), str) and bool(value["repo_id"])
            and isinstance(value.get("revision"), str) and bool(value["revision"]))


def point_origin(metadata: Optional[dict]) -> str:
    """Where a point came from: ``manual`` (added by a curator), ``edited`` (moved or resized, so its
    patch is cut again from the source image) or ``source`` (as imported, e.g. a Hugging Face patch)."""
    metadata = metadata or {}
    if metadata.get("manual_point"):
        return "manual"
    return "edited" if metadata.get("geometry_edited") else "source"


def _write_files(db: Session, project: CurationProject, version: CurationDatasetVersion) -> list[str]:
    folder = version_dir(project, version.version)
    folder.mkdir(parents=True, exist_ok=True)

    origins: dict[str, int] = defaultdict(int)
    with (folder / "manifest.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["item_id", "label", "split", "source_label", "crop_uri", "source_image_uri",
                         "row", "column", "crop_size", "region", "island", "site", "year", "annotator",
                            "last_changed_by", "source_key", "file_name", "origin"])
        rows = db.execute(
                     select(CurationVersionInput.item_id, CurationVersionInput.label, CurationVersionInput.split,
                         CurationVersionInput.source_label, CurationVersionInput.crop_uri, CurationVersionInput.source_image_uri,
                         CurationVersionInput.row_px, CurationVersionInput.column_px, CurationVersionInput.crop_size,
                         CurationVersionInput.region, CurationVersionInput.island, CurationVersionInput.site,
                         CurationVersionInput.survey_year, CurationVersionInput.annotator,
                         CurationVersionInput.updated_by_name, CurationVersionInput.source_key, CurationVersionInput.file_name,
                         CurationVersionInput.metadata_json)
                     .where(CurationVersionInput.version_id == version.id)
                     .order_by(CurationVersionInput.split, CurationVersionInput.label, CurationVersionInput.item_id)
            .execution_options(yield_per=5000)
        )
        for *row, metadata in rows:
            origin = point_origin(metadata)
            origins[origin] += 1
            writer.writerow((*row, origin))

    summary = {
        "input_schema": 1,
        "manifest_sha256": hashlib.sha256((folder / "manifest.csv").read_bytes()).hexdigest(),
        "project": project.name, "project_id": project.id, "dataset_mode": project.dataset_mode,
        "version": version.version, "published_by": version.created_by_name,
        "published_at": utcnow().isoformat() + "Z", "notes": version.notes,
        "items": version.item_count, "split_seed": version.split_seed,
        "split_ratios": list(settings.split_ratios()), "statistics": version.statistics,
        "changes": version.changes, "contributors": version.contributors,
        "event_watermark": version.event_watermark,
        "change_sets": (version.changes or {}).get("change_sets", []),
        "uncommitted": (version.changes or {}).get("uncommitted", []),
        "derived_from": hub_provenance(project),
        # derived_from names the dataset the project began as; these say how many rows are still as it had them.
        "origins": dict(origins),
    }
    (folder / "manifest.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    (folder / "CHANGELOG.md").write_text(changelog_markdown(project, version), encoding="utf-8")
    return ["manifest.csv", "manifest.json", "CHANGELOG.md"]


def changelog_markdown(project: CurationProject, version: CurationDatasetVersion) -> str:
    changes, stats = version.changes or {}, version.statistics or {}
    previous = changes.get("previous_version")
    lines = [
        f"# {project.name} — v{version.version}",
        "",
        f"Published by {version.created_by_name or 'unknown'} on {utcnow():%Y-%m-%d %H:%M} UTC."
        + (f" Compared with v{previous}." if previous else " First version."),
    ]
    derived = [f"Derived from: {source['repo_id']}@{source['revision'][:8]} ({source['relation']})"
               + (" (partial)" if source.get("partial") else "") + "." for source in hub_provenance(project)]
    for line in derived:  # each a paragraph of its own, not run into "Published by"
        lines += ["", line]
    if version.notes:
        lines += ["", f"> {version.notes}"]
    splits = stats.get("splits", {})
    lines += [
        "", "## In and out", "",
        f"| | items |", "|---|---:|",
        f"| before | {changes.get('before', 0):,} |",
        f"| came in | +{changes.get('in', 0):,} |",
        f"| went out | −{changes.get('out', 0):,} |",
        f"| relabelled | {changes.get('relabelled', 0):,} |",
        f"| changed split | {changes.get('moved', 0):,} |",
        f"| **now** | **{version.item_count:,}** "
        f"(train {splits.get('train', 0):,} · val {splits.get('val', 0):,} · test {splits.get('test', 0):,}) |",
    ]
    per_class = changes.get("per_class") or []
    if per_class:
        lines += ["", "## By class", "", "| class | before | in | out | relabelled in | relabelled out | moved split | now | net |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for row in per_class:
            lines.append(f"| {row['label']} | {row['before']:,} | {row['in']:,} | {row['out']:,} | "
                         f"{row['relabelled_in']:,} | {row['relabelled_out']:,} | {row.get('moved', 0):,} | "
                         f"{row['after']:,} | {row['net']:+,} |")
    relabels = changes.get("relabels") or []
    if relabels:
        lines += ["", "## Relabelled", ""] + [f"- {r['from']} → {r['to']}: {r['count']:,}" for r in relabels[:50]]
    split_moves = changes.get("split_moves") or []
    if split_moves:
        lines += ["", "## Split moves", ""] + [f"- {m['from']} → {m['to']}: {m['count']:,}" for m in split_moves[:50]]
    people = version.contributors or []
    if people:
        lines += ["", "## Contributors", ""]
        for person in people:
            focus = ", ".join(f"{c['label']} ({c['items']:,})" for c in person.get("classes", []))
            lines.append(f"- **{person['name']}** — {person['changes']:,} changes on {person['items']:,} items"
                         + (f"; mostly {focus}" if focus else ""))
    committed = changes.get("change_sets") or []
    if committed:
        lines += ["", "## Change sets", ""] + [
            f"- **{cs['author']}** — {cs['message']} ({cs['change_count']:,} changes)" for cs in committed]
    left = changes.get("uncommitted") or []
    if left:
        lines += ["", "Uncommitted: " + " · ".join(f"{u['name']} {u['count']:,}" for u in left)]
    return "\n".join(lines) + "\n"


def _upload(project: CurationProject, version: CurationDatasetVersion, files: list[str]) -> str:
    """Also copy the version's files to CURATION_PUBLISH_URI (gs://…), if set."""
    from library.storage.gcs import GCSBackend, parse_gs_uri  # noqa: PLC0415
    root = settings.publish_uri().rstrip("/")
    bucket, prefix = parse_gs_uri(root)
    backend = GCSBackend(f"gs://{bucket}")
    key_root = "/".join(p for p in (prefix, _slug(project.name), f"v{version.version}") if p)
    folder = version_dir(project, version.version)
    for name in files:
        backend.write_bytes(f"{key_root}/{name}", (folder / name).read_bytes(),
                            "text/csv" if name.endswith(".csv") else "text/plain")
    return f"gs://{bucket}/{key_root}"


# ── Image folders (optional, slow) ────────────────────────────────────────────

def start_image_export(db: Session, version: CurationDatasetVersion) -> None:
    if version.status != "published":
        raise ValueError("Only a published version can be exported as images")
    if (version.artifacts or {}).get("images", {}).get("status") == "running":
        raise ValueError("Images for this version are already being written")
    version.artifacts = {**(version.artifacts or {}), "images": {"status": "running", "done": 0, "failed": 0,
                                                               "total": version.item_count}}
    db.commit()
    threading.Thread(target=_export_images, args=(version.id,), daemon=True,
                     name=f"curation-images-{version.id}").start()


def _export_images(version_id: int) -> None:
    """Write <split>/<label>/<file> — the folder layout YOLO and timm classifiers train from."""
    from curation import images  # noqa: PLC0415
    db = new_session()
    state = {"status": "running", "done": 0, "failed": 0}
    try:
        version = db.get(CurationDatasetVersion, version_id)
        project = db.get(CurationProject, version.project_id)
        state["total"] = version.item_count
        target_root = settings.publish_uri().rstrip("/") if settings.publish_uri() else None
        local_root = version_dir(project, version.version) / "images"
        rows = db.execute(
            select(CurationVersionInput.label, CurationVersionInput.split, CurationVersionInput)
            .where(CurationVersionInput.version_id == version.id)
            .execution_options(yield_per=500)
        ).all()
        if len(rows) != version.item_count:
            raise ValueError("This legacy version has no frozen inputs; publish a new version before exporting images")
        for index, (label, split, item) in enumerate(rows, start=1):
            relative = f"{split}/{label}/{item.id}_{os.path.basename(item.file_name)}"
            try:
                payload = None
                if images.needs_fresh_crop(item) and item.row_px is not None:
                    # An edited or added patch has no stored crop that matches it: never fall back to crop_uri.
                    if not images.is_allowed(db, item.source_image_uri):
                        raise PermissionError(item.source_image_uri)
                    payload = images.crop_from_source(item)
                else:
                    for uri in images.crop_candidates(item, project):
                        if not images.is_allowed(db, uri):
                            continue
                        try:
                            payload = storage.read_bytes(uri)
                            break
                        except FileNotFoundError:
                            continue
                    if payload is None and item.row_px is not None and images.is_allowed(db, item.source_image_uri):
                        payload = images.crop_from_source(item)
                if payload is None:
                    raise FileNotFoundError(item.crop_uri)
                if target_root:
                    from library.storage.gcs import GCSBackend, parse_gs_uri  # noqa: PLC0415
                    bucket, prefix = parse_gs_uri(target_root)
                    key = "/".join(p for p in (prefix, _slug(project.name), f"v{version.version}", "images", relative) if p)
                    GCSBackend(f"gs://{bucket}").write_bytes(key, payload, "image/png")
                else:
                    path = local_root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(payload)
                state["done"] += 1
            except Exception:  # noqa: BLE001 — counted; one missing crop must not stop the rest
                state["failed"] += 1
            if index % 200 == 0:
                _save_image_state(version_id, state)
        state["status"] = "completed"
        state["location"] = f"{target_root}/{_slug(project.name)}/v{version.version}/images" if target_root else str(local_root)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Image export for version %s failed", version_id)
        state.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        db.close()
        _save_image_state(version_id, state)


def _save_image_state(version_id: int, state: dict) -> None:
    with session_scope() as db:
        version = db.get(CurationDatasetVersion, version_id)
        if version is not None:
            version.artifacts = {**(version.artifacts or {}), "images": dict(state)}


def mark_interrupted(db: Session) -> int:
    """Versions or image exports that were mid-build when the process died."""
    count = 0
    for version in db.scalars(select(CurationDatasetVersion).where(CurationDatasetVersion.status == "building")):
        db.query(CurationVersionItem).filter_by(version_id=version.id).delete()
        db.query(CurationVersionInput).filter_by(version_id=version.id).delete()
        version.status = "interrupted"
        version.error_message = "The server restarted while this version was being built. Publish again."
        count += 1
    for version in db.scalars(select(CurationDatasetVersion).where(CurationDatasetVersion.status == "published")):
        images_state = (version.artifacts or {}).get("images") or {}
        if images_state.get("status") == "running":
            version.artifacts = {**version.artifacts, "images": {**images_state, "status": "interrupted"}}
            count += 1
    return count
