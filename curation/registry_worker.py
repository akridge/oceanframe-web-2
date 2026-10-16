"""
Background registration of Hugging Face models: fetch the weights, then register them.

``POST /hub/models`` checks the request and queues an entry; the slow part
(downloading a checkpoint that may be hundreds of MB) happens here, on the one
coordinator per database (``OCEANFRAME_BACKGROUND_WORKERS``). A second app on
the same database, like the T3 review, can queue registrations; this process
runs them.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select, update

from appdb.engine import session_scope
from auth.models import User
from auth.service import snapshot
from curation import huggingface, model_registry
from curation.models import CurationClassifier, CurationHubEntry

logger = logging.getLogger(__name__)
POLL_SECONDS = 5.0


def claim_next() -> Optional[int]:
    with session_scope() as db:
        entry_id = db.scalar(select(CurationHubEntry.id).where(
            CurationHubEntry.kind == "model", CurationHubEntry.status == "queued",
        ).order_by(CurationHubEntry.id).limit(1))
        if entry_id is None:
            return None
        # Conditional update: if another worker got there first, rowcount is 0.
        claimed = db.execute(update(CurationHubEntry).where(
            CurationHubEntry.id == entry_id, CurationHubEntry.status == "queued",
        ).values(status="downloading", error_message=None)).rowcount
        return entry_id if claimed else None


def _message(exc: Exception) -> str:
    text = exc.detail if isinstance(exc, HTTPException) else str(exc)
    return (str(text).strip() or type(exc).__name__)[:2000]


def process(entry_id: int) -> None:
    """Download and register one claimed entry. A failure is kept on the entry, never raised."""
    try:
        with session_scope() as db:
            entry = db.get(CurationHubEntry, entry_id)
            owner = db.get(User, entry.created_by_user_id) if entry.created_by_user_id else None
            if owner is None:
                raise RuntimeError("The person who registered this model no longer has an account; register it again")
            weight_file = entry.options["weight_file"]
            local = huggingface.download("model", entry.repo_id, entry.revision, weight_file)
            with local.open("rb") as stream:
                classifier = model_registry.register_stream(
                    db, stream, name=entry.options.get("name") or entry.repo_id.split("/", 1)[1],
                    filename=weight_file, trusted=True, user=snapshot(owner),
                    provenance={"source": "huggingface", "repo_id": entry.repo_id, "revision": entry.revision,
                                "file": weight_file, "license": (entry.card or {}).get("license")})
            entry.classifier_id = classifier.id
            entry.status = "ready"
            entry.error_message = None
            classifier_id = classifier.id
    except Exception as exc:  # noqa: BLE001 — the entry carries the reason
        logger.warning("Registering Hugging Face model entry %s failed", entry_id, exc_info=True)
        try:
            with session_scope() as db:
                db.execute(update(CurationHubEntry).where(CurationHubEntry.id == entry_id)
                           .values(status="failed", error_message=_message(exc)))
        except Exception:  # noqa: BLE001 — recover() re-queues it at the next start
            logger.exception("Could not record the failure of model entry %s", entry_id)
        return
    _validate(classifier_id)


def _validate(classifier_id: str) -> None:
    """Start validation in the isolated worker when it is online, as the dialog used to."""
    try:
        with session_scope() as db:
            model = db.get(CurationClassifier, classifier_id)
            if model is not None and model.status in {"staged", "failed"} and model_registry.worker_available():
                model_registry.submit_validation(db, model)
    except Exception:  # noqa: BLE001 — the model list still offers Validate
        logger.warning("Validation of classifier %s did not start", classifier_id, exc_info=True)


def recover() -> int:
    """A download that was running when the coordinator stopped starts again."""
    with session_scope() as db:
        return db.execute(update(CurationHubEntry).where(
            CurationHubEntry.kind == "model", CurationHubEntry.status == "downloading",
        ).values(status="queued")).rowcount


def run_pending() -> int:
    """Process every queued entry now, in this thread (tests, and a manual nudge)."""
    done = 0
    while (entry_id := claim_next()) is not None:
        process(entry_id)
        done += 1
    return done


class RegistryWorker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        try:
            recovered = recover()
            if recovered:
                logger.info("Re-queued %d interrupted model registration(s)", recovered)
        except Exception:  # noqa: BLE001 — database briefly unavailable; the loop retries
            logger.exception("Could not recover interrupted model registrations")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="curation-registry", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                entry_id = claim_next()
            except Exception:  # noqa: BLE001
                logger.exception("Could not poll for model registrations")
                entry_id = None
            if entry_id is None:
                self._stop.wait(POLL_SECONDS)
                continue
            self._run_one(entry_id)

    def _run_one(self, entry_id: int) -> None:
        try:
            process(entry_id)
        except Exception:  # noqa: BLE001 — the loop must keep polling
            logger.exception("Model registration %s crashed", entry_id)


registry_worker = RegistryWorker()
