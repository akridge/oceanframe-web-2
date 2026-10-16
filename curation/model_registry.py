from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.orm import Session

from auth import config as auth_config
from curation import settings
from curation.models import CurationClassifier
from library import modelcache, settings as library_settings


def model_path(model: CurationClassifier) -> Path:
    return settings.eval_dir() / "models" / f"{model.sha256}.pt"


def summary(model: CurationClassifier) -> dict:
    return {"id": model.id, "name": model.name, "sha256": model.sha256, "size_bytes": model.size_bytes,
            "status": model.status, "class_names": model.class_names, "error_message": model.error_message,
            "created_at": model.created_at}


def require_model_policy() -> None:
    if not auth_config.load().enabled and not settings.allow_local_models():
        raise HTTPException(403, "Model registration needs authentication or CURATION_ALLOW_LOCAL_MODELS for trusted local use")


def worker_available() -> bool:
    heartbeat = settings.evaluation_spool() / "outputs" / "heartbeat.json"
    try:
        return settings.trusted_checkpoints() and time.time() - heartbeat.stat().st_mtime < 30
    except OSError:
        return False


def worker_gpus() -> int:
    """GPUs the live worker reported in its heartbeat (0 when unknown or offline)."""
    if not worker_available():
        return 0
    try:
        heartbeat = json.loads((settings.evaluation_spool() / "outputs" / "heartbeat.json").read_text(encoding="utf-8"))
        return max(0, int(heartbeat.get("gpus") or 0))
    except (OSError, ValueError, TypeError):
        return 0


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def register_stream(db: Session, stream, *, name: str, filename: str, trusted: bool, user,
                    provenance: dict | None = None) -> CurationClassifier:
    require_model_policy()
    if not trusted:
        raise HTTPException(422, "Confirm that these classifier weights come from a trusted source")
    if Path(filename.replace("\\", "/")).suffix.lower() != ".pt":
        raise HTTPException(422, "Only trusted Ultralytics .pt classification checkpoints are supported")
    name = name.strip()
    if not name or len(name) > 200:
        raise HTTPException(422, "Model name must be 1 to 200 characters")
    root = settings.eval_dir() / "models"
    root.mkdir(parents=True, exist_ok=True)
    digest, size = hashlib.sha256(), 0
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=root, suffix=".partial", delete=False) as target:
            temporary = Path(target.name)
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > settings.model_upload_limit():
                    raise HTTPException(413, "Classifier exceeds CURATION_MODEL_MAX_BYTES")
                digest.update(chunk)
                target.write(chunk)
        if not size:
            raise HTTPException(422, "Classifier checkpoint is empty")
        model = CurationClassifier(name=name, sha256=digest.hexdigest(), size_bytes=size,
                                  created_by_user_id=user.id, provenance={**(provenance or {}), "trusted": True})
        destination = model_path(model)
        if not destination.exists():
            temporary.replace(destination)
        else:
            with destination.open("rb") as existing:
                if hashlib.file_digest(existing, "sha256").hexdigest() != model.sha256:
                    raise HTTPException(409, "Registered model storage failed its integrity check")
        db.add(model)
        db.commit()
        db.refresh(model)
        return model
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def register_reference(db: Session, ref: str, *, name: str, trusted: bool, user) -> CurationClassifier:
    require_model_policy()
    if not modelcache.is_allowed(ref):
        raise HTTPException(422, "Choose a model from the configured model catalog")
    if ref.startswith("gs://"):
        from library.storage.gcs import GCSBackend, parse_gs_uri

        bucket, key = parse_gs_uri(ref)
        backend = GCSBackend(f"gs://{bucket}")
        backend._ensure_client()
        blob = backend._bucket.blob(key)
        blob.reload()
        if not blob.size or blob.size > settings.model_upload_limit():
            raise HTTPException(413, "Configured classifier is empty or exceeds the upload limit")
        generation = blob.generation
        with blob.open("rb", if_generation_match=generation) as stream:
            return register_stream(db, stream, name=name, filename=key, trusted=trusted, user=user,
                                   provenance={"reference": ref, "generation": str(generation)})
    path = Path(ref)
    if not path.is_absolute():
        path = library_settings.MODEL_DIR / path
    if not path.is_file():
        raise HTTPException(404, "Supply the configured weights first; registration never auto-downloads a model")
    with path.open("rb") as stream:
        return register_stream(db, stream, name=name, filename=path.name, trusted=trusted, user=user,
                               provenance={"reference": ref})


def submit_validation(db: Session, model: CurationClassifier) -> None:
    require_model_policy()
    if not worker_available():
        raise HTTPException(503, "The isolated evaluation worker is unavailable or trusted-checkpoint execution is disabled")
    if model.status == "validating":
        raise HTTPException(409, "Classifier validation is already running")
    if model.status == "ready":
        return
    task_id = f"model-{model.id}"
    folder = settings.evaluation_spool() / "inputs" / task_id
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(model_path(model), folder / "model.pt")
    shutil.rmtree(settings.evaluation_spool() / "outputs" / task_id, ignore_errors=True)
    model.status = "validating"
    model.error_message = None
    db.commit()
    write_json(folder / "task.json", {"id": task_id, "action": "validate", "model_sha256": model.sha256})