"""
The Hugging Face registry: look a repository up, register it at a pinned
commit, and hand datasets to the import pool and model weights to the
existing classifier registry (curation/model_registry.py).
"""
from __future__ import annotations

from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth.service import CurrentUser
from curation import evaluation, huggingface, hub_rows, images, model_registry, settings, storage
from curation.models import CurationHubEntry, CurationProject

RUNNABLE_MODEL = {"pipeline_tag": "image-classification", "library_name": "ultralytics"}
SAMPLE = 5


def _baseline(sha: str) -> bool:
    return sha in {evaluation.EXPECTED_MODEL_REVISION, evaluation.EXPECTED_DATASET_REVISION}


def _task(info: huggingface.RepoInfo) -> str | None:
    """The card's task, cut to the column's 40 characters (Oracle refuses longer)."""
    if info.kind == "model":
        task = info.card.get("pipeline_tag")
    else:
        categories = info.card.get("task_categories") or []
        task = categories[0] if categories else None
    return str(task)[:40] if task else None


def _weight_files(info: huggingface.RepoInfo) -> list[str]:
    return sorted(entry["path"] for entry in info.files if entry["path"].lower().endswith(".pt"))


def _model_runnable(info: huggingface.RepoInfo) -> tuple[bool, str | None]:
    if info.card.get("library_name") != RUNNABLE_MODEL["library_name"]:
        return False, "Only Ultralytics models can be evaluated here for now"
    if info.card.get("pipeline_tag") != RUNNABLE_MODEL["pipeline_tag"]:
        return False, (f"{info.card.get('pipeline_tag') or 'This task'} is catalogued only; "
                       "evaluation supports image classification")
    if not _weight_files(info):
        return False, "The repository has no .pt checkpoint"
    return True, None


def _dataset_shape(info: huggingface.RepoInfo) -> tuple[dict, dict, dict, str | None]:
    """(parquet by split, columns, detected column map, reason it cannot be used)."""
    parquet = hub_rows.parquet_by_split(info.card, info.files)
    columns = hub_rows.features(info.card)
    if not parquet:
        return parquet, columns, {}, "The dataset has no parquet files"
    try:
        return parquet, columns, hub_rows.detect_columns(columns), None
    except HTTPException as exc:
        return parquet, columns, {}, str(exc.detail)


def _split_counts(card: dict) -> dict[str, int]:
    info = card.get("dataset_info") or {}
    if isinstance(info, list):
        info = info[0] if info else {}
    counts = {}
    for split in info.get("splits") or []:
        name = hub_rows._split(split.get("name", ""))  # noqa: SLF001 — same package
        counts[name] = counts.get(name, 0) + int(split.get("num_examples") or 0)
    return counts


def _existing(db: Session, info: huggingface.RepoInfo) -> CurationHubEntry | None:
    return db.scalar(select(CurationHubEntry).where(
        CurationHubEntry.kind == info.kind, CurationHubEntry.repo_id == info.repo_id,
        CurationHubEntry.revision == info.sha))


def lookup(db: Session, text: str, kind: str | None) -> dict:
    info = huggingface.repo_info(huggingface.parse_ref(text, kind))
    existing = _existing(db, info)
    preview = {
        "kind": info.kind, "repo_id": info.repo_id, "revision": info.sha, "task": _task(info),
        "license": info.card.get("license"), "tags": info.card.get("tags") or [],
        "url": huggingface.browse_url(info.kind, info.repo_id, info.sha),
        "matches_t3_baseline": _baseline(info.sha),
        "existing_entry_id": existing.id if existing else None,
    }
    if info.kind == "model":
        runnable, reason = _model_runnable(info)
        return {**preview, "runnable": runnable, "not_runnable_reason": reason,
                "library_name": info.card.get("library_name"), "weight_files": _weight_files(info),
                "linked_datasets": list(info.card.get("datasets") or []),
                "images": [entry["path"] for entry in info.files if entry["path"].lower().endswith(".png")]}
    parquet, columns, column_map, reason = _dataset_shape(info)
    return {**preview, "runnable": reason is None, "not_runnable_reason": reason,
            "columns": columns, "column_map": column_map, "parquet": parquet,
            "splits": _split_counts(info.card) or {split: None for split in parquet}}


def _create(db: Session, info: huggingface.RepoInfo, requested: str, user: CurrentUser, **fields) -> CurationHubEntry:
    entry = CurationHubEntry(kind=info.kind, repo_id=info.repo_id, revision=info.sha, requested_ref=requested[:100],
                             task=_task(info), card=info.card, files=info.files,
                             created_by_user_id=user.id, created_by_name=user.label, **fields)
    db.add(entry)
    try:
        db.commit()
    except IntegrityError:
        # Two leads registered the same commit at once: the other one won.
        db.rollback()
        entry = _existing(db, info)
        if entry is None:
            raise
    db.refresh(entry)
    return entry


def _check_location(db: Session, info: huggingface.RepoInfo, column_map: dict, location: dict) -> dict:
    if location.get("mode") not in {"column", "env_root", "custom"}:
        raise HTTPException(422, "Choose where the patches are: the dataset's column, the configured root, or a gs:// root")
    entry_like = SimpleNamespace(kind=info.kind, repo_id=info.repo_id, revision=info.sha,
                                 card=info.card, files=info.files)
    names = hub_rows.class_names(info.card, column_map["label"])
    found, checked = 0, 0
    for split, row in hub_rows.sample_rows(entry_like, SAMPLE):
        split, label, file_name = hub_rows.row_fields(row, split, column_map, names)
        uri = hub_rows.crop_uri(row, split, label, file_name, column_map, location)
        images.require_allowed(db, uri)
        checked += 1
        try:
            storage.read_bytes(uri)
            found += 1
        except (FileNotFoundError, PermissionError):
            pass
    if not checked:
        raise HTTPException(422, "No sample rows could be read from this dataset")
    if checked and not found:
        raise HTTPException(422, f"None of {checked} sample patches were found where this location points")
    return {"checked": checked, "found": found}


def _location_key(location: dict) -> tuple:
    mode = location.get("mode")
    return mode, (str(location.get("root") or "").rstrip("/") or None) if mode == "custom" else None


def _describe_location(location: dict) -> str:
    mode, root = _location_key(location)
    return f"{mode} {root}" if root else str(mode)


def _named(column_map: dict) -> dict:
    return {role: name for role, name in column_map.items() if name}


def _same_dataset_options(existing: CurationHubEntry, column_map: dict, location: dict) -> None:
    """A commit is registered once: asking again with other columns or patches is a conflict, not a no-op."""
    stored_map, stored_location = existing.options.get("column_map") or {}, existing.options.get("location") or {}
    if _named(stored_map) != _named(column_map) or _location_key(stored_location) != _location_key(location):
        columns = ", ".join(f"{role}={name}" for role, name in sorted(_named(stored_map).items()))
        raise HTTPException(409, f"This commit is already registered with location {_describe_location(stored_location)} "
                                 f"/ columns {columns}")


def register_dataset(db: Session, user: CurrentUser, *, ref: str, kind: str | None = "dataset",
                     column_map: dict | None, location: dict) -> tuple[CurationHubEntry, dict]:
    parsed = huggingface.parse_ref(ref, kind or "dataset")
    info = huggingface.repo_info(parsed)
    if info.kind != "dataset":
        raise HTTPException(422, "That is a model; register it as a model")
    parquet, columns, detected, reason = _dataset_shape(info)
    if reason and not column_map:
        raise HTTPException(422, reason)
    column_map = {**detected, **(column_map or {})}
    missing = [role for role in ("image", "label") if not column_map.get(role)]
    if missing:
        raise HTTPException(422, f"Choose the dataset's {' and '.join(missing)} column")
    unknown = [name for name in column_map.values() if name and columns and name not in columns]
    if unknown:
        raise HTTPException(422, f"Unknown column(s): {', '.join(unknown)}")
    existing = _existing(db, info)
    if existing:
        _same_dataset_options(existing, column_map, location)
        return existing, existing.options.get("sample", {})
    sample = _check_location(db, info, column_map, location)
    entry = _create(db, info, parsed.ref, user, runnable=True,
                    options={"column_map": column_map, "location": location, "sample": sample})
    return entry, sample


def requeue_failed(db: Session, user: CurrentUser, entry: CurationHubEntry, **options) -> CurationHubEntry:
    """Queue a failed registration again under the acting user (the first registrant may be gone)."""
    entry.created_by_user_id, entry.created_by_name = user.id, user.label
    entry.status, entry.error_message = "queued", None
    kept = dict(entry.options)
    for key, value in options.items():
        if value and not kept.get(key):
            kept[key] = value
    entry.options = kept
    db.commit()
    db.refresh(entry)
    return entry


def register_model(db: Session, user: CurrentUser, *, ref: str, kind: str | None = "model",
                   weight_file: str | None, name: str | None, trusted: bool) -> CurationHubEntry:
    parsed = huggingface.parse_ref(ref, kind or "model")
    info = huggingface.repo_info(parsed)
    if info.kind != "model":
        raise HTTPException(422, "That is a dataset; register it as a dataset")
    existing = _existing(db, info)
    if existing:
        chosen = existing.options.get("weight_file")
        if weight_file and chosen and weight_file != chosen:
            raise HTTPException(409, f"This commit is already registered with {chosen}")
        if existing.status == "failed":
            return requeue_failed(db, user, existing, weight_file=weight_file,
                                  name=(name[:200] if name else None))
        return existing
    runnable, reason = _model_runnable(info)
    if not runnable:
        return _create(db, info, parsed.ref, user, runnable=False, options={"not_runnable_reason": reason})
    files = {entry["path"]: entry.get("size") or 0 for entry in info.files}
    if not weight_file or weight_file not in files or not weight_file.lower().endswith(".pt"):
        raise HTTPException(422, f"Choose one of the repository's .pt files: {', '.join(_weight_files(info))}")
    if not trusted:
        raise HTTPException(422, "Confirm that these classifier weights come from a trusted source")
    model_registry.require_model_policy()  # before any bytes are fetched
    if files[weight_file] > settings.model_upload_limit():
        raise HTTPException(413, "Classifier exceeds CURATION_MODEL_MAX_BYTES")
    # The weights are fetched by the coordinator (curation/registry_worker.py),
    # so this request returns at once; checks above already ran.
    display_name = (name or info.repo_id.split("/", 1)[1])[:200]
    return _create(db, info, parsed.ref, user, runnable=True, status="queued",
                   options={"weight_file": weight_file, "name": display_name})


def get_entry_or_404(db: Session, entry_id: int, kind: str | None = None) -> CurationHubEntry:
    entry = db.get(CurationHubEntry, entry_id)
    if entry is None or (kind and entry.kind != kind):
        raise HTTPException(404, "Registry entry not found")
    return entry


def _links(project: CurationProject, entry_id: int) -> bool:
    config = project.source_config if isinstance(project.source_config, dict) else {}
    hub = config.get("hub")
    if isinstance(hub, dict) and hub.get("entry_id") == entry_id:
        return True
    links = config.get("hub_links")
    return isinstance(links, list) and any(isinstance(link, dict) and link.get("entry_id") == entry_id for link in links)


def summary(db: Session, entry: CurationHubEntry, projects: list[CurationProject] | None = None) -> dict:
    projects = projects if projects is not None else db.scalars(select(CurationProject)).all()
    linked = [{"id": project.id, "name": project.name} for project in projects if _links(project, entry.id)]
    return {
        "id": entry.id, "kind": entry.kind, "repo_id": entry.repo_id, "revision": entry.revision,
        "short_revision": entry.revision[:8], "requested_ref": entry.requested_ref,
        "url": huggingface.browse_url(entry.kind, entry.repo_id, entry.revision),
        "task": entry.task, "runnable": entry.runnable,
        "not_runnable_reason": entry.options.get("not_runnable_reason"),
        "license": entry.card.get("license"), "splits": _split_counts(entry.card) if entry.kind == "dataset" else None,
        "options": {key: value for key, value in entry.options.items() if key != "sample"},
        "sample": entry.options.get("sample"), "classifier_id": entry.classifier_id,
        "status": entry.status, "error_message": entry.error_message,
        "created_by_name": entry.created_by_name, "created_at": entry.created_at,
        "matches_t3_baseline": _baseline(entry.revision), "projects": linked,
        "trained_on": list(entry.card.get("datasets") or []) if entry.kind == "model" else [],
    }


def create_project_from_dataset(db: Session, user: CurrentUser, entry: CurationHubEntry, *, name: str,
                                description: str | None, dataset_mode: str, splits: list[str],
                                starter: str | None = None):
    from curation import service  # noqa: PLC0415
    from curation.models import CurationImportOperation  # noqa: PLC0415
    from curation.schemas import CurationProjectCreate  # noqa: PLC0415

    if not entry.runnable:
        raise HTTPException(422, "This dataset cannot be imported")
    available = set(hub_rows.parquet_by_split(entry.card, entry.files))
    unknown = [split for split in splits if split not in available]
    if unknown or not available:
        raise HTTPException(422, f"Unknown split(s) {', '.join(unknown) or '(none)'}; choose from: {', '.join(sorted(available))}")
    chosen = list(dict.fromkeys(splits)) or sorted(available)
    hub = {"entry_id": entry.id, "repo_id": entry.repo_id, "revision": entry.revision}
    project = service.create_project(db, CurationProjectCreate(
        name=name, description=description or f"Patches from {entry.repo_id}@{entry.revision[:8]}",
        dataset_mode=dataset_mode, source_config={"hub": hub, **({"starter": starter} if starter else {})}),
        user, source_kind="huggingface")
    operation = CurationImportOperation(
        project_id=project.id, batch_size=2000, created_by_user_id=user.id, created_by_name=user.label,
        sources=[{"kind": "hub_dataset", "name": f"{entry.repo_id}@{entry.revision[:8]}"[:200],
                  "entry_id": entry.id, "splits": chosen}])
    db.add(operation)
    db.commit()
    db.refresh(project)
    db.refresh(operation)
    return project, operation


def resume_import(db: Session, user: CurrentUser, entry: CurationHubEntry, project: CurationProject):
    """Queue a project's hub import again, with the same splits; rows that landed are skipped."""
    from curation.models import CurationImportOperation  # noqa: PLC0415

    config = project.source_config if isinstance(project.source_config, dict) else {}
    hub = config.get("hub")
    if not isinstance(hub, dict) or hub.get("entry_id") != entry.id:
        raise HTTPException(422, "This project was not created from this dataset")
    operations = db.scalars(select(CurationImportOperation).where(CurationImportOperation.project_id == project.id)
                            .order_by(CurationImportOperation.created_at.desc())).all()
    if any(operation.status in {"queued", "starting", "running"} for operation in operations):
        raise HTTPException(409, "An import is already queued or running for this project")
    previous = next((source for operation in operations for source in operation.sources or []
                     if source.get("kind") == "hub_dataset" and source.get("entry_id") == entry.id), None)
    splits = list(previous.get("splits") or []) if previous else []
    operation = CurationImportOperation(
        project_id=project.id, batch_size=2000, created_by_user_id=user.id, created_by_name=user.label,
        sources=[{"kind": "hub_dataset", "name": f"{entry.repo_id}@{entry.revision[:8]}"[:200],
                  "entry_id": entry.id, "splits": splits or sorted(hub_rows.parquet_by_split(entry.card, entry.files))}])
    db.add(operation)
    db.commit()
    db.refresh(operation)
    return operation


def label_report(entry: CurationHubEntry, dataset_mode: str, class_names: list[str] | None = None) -> list[dict]:
    """Labels with counts; with no taxonomy file installed every label is reported as known.

    With a model's class_names, each row also says whether the model knows the label.
    """
    from collections import Counter  # noqa: PLC0415

    from curation import taxonomy  # noqa: PLC0415

    column_map = entry.options["column_map"]
    names = hub_rows.class_names(entry.card, column_map["label"])
    counts: Counter = Counter()
    for split, row in hub_rows.iter_rows(entry, None, [name for name in column_map.values() if name]):
        try:
            counts[hub_rows.row_fields(row, split, column_map, names)[1]] += 1
        except (TypeError, ValueError):
            continue
    check = taxonomy.available()
    rows = [{"label": label, "count": count, "effective": taxonomy.effective_label(label, dataset_mode),
             "known": (not check) or taxonomy.project_code(label, dataset_mode) is not None}
            for label, count in sorted(counts.items())]
    if class_names is not None:  # key only present when a model was asked about, so plain reports are unchanged
        for row in rows:
            row["in_model"] = row["effective"] in class_names
    return rows


def queue_link(db: Session, user: CurrentUser, entry: CurationHubEntry, project: CurationProject, *,
               adopt_splits: bool, dry_run: bool):
    from curation.models import CurationImportOperation  # noqa: PLC0415

    if not entry.runnable:
        raise HTTPException(422, "This dataset cannot be linked")
    verb = "Preview link" if dry_run else "Link"
    operation = CurationImportOperation(
        project_id=project.id, batch_size=2000, created_by_user_id=user.id, created_by_name=user.label,
        sources=[{"kind": "hub_link", "name": f"{verb} {entry.repo_id}@{entry.revision[:8]}"[:200],
                  "entry_id": entry.id, "adopt_splits": adopt_splits, "dry_run": dry_run}])
    db.add(operation)
    db.commit()
    db.refresh(operation)
    return operation
