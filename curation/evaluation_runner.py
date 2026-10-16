from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import tempfile
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from appdb.base import utcnow
from appdb.engine import session_scope
from curation import evaluation_runs, image_cache, images, model_registry, service, settings, storage
from curation.models import CurationClassifier, CurationDatasetVersion, CurationEvaluationPrediction, CurationEvaluationRun, CurationVersionInput
from curation.evaluation_worker import EVALUATOR_VERSION

logger = logging.getLogger(__name__)


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def start_run(db: Session, project_id: str, *, version_number: int, classifier_id: str, splits: list[str],
              image_size: int, batch_size: int, device: str, user) -> CurationEvaluationRun:
    project = service.get_project_or_404(db, project_id)
    if not model_registry.worker_available():
        raise HTTPException(503, "Start the isolated evaluation worker and enable trusted-checkpoint execution first")
    version = db.scalar(select(CurationDatasetVersion).where(CurationDatasetVersion.project_id == project_id,
                                                           CurationDatasetVersion.version == version_number))
    if version is None or version.status != "published":
        raise HTTPException(409, "Choose a published dataset version")
    model = db.get(CurationClassifier, classifier_id)
    if model is None or model.status != "ready":
        raise HTTPException(409, "Choose a validated classification model")
    counts = dict(db.execute(select(CurationVersionInput.split, func.count())
                            .where(CurationVersionInput.version_id == version.id)
                            .group_by(CurationVersionInput.split)).all())
    if sum(counts.values()) != version.item_count:
        raise HTTPException(409, "This legacy version has no frozen input snapshot; publish a new version")
    selected = [split for split in evaluation_runs.SPLITS if split in splits and counts.get(split)]
    if not selected:
        raise HTTPException(422, "Selected splits contain no images")
    labels = set(db.scalars(select(CurationVersionInput.label).distinct().where(
        CurationVersionInput.version_id == version.id, CurationVersionInput.split.in_(selected))))
    missing = labels - set(model.class_names)
    if missing:
        raise HTTPException(422, f"Classifier does not contain dataset labels: {', '.join(sorted(missing)[:20])}")
    run = CurationEvaluationRun(project_id=project_id, version_id=version.id, classifier_id=model.id,
                               created_by_user_id=user.id, expected_count=sum(counts[split] for split in selected),
                               specification={"name": f"{model.name} / v{version.version}", "version": version.version,
                                              "model_name": model.name, "model_sha256": model.sha256,
                                              "class_names": model.class_names, "dataset_mode": project.dataset_mode,
                                              "splits": selected, "split_counts": {split: counts[split] for split in selected},
                                              "image_size": image_size, "batch_size": batch_size, "device": device,
                                              "evaluator_version": EVALUATOR_VERSION,
                                              "cross_split_source_images": version.statistics.get("cross_split_source_images", 0),
                                              "test_informed": version.statistics.get("test_informed", False)})
    db.add(run)
    db.commit()
    db.refresh(run)
    evaluation_pool.wake()
    return run


CHUNK = 256


def _png_rgb(payload: bytes) -> bytes:
    """The patch as an RGB PNG; already-RGB PNGs (the HF patches) pass through untouched."""
    with Image.open(BytesIO(payload)) as image:
        if image.format == "PNG" and image.mode == "RGB":
            return payload
        buffer = BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        return buffer.getvalue()


def _cache_path(uri: str) -> Path | None:
    root = settings.patch_cache_dir()
    if root is None:
        return None
    digest = hashlib.sha256(uri.encode("utf-8")).hexdigest()
    return root / digest[:2] / f"{digest}.png"


def _link_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    try:
        os.link(source, target)
    except OSError:  # another filesystem, or links not allowed
        shutil.copyfile(source, target)


def _store_in_cache(cached: Path, payload: bytes) -> Path | None:
    """Write the patch into the cache atomically; None when the cache cannot take it."""
    cached.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=cached.parent, suffix=".partial")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        os.replace(name, cached)
        return cached
    except PermissionError:  # Windows: the cache file is open elsewhere; the bytes are still good
        Path(name).unlink(missing_ok=True)
        return None
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def load_patch(item, project, buckets: set[str]) -> tuple[bytes, Path | None] | str:
    """(RGB PNG bytes, cache file) for one frozen item, or why no copy could be used:
    "not_found" or "undecodable".

    Runs on worker threads: no database session here, only the bucket set
    fetched once by stage_run. The cache is keyed by URI (a patch cut from
    its source also by its geometry) and never revalidated: delete it if
    objects at the same URIs are replaced.

    A moved, resized or hand-added point has no stored crop that matches it:
    it is cut from its source image and never falls back to crop_uri.
    """
    if images.needs_fresh_crop(item) and item.row_px is not None and item.column_px is not None:
        return _load_fresh_patch(item, buckets)
    reason = "not_found"
    for uri in images.crop_candidates(item, project):
        if not images.is_allowed_in(buckets, uri):
            continue
        cached = _cache_path(uri)
        if cached is not None and cached.is_file():
            return cached.read_bytes(), cached
        try:
            payload = _png_rgb(storage.read_bytes(uri))
        except FileNotFoundError:
            continue
        except (UnidentifiedImageError, OSError):
            reason = "undecodable"
            continue
        if cached is not None:
            cached = _store_in_cache(cached, payload)
        return payload, cached
    if item.row_px is not None and item.column_px is not None and images.is_allowed_in(buckets, item.source_image_uri):
        try:
            return _png_rgb(images.crop_from_source(item)), None
        except FileNotFoundError:
            return reason
        except (UnidentifiedImageError, OSError):
            return "undecodable"
    return reason


def _load_fresh_patch(item, buckets: set[str]) -> tuple[bytes, Path | None] | str:
    """The patch cut from the source image at the item's frozen geometry, or why not."""
    uri = item.source_image_uri
    if not images.is_allowed_in(buckets, uri):
        return "not_found"
    cached = _cache_path(f"fresh:{uri}:{item.row_px}:{item.column_px}:{item.crop_size}")
    if cached is not None and cached.is_file():
        return cached.read_bytes(), cached
    try:
        payload = _png_rgb(image_cache.patch(uri, item.row_px, item.column_px, item.crop_size))
    except FileNotFoundError:
        return "not_found"
    except (UnidentifiedImageError, OSError):
        return "undecodable"
    if cached is not None:
        cached = _store_in_cache(cached, payload)
    return payload, cached


def discard_inputs(run_id: str) -> None:
    """Remove the run's spool inputs once the worker is done with them.

    Deployed, the spool is a separate mount, so these are full copies, not
    links. The worker mounts inputs read-only: the app cleans up.
    """
    shutil.rmtree(settings.evaluation_spool() / "inputs" / run_id, ignore_errors=True)


def _check_missing(run: CurationEvaluationRun, missing: list[dict], requested: int) -> None:
    """Write missing.json, then fail when more than the allowed share of patches is gone."""
    (evaluation_runs.directory(run.id) / "missing.json").write_text(json.dumps(missing), encoding="utf-8")
    if missing and len(missing) > settings.evaluation_max_missing() * requested:
        examples = ", ".join(entry["crop_uri"] for entry in missing[:3])
        raise ValueError(f"{len(missing):,} of {requested:,} patches are missing (more than "
                         f"{settings.evaluation_max_missing():.0%} allowed by CURATION_EVAL_MAX_MISSING); first: {examples}")


def stage_run(db: Session, run: CurationEvaluationRun) -> None:
    run.status = "staging"
    db.commit()
    model = db.get(CurationClassifier, run.classifier_id)
    source = model_registry.model_path(model)
    if file_hash(source) != run.specification["model_sha256"]:
        raise ValueError("Registered model has changed on disk")
    folder = settings.evaluation_spool() / "inputs" / run.id
    (folder / "images").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, folder / "model.pt")
    frozen = db.scalars(select(CurationVersionInput).where(
        CurationVersionInput.version_id == run.version_id,
        CurationVersionInput.split.in_(run.specification["splits"])).order_by(CurationVersionInput.item_id)).all()
    project = SimpleNamespace(dataset_mode=run.specification["dataset_mode"])
    buckets = images.allowed_buckets(db)
    private_root = evaluation_runs.directory(run.id) / "images"
    private_root.mkdir(parents=True, exist_ok=True)
    manifest, missing = [], []
    requested = len(frozen)
    if requested != run.expected_count:
        raise ValueError("Frozen input count does not match the requested evaluation")
    with ThreadPoolExecutor(max_workers=settings.evaluation_fetch_workers(),
                            thread_name_prefix="eval-fetch") as pool:
        for start in range(0, len(frozen), CHUNK):
            db.refresh(run)
            if run.cancel_requested:
                run.status = "cancelled"
                run.completed_at = utcnow()
                db.commit()
                discard_inputs(run.id)
                return
            chunk = frozen[start:start + CHUNK]
            # map keeps input order, so the manifest stays in item_id order.
            for item, loaded in zip(chunk, pool.map(lambda frozen_item: load_patch(frozen_item, project, buckets), chunk)):
                if isinstance(loaded, str):
                    missing.append({"item_id": item.item_id, "split": item.split,
                                    "crop_uri": item.crop_uri or item.source_image_uri, "reason": loaded})
                    continue
                payload, cached = loaded
                private = private_root / f"{item.item_id}.png"
                if cached is not None:
                    _link_or_copy(cached, private)
                else:
                    private.write_bytes(payload)
                _link_or_copy(private, folder / "images" / f"{item.item_id}.png")
                manifest.append({"item_id": item.item_id, "source_key": item.source_key, "label": item.label,
                                 "split": item.split, "file_name": item.file_name,
                                 "image_sha256": hashlib.sha256(payload).hexdigest()})
            # How far the fetch has got, for the matrix's progress card (replaced when staging ends).
            run.provenance = {**(run.provenance or {}), "staged_count": start + len(chunk)}
            db.commit()
            # Fail fast: a wrong root should not cost one request per patch.
            if len(missing) > settings.evaluation_max_missing() * requested:
                _check_missing(run, missing, requested)
    _check_missing(run, missing, requested)
    if not manifest:
        raise ValueError("No patches could be read for this evaluation")
    staged = Counter(entry["split"] for entry in manifest)
    run.specification = {**run.specification, "requested_split_counts": run.specification["split_counts"],
                         "split_counts": {split: staged[split] for split in run.specification["splits"] if staged[split]}}
    run.expected_count = len(manifest)
    unscored = {"count": len(missing), "by_split": dict(Counter(entry["split"] for entry in missing)),
                "examples": missing[:20]}
    manifest_path = folder / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, separators=(",", ":")), encoding="utf-8")
    private_manifest = evaluation_runs.directory(run.id) / "manifest.json"
    shutil.copyfile(manifest_path, private_manifest)
    digest = file_hash(private_manifest)
    run.provenance = {"manifest_sha256": digest, "materialized_at": utcnow().isoformat() + "Z",
                      "dataset_mode": run.specification["dataset_mode"], "image_size": run.specification["image_size"],
                      "test_informed": run.specification.get("test_informed", False),
                      "cross_split_source_images": run.specification.get("cross_split_source_images", 0),
                      "unscored": unscored}
    run.status = "running"
    db.commit()
    model_registry.write_json(folder / "task.json", {"id": run.id, "action": "evaluate",
        "model_sha256": run.specification["model_sha256"], "manifest_sha256": digest,
        **{key: run.specification[key] for key in ("class_names", "image_size", "batch_size", "device")}})


def ingest_result(db: Session, run: CurationEvaluationRun, output: Path, result: dict) -> None:
    if run.status != "running":
        raise ValueError("Only a running evaluation may publish results; completed evidence is immutable")
    if result.get("model_sha256") != run.specification["model_sha256"] or \
            result.get("manifest_sha256") != run.provenance["manifest_sha256"]:
        raise ValueError("Worker result does not match the submitted model and dataset")
    path = output / "predictions.jsonl"
    if file_hash(path) != result.get("predictions_sha256"):
        raise ValueError("Prediction output failed its integrity check")
    manifest_path = evaluation_runs.directory(run.id) / "manifest.json"
    if file_hash(manifest_path) != run.provenance["manifest_sha256"]:
        raise ValueError("Saved evaluation manifest changed")
    expected = {entry["item_id"]: entry for entry in json.loads(manifest_path.read_text(encoding="utf-8"))}
    seen = set()
    labels = set(run.specification["class_names"])
    split_counts = Counter()
    records = []
    db.execute(delete(CurationEvaluationPrediction).where(CurationEvaluationPrediction.run_id == run.id))
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            predicted = json.loads(line)
            item_id = predicted["item_id"]
            if item_id not in expected or item_id in seen:
                raise ValueError("Worker emitted an unexpected or duplicate image")
            seen.add(item_id)
            entry = expected[item_id]
            ranked = predicted["top_predictions"]
            if not ranked or len(ranked) > min(5, len(labels)):
                raise ValueError("Worker emitted invalid top-k predictions")
            if len({value["label"] for value in ranked}) != len(ranked):
                raise ValueError("Worker emitted duplicate top-k labels")
            previous = 1.0
            for rank, value in enumerate(ranked, start=1):
                confidence = float(value["confidence"])
                if value["label"] not in labels or not math.isfinite(confidence) or not 0 <= confidence <= previous or value["rank"] != rank:
                    raise ValueError("Worker emitted invalid class probabilities")
                previous = confidence
            split_counts[entry["split"]] += 1
            records.append(CurationEvaluationPrediction(run_id=run.id, item_id=item_id, source_key=entry["source_key"],
                split=entry["split"], actual_label=entry["label"], predicted_label=ranked[0]["label"],
                confidence=ranked[0]["confidence"], top_predictions=ranked,
                image_sha256=entry["image_sha256"], file_name=entry["file_name"]))
            if len(records) >= 500:
                db.add_all(records)
                db.flush()
                records.clear()
    if len(seen) != run.expected_count or dict(split_counts) != run.specification["split_counts"]:
        raise ValueError("Worker results are incomplete; no completed matrix will be published")
    summaries = result["results"]
    for split, count in {**dict(split_counts), "all": run.expected_count}.items():
        if summaries.get(split, {}).get("images") != count:
            raise ValueError("Worker metric counts do not match predictions")
    db.add_all(records)
    run.results = summaries
    run.provenance = {**run.provenance, **result.get("runtime", {}), "predictions_sha256": result["predictions_sha256"]}
    run.scored_count = len(seen)
    run.status = "completed"
    run.completed_at = utcnow()
    db.commit()
    discard_inputs(run.id)


def sync_outputs(db: Session) -> None:
    for model in db.scalars(select(CurationClassifier).where(CurationClassifier.status == "validating")).all():
        path = settings.evaluation_spool() / "outputs" / f"model-{model.id}" / "result.json"
        if not path.is_file():
            continue
        result = json.loads(path.read_text(encoding="utf-8"))
        names = result.get("class_names", [])
        if result.get("status") == "completed" and result.get("model_sha256") == model.sha256 and names and \
            all(isinstance(name, str) and 0 < len(name) <= 100 for name in names) and len(names) == len(set(names)):
            model.status = "ready"
            model.class_names = names
            model.provenance = {**model.provenance, **result.get("runtime", {})}
        else:
            model.status = "failed"
            model.error_message = result.get("error", "Classifier validation failed")[:2000]
        db.commit()
    for run in db.scalars(select(CurationEvaluationRun).where(CurationEvaluationRun.status == "running")).all():
        folder = settings.evaluation_spool() / "inputs" / run.id
        if not (folder / "task.json").is_file():
            run.status = "interrupted"
            run.error_message = "The server stopped before submitting the worker task; create a new evaluation"
            run.completed_at = utcnow()
            db.commit()
            discard_inputs(run.id)
            continue
        if run.cancel_requested:
            (folder / "cancel").touch()
        output = settings.evaluation_spool() / "outputs" / run.id
        if not (output / "result.json").is_file():
            progress = output / "progress.json"
            if progress.is_file():
                run.scored_count = min(run.expected_count, int(json.loads(progress.read_text())["scored_count"]))
                db.commit()
            continue
        try:
            result = json.loads((output / "result.json").read_text(encoding="utf-8"))
            if run.cancel_requested:
                run.status = "cancelled"
            elif result.get("status") == "completed":
                ingest_result(db, run, output, result)
                continue
            else:
                run.status = result["status"] if result.get("status") in {"cancelled", "interrupted"} else "failed"
                run.error_message = result.get("error", "Evaluation did not complete")[:2000]
            run.completed_at = utcnow()
            db.commit()
            discard_inputs(run.id)
        except Exception as exc:
            db.rollback()
            run = db.get(CurationEvaluationRun, run.id)
            run.status = "failed"
            run.error_message = str(exc)[:2000]
            run.completed_at = utcnow()
            db.commit()
            discard_inputs(run.id)


class EvaluationPool:
    def __init__(self):
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None

    def wake(self):
        self._wake.set()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        with session_scope() as db:
            recover_staging(db)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="curation-evaluation")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _loop(self):
        while not self._stop.is_set():
            try:
                with session_scope() as db:
                    sync_outputs(db)
                    run = db.scalar(select(CurationEvaluationRun).where(CurationEvaluationRun.status == "queued")
                                    .order_by(CurationEvaluationRun.created_at).limit(1))
                    if run and run.cancel_requested:
                        run.status = "cancelled"
                        run.completed_at = utcnow()
                    elif run and model_registry.worker_available():
                        try:
                            stage_run(db, run)
                        except Exception as exc:
                            db.rollback()
                            run = db.get(CurationEvaluationRun, run.id)
                            run.status = "failed"
                            run.error_message = str(exc)[:2000]
                            run.completed_at = utcnow()
                            discard_inputs(run.id)
            except Exception:
                logger.exception("Evaluation coordinator failed")
            self._wake.wait(1)
            self._wake.clear()


evaluation_pool = EvaluationPool()


def recover_staging(db: Session) -> None:
    for run in db.scalars(select(CurationEvaluationRun).where(CurationEvaluationRun.status == "staging")):
        run.status = "interrupted"
        run.error_message = "Server restarted while staging inputs; create a new evaluation"
        run.completed_at = utcnow()