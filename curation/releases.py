"""
Combined releases: one dataset built from a published version of each of
several projects. Reads only frozen version rows, so a release is reproducible.

The same crop (file_name, source_label) twice with the same label is kept once
and counted as a duplicate; with different labels it is a conflict — every row
of that crop is left out and listed in conflicts.csv. A source image that ends
up in more than one split across projects is listed in leakage.csv; splits are
not rewritten, because each project's lead owns them. A build streams the rows:
each crop's winner (its first row in request order) is picked in SQL, so memory
holds the conflicted crop keys and their rows, one entry per source image for
the leakage check and the counters — no full rows of kept crops.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import shutil
import threading
from collections import defaultdict
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from appdb.base import utcnow
from appdb.engine import session_scope
from auth.service import CurrentUser
from curation import members, settings
from curation.models import CurationDatasetVersion, CurationProject, CurationRelease, CurationVersionInput
from curation.versions import SPLITS, _slug

logger = logging.getLogger(__name__)
FILES = ("manifest.csv", "manifest.json", "CHANGELOG.md", "conflicts.csv", "leakage.csv")
COLUMNS = ["project", "project_version", "item_id", "label", "split", "source_label", "crop_uri", "source_image_uri",
           "row", "column", "crop_size", "region", "island", "site", "year", "annotator", "source_key", "file_name"]


def _folder_name(release: CurationRelease) -> str:
    slug = _slug(release.name)
    return f"{release.id}-{slug if slug not in ('.', '..') else 'release'}"


def release_dir(release: CurationRelease) -> Path:
    return settings.versions_dir() / "_releases" / _folder_name(release) / f"r{release.number}"


def as_dict(release: CurationRelease) -> dict:
    return {"id": release.id, "name": release.name, "number": release.number, "dataset_mode": release.dataset_mode,
            "status": release.status, "notes": release.notes, "sources": release.sources or [],
            "statistics": release.statistics or {}, "item_count": release.item_count,
            "output_uri": release.output_uri, "error_message": release.error_message,
            "created_by_name": release.created_by_name, "created_at": release.created_at,
            "published_at": release.published_at, "files": list(FILES) if release.status == "published" else []}


def start(db: Session, user: CurrentUser, name: str, dataset_mode: str, sources: list[dict],
          notes: Optional[str]) -> CurationRelease:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Give the release a name")
    if not sources:
        raise HTTPException(status_code=422, detail="Choose at least one project version")
    if len({s["project_id"] for s in sources}) != len(sources):
        raise HTTPException(status_code=422, detail="Each project can appear only once")
    if not user.has_role("lead"):
        raise HTTPException(status_code=403, detail="Building a release needs the app-wide lead role")
    resolved = []
    for source in sources:
        project = db.get(CurationProject, source["project_id"])
        if project is None:
            raise HTTPException(status_code=422, detail="Unknown project")
        version = db.scalar(select(CurationDatasetVersion).where(
            CurationDatasetVersion.project_id == project.id, CurationDatasetVersion.version == source["version"]))
        if version is None or version.status != "published":
            raise HTTPException(status_code=422, detail=f"{project.name} has no published v{source['version']}")
        if project.dataset_mode != dataset_mode:
            raise HTTPException(status_code=422, detail=f"{project.name} is {project.dataset_mode}, not {dataset_mode}")
        if members.project_role(db, project.id, user) != "lead":
            raise HTTPException(status_code=403, detail=f"You need the project lead role on {project.name}")
        has_inputs = db.scalar(select(CurationVersionInput.item_id)
                               .where(CurationVersionInput.version_id == version.id).limit(1)) is not None
        if version.item_count and not has_inputs:
            raise HTTPException(status_code=422, detail=f"v{version.version} of {project.name} was published before "
                                                        "releases were supported — publish a new version first")
        resolved.append({"project_id": project.id, "project_name": project.name, "version": version.version,
                         "version_id": version.id, "notes": version.notes, "item_count": version.item_count})
    for _ in range(3):
        number = (db.scalar(select(func.max(CurationRelease.number)).where(CurationRelease.name == name)) or 0) + 1
        release = CurationRelease(name=name, number=number, dataset_mode=dataset_mode, status="building",
                                  notes=(notes or "").strip() or None, sources=resolved,
                                  created_by_user_id=user.id, created_by_name=user.label)
        db.add(release)
        try:
            db.commit()
            break
        except IntegrityError:
            db.rollback()
    else:
        raise HTTPException(status_code=409, detail="Another release with this name is being numbered; try again")
    db.refresh(release)
    threading.Thread(target=_build, args=(release.id,), name=f"release-{release.id}", daemon=True).start()
    return release


def _build(release_id: int) -> None:
    folder: Optional[Path] = None
    try:
        with session_scope() as db:
            release = db.get(CurationRelease, release_id)
            folder = release_dir(release)
            if folder.exists():
                shutil.rmtree(folder)
            folder.mkdir(parents=True)
            _write(db, folder, release)
            release.status = "published"
            release.published_at = utcnow()
            release.output_uri = _upload_dir(folder, release) or str(folder)
    except Exception as exc:  # noqa: BLE001 — the release carries the reason
        logger.exception("Release %s failed", release_id)
        if folder is not None and folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        try:
            with session_scope() as db:
                failed = db.get(CurationRelease, release_id)
                if failed is not None:
                    failed.status = "failed"
                    failed.error_message = str(exc)[:2000] or type(exc).__name__
        except Exception:  # noqa: BLE001
            logger.exception("Could not record the failure of release %s", release_id)


_ROW = (CurationVersionInput.item_id, CurationVersionInput.label, CurationVersionInput.split,
        CurationVersionInput.source_label, CurationVersionInput.crop_uri,
        CurationVersionInput.source_image_uri, CurationVersionInput.row_px, CurationVersionInput.column_px,
        CurationVersionInput.crop_size, CurationVersionInput.region, CurationVersionInput.island,
        CurationVersionInput.site, CurationVersionInput.survey_year, CurationVersionInput.annotator,
        CurationVersionInput.source_key, CurationVersionInput.file_name)


def _write(db: Session, folder: Path, release: CurationRelease) -> None:
    """Stream the release's files straight from the frozen version rows.

    A crop's winner is its first row in request order (source position, then
    item_id), picked in SQL; the manifest is written in (split, label, project
    name, item_id) order. Only the conflicted keys, the conflicting rows and the
    per-image split sets are held in memory.
    """
    sources = release.sources
    vids = [s["version_id"] for s in sources]   # ≤ one per project, well under Oracle's 1000-item IN limit
    position = {s["version_id"]: index for index, s in enumerate(sources)}
    names = sorted({s["project_name"] for s in sources})
    name_rank = {s["version_id"]: names.index(s["project_name"]) for s in sources}
    by_vid = {s["version_id"]: s for s in sources}
    vi = CurationVersionInput
    in_release = vi.version_id.in_(vids)

    conflicted = {tuple(key) for key in db.execute(
        select(vi.file_name, vi.source_label).where(in_release)
        .group_by(vi.file_name, vi.source_label).having(func.count(func.distinct(vi.label)) > 1))}
    total = db.scalar(select(func.count()).select_from(vi).where(in_release)) or 0

    # conflicts.csv: every row of a conflicted crop, grouped by crop, in request order within it.
    groups: dict[tuple, list] = defaultdict(list)
    if conflicted:
        rows = db.execute(select(vi.version_id, vi.item_id, vi.label, vi.split, vi.file_name, vi.source_label)
                          .where(in_release).execution_options(yield_per=5000))
        for row in rows:
            key = (row.file_name, row.source_label)
            if key in conflicted:
                groups[key].append((position[row.version_id], row.item_id, row.version_id, row.label, row.split))
    conflicted_rows = sum(len(group) for group in groups.values())
    with (folder / "conflicts.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["file_name", "source_label", "project", "project_version", "item_id", "label", "split"])
        for key in sorted(conflicted):
            for _, item_id, vid, label, split in sorted(groups[key]):
                writer.writerow([key[0], key[1], by_vid[vid]["project_name"], by_vid[vid]["version"], item_id, label, split])
    del groups

    classes: dict[str, dict[str, int]] = defaultdict(lambda: {s: 0 for s in SPLITS})
    per_source: dict[str, int] = defaultdict(int)
    image_splits: dict[str, set] = defaultdict(set)
    image_owners: dict[str, set] = defaultdict(set)
    written = 0
    first = func.row_number().over(
        partition_by=(vi.file_name, vi.source_label),
        order_by=(case(position, value=vi.version_id), vi.item_id)).label("rn")
    ranked = select(vi.version_id, *_ROW, first).where(in_release).subquery("ranked")
    winners = select(ranked).where(ranked.c.rn == 1).order_by(
        ranked.c.split, ranked.c.label, case(name_rank, value=ranked.c.version_id), ranked.c.item_id,
        case(position, value=ranked.c.version_id))
    with (folder / "manifest.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(COLUMNS)
        for row in db.execute(winners.execution_options(yield_per=5000)):
            if (row.file_name, row.source_label) in conflicted:
                continue
            source = by_vid[row.version_id]
            project, version, split = source["project_name"], source["version"], row.split
            writer.writerow((project, version, *(getattr(row, column.key) for column in _ROW)))
            written += 1
            classes[row.label][split if split in SPLITS else "train"] += 1
            per_source[f"{project} v{version}"] += 1
            image_splits[row.source_image_uri].add(split)
            image_owners[row.source_image_uri].add(f"{project}:{split}")
    duplicates = total - conflicted_rows - written
    leaks = sorted(image for image, splits in image_splits.items() if len(splits) > 1)
    with (folder / "leakage.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["source_image_uri", "project_splits"])
        for image in leaks:
            writer.writerow([image, " ".join(sorted(image_owners[image]))])
    release.statistics = {
        "classes": {label: dict(counts) for label, counts in sorted(classes.items())},
        "splits": {s: sum(c[s] for c in classes.values()) for s in SPLITS},
        "per_source": dict(per_source), "duplicates": duplicates, "conflicts": len(conflicted),
        "leakage": len(leaks), "class_count": len(classes),
    }
    release.item_count = written
    summary = {"release": release.name, "number": release.number, "dataset_mode": release.dataset_mode,
               "notes": release.notes, "sources": release.sources, "statistics": release.statistics,
               "manifest_sha256": _sha256(folder / "manifest.csv"),
               "built_by": release.created_by_name, "built_at": utcnow().isoformat() + "Z"}
    (folder / "manifest.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    (folder / "CHANGELOG.md").write_text(_changelog(release), encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _changelog(release: CurationRelease) -> str:
    stats = release.statistics
    lines = [f"# {release.name} — r{release.number}", "",
             f"Built by {release.created_by_name or 'unknown'} on {utcnow():%Y-%m-%d %H:%M} UTC "
             f"({release.dataset_mode})."]
    if release.notes:
        lines += ["", f"> {release.notes}"]
    lines += ["", "## Sources", ""] + [
        f"- {s['project_name']} v{s['version']} — {s['item_count']:,} items" + (f": {s['notes']}" if s.get("notes") else "")
        for s in release.sources]
    with session_scope() as db:
        previous = db.scalar(select(CurationRelease).where(
            CurationRelease.name == release.name, CurationRelease.number < release.number,
            CurationRelease.status == "published").order_by(CurationRelease.number.desc()).limit(1))
        before = {s["project_name"]: s["version"] for s in (previous.sources if previous else [])}
    now = {s["project_name"]: s["version"] for s in release.sources}
    if previous is not None:
        moves = [f"- {p}: v{before[p]} → v{now[p]}" for p in sorted(now) if p in before and before[p] != now[p]]
        moves += [f"- {p}: added (v{now[p]})" for p in sorted(now) if p not in before]
        moves += [f"- {p}: removed" for p in sorted(before) if p not in now]
        lines += ["", f"## Since r{previous.number}", ""] + (moves or ["- Sources unchanged."])
    splits = stats["splits"]
    lines += ["", "## Totals", "",
              f"- {release.item_count:,} items (train {splits['train']:,} · val {splits['val']:,} · test {splits['test']:,})",
              f"- {stats['duplicates']:,} duplicate crops kept once",
              f"- {stats['conflicts']:,} crops left out for conflicting labels (conflicts.csv)",
              f"- {stats['leakage']:,} source images in more than one split (leakage.csv)"]
    return "\n".join(lines) + "\n"


def _upload_dir(folder: Path, release: CurationRelease) -> Optional[str]:
    """Copy the release's files to CURATION_PUBLISH_URI (gs://…) if set, as versions._upload does."""
    if not settings.publish_uri():
        return None
    from library.storage.gcs import GCSBackend, parse_gs_uri  # noqa: PLC0415

    bucket, prefix = parse_gs_uri(settings.publish_uri().rstrip("/"))
    backend = GCSBackend(f"gs://{bucket}")
    key_root = "/".join(p for p in (prefix, "_releases", _folder_name(release), f"r{release.number}") if p)
    for name in FILES:
        backend.write_bytes(f"{key_root}/{name}", (folder / name).read_bytes(),
                            "text/csv" if name.endswith(".csv") else "application/json" if name.endswith(".json") else "text/markdown")
    return f"gs://{bucket}/{key_root}"


def mark_interrupted(db: Session) -> int:
    count = 0
    for release in db.scalars(select(CurationRelease).where(CurationRelease.status == "building")):
        release.status = "failed"
        release.error_message = "The server restarted while this release was being built — build it again."
        count += 1
    return count
