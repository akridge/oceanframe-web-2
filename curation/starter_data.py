"""
Starter data on a new install: the NOAA Pacific T3 regional project, plus the
Hugging Face dataset (as a project) and the classifier model, which is then
scored once on that project's test and val splits (the matrix's baseline).

``ensure_once`` is the synchronous, idempotent core; ``start_background`` runs
it from a daemon thread on the coordinator, retrying with backoff while the
network is out, so start-up never waits on (or fails because of) Hugging Face.
A starter project an admin deletes leaves a ledger row, and stays deleted.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from appdb.base import utcnow
from appdb.engine import session_scope
from curation import importer, model_registry, registry, settings
from curation.models import (CurationClassifier, CurationDatasetVersion, CurationEvaluationRun, CurationHubEntry,
                             CurationImportOperation, CurationProject)

logger = logging.getLogger(__name__)

REGIONAL = importer.REGIONAL_MARKER
HUB = "noaa-t3-hub"
HUB_PROJECT = "NOAA Pacific T3 (Hugging Face)"
DATASET_REF = "NMFS-OSI/noaa-pacific-benthic-cover-t3"
MODEL_REF = "NMFS-OSI/yolo11m-cls-noaa-pacific-benthic-cover-t3"
# The way the Hub tab fills its form for this dataset: columns as detected from
# the card, patches from the dataset's own image column.
DATASET_LOCATION = {"mode": "column"}
# The repository holds weights/best.pt and a second copy named after the model;
# best.pt is what the T3 baseline was scored with (docs/CURATION.md).
PREFERRED_WEIGHT = "best.pt"
# The baseline the starter model is scored on, once; the ledger key marks its v1 publish.
BASELINE_SPLITS = ("test", "val")
BASELINE_PUBLISH = "starter-bl-publish"

# migration_id is 20 characters wide: a short key per marker.
_REMOVED = {REGIONAL: "starter-rm-regional", HUB: "starter-rm-hub"}
_STUCK = {"failed", "canceled", "interrupted"}
_ACTIVE = {"queued", "starting", "running"}
FIRST_DELAY, MAX_DELAY = 60.0, 1800.0
_FIXES = {
    "regional": "Check CURATION_IMPORT_SOURCES and that the app can reach the bucket.",
    "dataset": "Allow the Hugging Face owner (CURATION_HF_ALLOWED_ORGS) and the image bucket (CURATION_IMAGE_BUCKETS), "
               "or set CURATION_STARTER_DATA=regional.",
    "model": "Allow the Hugging Face owner (CURATION_HF_ALLOWED_ORGS), or set CURATION_STARTER_DATA=regional.",
    "context": "Check that both starter imports completed under Dataset status.",
    "baseline": "Check the evaluation worker (oceanframe-evaluation) and the model under Models and evaluation runs.",
}


# ── Deletion sticks ───────────────────────────────────────────────────────────

def record_removed(db: Session, project: CurationProject) -> None:
    """Called as an admin deletes a project: a starter project is not brought back."""
    from appdb.bootstrap import _ledger  # noqa: PLC0415

    config = project.source_config if isinstance(project.source_config, dict) else {}
    key = _REMOVED.get(config.get("starter"))
    if key and not _ledger_has(db, key):
        db.execute(_ledger.insert().values(migration_id=key, description=f"Removed {project.name}"[:200],
                                           applied_at=utcnow()))


def _ledger_has(db: Session, migration_id: str) -> bool:
    from appdb.bootstrap import _ledger  # noqa: PLC0415

    return db.execute(select(_ledger.c.migration_id).where(_ledger.c.migration_id == migration_id)).first() is not None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _marker(project: CurationProject) -> Optional[str]:
    config = project.source_config if isinstance(project.source_config, dict) else {}
    return config.get("starter")


def _marked(db: Session, marker: str) -> Optional[CurationProject]:
    return next((project for project in db.scalars(select(CurationProject).order_by(CurationProject.created_at))
                 if _marker(project) == marker), None)


def _latest_status(db: Session, project: CurationProject) -> Optional[str]:
    """The status of the project's latest import. Availability checks run as operations too (one is queued
    after every import) but are not imports, so they are passed over."""
    from curation import availability  # noqa: PLC0415

    operations = db.scalars(select(CurationImportOperation).where(CurationImportOperation.project_id == project.id)
                            .order_by(CurationImportOperation.created_at.desc()).limit(20))
    return next((operation.status for operation in operations if not availability.is_check(operation)), None)


def _needs_import(db: Session, project: CurationProject) -> bool:
    """Its last import was cut off by a restart (resumed: rows already in are skipped), or it is empty with
    nothing queued or running because its last import failed or was canceled."""
    status = _latest_status(db, project)
    if status == "interrupted":
        return True
    if project.item_count:
        return False
    return status is None or status in _STUCK


def _entry(db: Session, kind: str, repo_id: str) -> Optional[CurationHubEntry]:
    return db.scalar(select(CurationHubEntry).where(CurationHubEntry.kind == kind, CurationHubEntry.repo_id == repo_id)
                     .order_by(CurationHubEntry.created_at.desc()).limit(1))


def _mark(db: Session, project: CurationProject, marker: str) -> None:
    project.source_config = {**(project.source_config or {}), "starter": marker}
    db.commit()


def _add_starter_members(db: Session, project: CurationProject) -> None:
    """The deployment's starter accounts can curate the starter projects from the first sign-in: leads and
    admins as project leads, curators as curators. Only when the project is made, so a removal sticks."""
    from auth.models import User  # noqa: PLC0415
    from auth.service import starter_users  # noqa: PLC0415
    from curation import members  # noqa: PLC0415

    for username, _role, _password in starter_users():
        user = db.scalar(select(User).where(User.username == username, User.is_active == True))  # noqa: E712
        if user is None or user.role == "viewer":
            continue
        members.ensure_member(db, project.id, user.id, "lead" if user.role in {"lead", "admin"} else "curator",
                              added_by="starter data")
    db.commit()


# ── The parts ───────────────────────────────────────────────────────────────

def _regional(db: Session) -> str:
    from appdb.bootstrap import _ledger  # noqa: PLC0415

    owner = importer._starter_owner(db)  # noqa: SLF001 — same package
    project = _marked(db, REGIONAL)
    if project is None:
        # Seeded by an earlier release (no marker yet): adopt it rather than duplicate it.
        project = db.scalar(select(CurationProject).where(CurationProject.name == importer.SEED_PROJECT))
        if project is not None:
            _mark(db, project, REGIONAL)
    if project is None:
        if _ledger_has(db, _REMOVED[REGIONAL]) or _ledger_has(db, importer.SEED_ID):
            return "removed"
        _add_starter_members(db, importer.create_regional_project(db, owner))
        # Same once-per-database record the earlier seed kept, so a project removed by hand stays removed.
        db.execute(_ledger.insert().values(migration_id=importer.SEED_ID, description=f"Seeded {importer.SEED_PROJECT}",
                                           applied_at=utcnow()))
        db.commit()
        logger.info("Created %r and queued its import (~160 MB from the public bucket)", importer.SEED_PROJECT)
        return "created"
    if _needs_import(db, project):
        importer.queue_regional_import(db, project, owner)
        db.commit()
        logger.info("Queued the %r import again", project.name)
        return "requeued"
    return "ok"


def _choose_weight(files: list[str], repo_id: str) -> Optional[str]:
    if len(files) == 1:
        return files[0]
    best = [path for path in files if path.rsplit("/", 1)[-1].lower() == PREFERRED_WEIGHT]
    if len(best) == 1:
        return best[0]
    named = [path for path in files if path.rsplit("/", 1)[-1].lower().startswith(repo_id.split("/", 1)[-1].lower())]
    return named[0] if len(named) == 1 else None


def _dataset_entries(db: Session) -> list[CurationHubEntry]:
    return list(db.scalars(select(CurationHubEntry).where(
        CurationHubEntry.kind == "dataset", CurationHubEntry.repo_id == DATASET_REF)))


def _dataset(db: Session, user) -> str:
    entry = _entry(db, "dataset", DATASET_REF)
    if entry is None:
        entry, _sample = registry.register_dataset(db, user, ref=DATASET_REF, kind="dataset",
                                                   column_map=None, location=dict(DATASET_LOCATION))
        logger.info("Registered the Hugging Face dataset %s@%s", entry.repo_id, entry.revision[:8])
        state = "registered"
    else:
        state = "ok"
    if entry.status == "failed":
        registry.requeue_failed(db, user, entry)
        state = "requeued"
    project = _marked(db, HUB)
    if project is None:
        # A project someone already made from this dataset (any commit of it) is adopted, not duplicated,
        # and from then on counts as starter data: deleting it sticks.
        entry_ids = [candidate.id for candidate in _dataset_entries(db)]
        linked = next((candidate for candidate in db.scalars(select(CurationProject).order_by(CurationProject.created_at))
                       if any(registry._links(candidate, entry_id) for entry_id in entry_ids)), None)  # noqa: SLF001
        if linked is not None:
            _mark(db, linked, HUB)
            return "ok" if state == "ok" else state
        if _ledger_has(db, _REMOVED[HUB]):
            return "removed"
        created, _operation = registry.create_project_from_dataset(
            db, user, entry, name=HUB_PROJECT, description=None, dataset_mode="T3", splits=[], starter=HUB)
        _add_starter_members(db, created)
        logger.info("Created %r and queued its import from Hugging Face", HUB_PROJECT)
        return "created"
    if _needs_import(db, project):
        registry.resume_import(db, user, entry, project)
        logger.info("Queued the %r import again", project.name)
        return "requeued"
    return state


_policy_logged = False


def _model(db: Session, user) -> str:
    global _policy_logged
    entry = _entry(db, "model", MODEL_REF)
    if entry is not None:
        if entry.status == "failed":
            registry.requeue_failed(db, user, entry)
            return "requeued"
        return "ok"
    try:
        model_registry.require_model_policy()
    except HTTPException as exc:
        if not _policy_logged:
            logger.warning("Starter model skipped: %s", exc.detail)
            _policy_logged = True
        return "skipped"
    preview = registry.lookup(db, MODEL_REF, "model")
    if not preview["runnable"]:
        registry.register_model(db, user, ref=MODEL_REF, kind="model", weight_file=None, name=None, trusted=True)
        return "registered"
    weight = _choose_weight(preview["weight_files"], MODEL_REF)
    if weight is None:
        logger.warning("Starter model skipped: %s has several .pt files and none is clearly the one (%s)",
                       MODEL_REF, ", ".join(preview["weight_files"]))
        return "skipped"
    registry.register_model(db, user, ref=MODEL_REF, kind="model", weight_file=weight, name=None, trusted=True)
    logger.info("Registered the Hugging Face model %s (%s); its weights download in the background", MODEL_REF, weight)
    return "registered"


def _context(db: Session) -> str:
    """Give the Hugging Face project's patches their source images and points from the regional project
    (curation/source_context.py), once both imports are complete. Recorded on the project when done."""
    from curation import availability, source_context  # noqa: PLC0415

    hub = _marked(db, HUB)
    if hub is None:
        return "removed" if _ledger_has(db, _REMOVED[HUB]) else "waiting"
    if (hub.source_config or {}).get("context_from"):
        return "ok"
    # The regional part runs first in the same pass, so a missing regional project was removed.
    regional = _marked(db, REGIONAL)
    if regional is None:
        return "removed"
    if _latest_status(db, hub) != "completed" or _latest_status(db, regional) != "completed":
        return "waiting"
    counts = source_context.link(db, hub.id, regional.id)
    hub.source_config = {**(hub.source_config or {}), "context_from": regional.id}
    db.commit()
    availability.queue_check(db, hub.id)  # the patches now point at full images: are they in the bucket?
    logger.info("Linked %d of the %r patches to their source images in %r (%d without a match)",
                counts["linked"], hub.name, regional.name, counts["unmatched"])
    return "linked"


def _publish_count(db: Session, project: CurationProject, status: str) -> int:
    return db.scalar(select(func.count()).select_from(CurationDatasetVersion).where(
        CurationDatasetVersion.project_id == project.id, CurationDatasetVersion.status == status)) or 0


def _baseline(db: Session, user) -> str:
    """Score the starter model once on the Hugging Face project's test and val splits, so its
    confusion matrix has a baseline from the first visit. One step per pass, decided from the
    database: wait for the import and the model, publish v1 if nothing is published, start the
    run. "waiting" means a later pass has more to do; the run's id ends the part for good."""
    from curation import evaluation_runner, versions  # noqa: PLC0415 — they import this package's neighbours

    project = _marked(db, HUB)
    if project is None:
        return "removed" if _ledger_has(db, _REMOVED[HUB]) else "waiting"
    config = project.source_config if isinstance(project.source_config, dict) else {}
    if config.get("baseline_run"):
        previous = db.get(CurationEvaluationRun, config["baseline_run"])
        # A restart cut it off (not a failure, not a curator's cancel): score it again.
        if previous is None or previous.status != "interrupted":
            return "ok"
    if _latest_status(db, project) != "completed" or not project.item_count:
        return "waiting"
    entry = _entry(db, "model", MODEL_REF)
    model = db.get(CurationClassifier, entry.classifier_id) if entry is not None and entry.classifier_id else None
    if model is None:
        return "waiting"
    if model.status == "failed":
        raise HTTPException(422, f"The starter model failed validation: {model.error_message or 'no reason given'}")
    if not model_registry.worker_available():
        return "waiting"
    if model.status == "staged":
        model_registry.submit_validation(db, model)
        logger.info("Validating the starter model %s in the evaluation worker", model.name)
        return "waiting"
    if model.status != "ready":
        return "waiting"
    published = versions.latest_published(db, project.id)
    if published is None:
        if _publish_count(db, project, "building"):
            return "waiting"
        if _ledger_has(db, BASELINE_PUBLISH):
            raise HTTPException(409, "The starter baseline version did not publish; publish one from Versions")
        from appdb.bootstrap import _ledger  # noqa: PLC0415

        # Recorded first: a crash between the two never publishes twice.
        db.execute(_ledger.insert().values(migration_id=BASELINE_PUBLISH, description=f"Published {project.name} v1",
                                           applied_at=utcnow()))
        db.commit()
        versions.start_publish(db, project, user, "Starter baseline: the Hugging Face dataset as imported, "
                                                  "scored once by the T3 classifier on test and val.")
        logger.info("Publishing %r v1 for the starter baseline evaluation", project.name)
        return "waiting"
    run = db.scalar(select(CurationEvaluationRun).where(
        CurationEvaluationRun.project_id == project.id, CurationEvaluationRun.version_id == published.id,
        CurationEvaluationRun.classifier_id == model.id, CurationEvaluationRun.status != "interrupted",
    ).order_by(CurationEvaluationRun.created_at).limit(1))
    if run is None:
        device = "0" if model_registry.worker_gpus() else "cpu"
        run = evaluation_runner.start_run(db, project.id, version_number=published.version, classifier_id=model.id,
                                          splits=list(BASELINE_SPLITS), image_size=224,
                                          batch_size=128 if device != "cpu" else 32, device=device, user=user)
        logger.info("Started the starter baseline evaluation %s on %s (%s)", run.id, "/".join(BASELINE_SPLITS), device)
    project.source_config = {**config, "baseline_run": run.id}
    db.commit()
    return "started"


# ── One pass, and the thread that repeats it ──────────────────────────────────

def ensure_once(db: Session, gave_up: Optional[set] = None) -> dict:
    """Bring each wanted part up to date. Never raises.

    A part that could not be reached right now is "pending" (retry). A part the Hub or
    registry refused for good (a 4xx other than 408/429) is "attention": it is named in
    *gave_up*, logged once with the fix, and not tried again by the caller that keeps that set.
    """
    gave_up = gave_up if gave_up is not None else set()
    mode = settings.starter_data()
    result: dict = {"mode": mode, "parts": {}, "pending": False}
    if mode == "off":
        return result
    owner = importer._starter_owner(db)  # noqa: SLF001
    parts: list[tuple[str, Callable]] = []
    if mode in {"noaa-t3", "regional"}:
        parts.append(("regional", lambda: _regional(db)))
    if mode in {"noaa-t3", "hub"}:
        from auth.service import snapshot  # noqa: PLC0415

        user = snapshot(owner) if owner is not None else None

        def need_user(step):
            def run():
                if user is None:
                    raise RuntimeError("no user to act as yet")
                return step(db, user)
            return run
        parts += [("dataset", need_user(_dataset)), ("model", need_user(_model))]
        if mode == "noaa-t3":
            # Before the baseline, so the version it publishes carries the source images too.
            parts.append(("context", lambda: _context(db)))
        parts.append(("baseline", need_user(_baseline)))
    for name, run in parts:
        if name in gave_up:
            result["parts"][name] = "attention"
            continue
        if (name == "baseline" and result["parts"].get("model") in {"skipped", "attention"}
                or name in {"context", "baseline"} and result["parts"].get("dataset") == "attention"):
            result["parts"][name] = "skipped"
            continue
        try:
            result["parts"][name] = run()
            if result["parts"][name] == "waiting":
                # Imports, validation and publishing finish on their own; look again soon.
                result["pending"] = result["waiting"] = True
        except Exception as exc:  # noqa: BLE001 — network, Hub or database trouble must not stop start-up
            db.rollback()
            if isinstance(exc, HTTPException) and 400 <= exc.status_code < 500 and exc.status_code not in {408, 429}:
                logger.warning("Starter data %s needs attention and will not be retried until the app restarts: %s. %s",
                               name, exc.detail, _FIXES[name])
                gave_up.add(name)
                result["parts"][name] = "attention"
                continue
            detail = exc.detail if isinstance(exc, HTTPException) else exc
            logger.warning("Starter data %s is not ready yet: %s", name, detail)
            result["parts"][name] = "pending"
            result["pending"] = True
    return result


_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def _loop(stop: threading.Event, first_delay: float = FIRST_DELAY, max_delay: float = MAX_DELAY) -> None:
    delay = first_delay
    gave_up: set = set()
    while not stop.is_set():
        try:
            with session_scope() as db:
                outcome = ensure_once(db, gave_up)
                pending, waiting = outcome["pending"], outcome.get("waiting", False)
        except Exception:  # noqa: BLE001
            logger.exception("Starter data pass failed")
            pending, waiting = True, False
        if not pending:
            return
        stop.wait(first_delay if waiting else delay)
        delay = first_delay if waiting else min(delay * 2, max_delay)


def start_background() -> Optional[threading.Thread]:
    """Coordinator only (the caller checks). Returns the thread, or None when starter data is off."""
    global _thread
    if settings.starter_data() == "off":
        return None
    if _thread is not None and _thread.is_alive():
        return _thread
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(_stop,), name="curation-starter-data", daemon=True)
    _thread.start()
    return _thread


def stop_background(timeout: float = 5.0) -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=timeout)
