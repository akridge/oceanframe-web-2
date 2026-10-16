"""
Background jobs with SSE progress.

Indexing a dense bucket takes minutes to hours, so every long operation runs as
a job: it is cancellable, it survives the request that started it, and its
progress is both streamable (SSE, for the open tab) and pollable (for a tab that
was closed and reopened).

State lives in memory for the stream and is mirrored into the ``jobs`` table so
a restart can tell you what was interrupted.
"""
from __future__ import annotations

import queue
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Callable, Iterator

from library import db

MAX_KEPT_JOBS = 50


@dataclass
class Job:
    id:      str
    kind:    str
    params:  dict
    status:  str = "running"          # running | done | error | cancelled
    total:   int = 0
    done:    int = 0
    message: str = ""
    result:  dict = field(default_factory=dict)
    started_at:  float = field(default_factory=time.time)
    finished_at: float = 0.0
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _subscribers: list[queue.Queue] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ── control ───────────────────────────────────────────────────────────────

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()

    # ── progress ──────────────────────────────────────────────────────────────

    def set_total(self, total: int) -> None:
        self.total = total
        self.publish({"type": "total", "total": total})

    def advance(self, n: int = 1, message: str = "") -> None:
        self.done += n
        if message:
            self.message = message
        self.publish({
            "type": "progress",
            "done": self.done,
            "total": self.total,
            "message": self.message,
            "frac": round(self.done / self.total, 4) if self.total else 0.0,
        })

    def log(self, message: str) -> None:
        self.message = message
        self.publish({"type": "log", "message": message})

    def publish(self, event: dict) -> None:
        event.setdefault("job_id", self.id)
        with self._lock:
            subscribers = list(self._subscribers)
        for sub in subscribers:
            try:
                sub.put_nowait(event)
            except queue.Full:
                # A stalled reader must never block the worker; it will resync
                # from the terminal snapshot it gets on reconnect.
                pass

    def subscribe(self) -> queue.Queue:
        sub: queue.Queue = queue.Queue(maxsize=2048)
        with self._lock:
            self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: queue.Queue) -> None:
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "total": self.total,
            "done": self.done,
            "message": self.message,
            "result": self.result,
            "params": self.params,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
        }


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _persist(job: Job) -> None:
    try:
        import json  # noqa: PLC0415

        with db.write() as conn:
            conn.execute(
                "INSERT INTO jobs(id, kind, status, total, done, message, params_json, started_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status=excluded.status, total=excluded.total, "
                "done=excluded.done, message=excluded.message, finished_at=excluded.finished_at",
                (job.id, job.kind, job.status, job.total, job.done, job.message[:500],
                 json.dumps(job.params)[:4000], job.started_at, job.finished_at),
            )
    except Exception:
        pass   # progress bookkeeping must never take down the work itself


def start_job(kind: str, params: dict, target: Callable[[Job], dict]) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], kind=kind, params=params)
    with _jobs_lock:
        _jobs[job.id] = job
        _prune()
    _persist(job)

    def runner() -> None:
        try:
            job.result = target(job) or {}
            job.status = "cancelled" if job.cancelled else "done"
        except Exception as exc:
            job.status = "error"
            job.message = f"{type(exc).__name__}: {exc}"
            job.result = {"traceback": traceback.format_exc()[-4000:]}
        finally:
            job.finished_at = time.time()
            _persist(job)
            job.publish({"type": job.status, **job.to_dict()})

    threading.Thread(target=runner, name=f"job-{kind}-{job.id}", daemon=True).start()
    return job


def _prune() -> None:
    """Keep the newest MAX_KEPT_JOBS finished jobs in memory; the table keeps the rest."""
    finished = sorted(
        (j for j in _jobs.values() if j.status != "running"), key=lambda j: j.finished_at
    )
    for job in finished[: max(0, len(finished) - MAX_KEPT_JOBS)]:
        _jobs.pop(job.id, None)


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def list_jobs(limit: int = 20) -> list[dict]:
    with _jobs_lock:
        jobs = sorted(_jobs.values(), key=lambda j: j.started_at, reverse=True)
    return [j.to_dict() for j in jobs[:limit]]


def active_job(kind: str) -> Job | None:
    for job in _jobs.values():
        if job.kind == kind and job.status == "running":
            return job
    return None


def stream(job: Job, heartbeat: float = 15.0) -> Iterator[str]:
    """SSE lines for one job, replaying its current state first."""
    sub = job.subscribe()
    try:
        yield _sse({"type": "snapshot", **job.to_dict()})
        while True:
            try:
                event = sub.get(timeout=heartbeat)
            except queue.Empty:
                if job.status != "running":
                    return
                yield ": keepalive\n\n"
                continue
            yield _sse(event)
            if event.get("type") in {"done", "error", "cancelled"}:
                return
    finally:
        job.unsubscribe(sub)


def _sse(payload: dict) -> str:
    import json  # noqa: PLC0415

    return f"data: {json.dumps(payload)}\n\n"


def mark_interrupted() -> None:
    """On startup, any job still marked running in the table died with the process."""
    try:
        with db.write() as conn:
            conn.execute(
                "UPDATE jobs SET status='error', message='interrupted by restart', finished_at=? "
                "WHERE status='running'",
                (time.time(),),
            )
    except Exception:
        pass
