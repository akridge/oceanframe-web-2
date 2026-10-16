"""
Engine and sessions for the application database.

``OCEANFRAME_DB_URL`` picks the backend:

* ``oracle+oracledb://user:pass@host:1521/?service_name=FREEPDB1`` — the stack.
  python-oracledb runs in thin mode, so no Oracle client libraries are needed
  in the image.
* ``sqlite:///path/app.sqlite`` — local development and tests. Unset means
  ``$LIB_DATA_DIR/app.sqlite``.

The engine is created on first use and read from the environment at that
moment, so tests can point it somewhere else and call :func:`reset_engine`.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

_engine: Engine | None = None
_session_factory: sessionmaker | None = None
_lock = threading.Lock()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def db_url() -> str | URL:
    url = os.getenv("OCEANFRAME_DB_URL", "").strip()
    if url:
        return url
    # The stack passes the parts separately: a generated password may contain
    # characters that would break a hand-assembled URL, and URL.create escapes.
    user = os.getenv("OCEANFRAME_DB_USER", "").strip()
    dsn = os.getenv("OCEANFRAME_DB_DSN", "").strip()  # host:port/service
    if user and dsn:
        host_port, _, service = dsn.partition("/")
        host, _, port = host_port.partition(":")
        return URL.create(
            "oracle+oracledb", username=user, password=os.getenv("OCEANFRAME_DB_PASSWORD", ""),
            host=host, port=int(port or 1521), query={"service_name": service or "FREEPDB1"},
        )
    data_dir = Path(os.getenv("LIB_DATA_DIR", str(Path(__file__).resolve().parents[1] / "library_data")))
    data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(data_dir / 'app.sqlite').as_posix()}"


def _url() -> URL:
    url = db_url()
    return url if isinstance(url, URL) else make_url(url)


def backend_name() -> str:
    """'oracle' or 'sqlite' (or another SQLAlchemy backend), from configuration alone.

    Never connects: /healthz and the page header call it, and neither may wait
    on the database.
    """
    return _url().get_backend_name()


def describe() -> str:
    """The backend and where it is, for logs and the UI. Never includes a password."""
    url = _url()
    if url.get_backend_name() == "sqlite":
        return f"sqlite ({url.database})"
    service = url.query.get("service_name") or url.database or ""
    return f"{url.get_backend_name()} ({url.host}:{url.port or 1521}/{service})"


def _sqlite_pragmas(dbapi_connection, _record) -> None:
    # Same settings as the library catalog (library/db.py _configure). Foreign
    # keys are off by default in SQLite, which is how OASIS ended up with
    # orphaned items after deleting a project.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=15000")
    cursor.close()


def _oracle_session_utc(dbapi_connection, _record) -> None:
    # CURRENT_TIMESTAMP follows the session time zone, which python-oracledb
    # takes from the client. Pin it so stored times are UTC wherever the app
    # runs (the same reason CAT does this in cat/db/oracle.py).
    cursor = dbapi_connection.cursor()
    cursor.execute("ALTER SESSION SET TIME_ZONE = '+00:00'")
    cursor.close()


def _build(url: str | URL) -> Engine:
    if str(url).startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 15})
        event.listen(engine, "connect", _sqlite_pragmas)
        return engine

    pool_max = max(2, _env_int("OCEANFRAME_DB_POOL_MAX", 12))
    engine = create_engine(
        url,
        pool_size=pool_max,
        max_overflow=0,
        # Bounded wait for a free connection: a stuck request fails with a
        # clear error instead of silently holding a worker thread.
        pool_timeout=max(1, _env_int("OCEANFRAME_DB_POOL_WAIT_S", 15)),
        # A connection that sat idle may have been dropped by a firewall or a
        # database restart; check before handing it out.
        pool_pre_ping=True,
        pool_recycle=1800,
    )
    if engine.dialect.name == "oracle":
        event.listen(engine, "connect", _oracle_session_utc)
    return engine


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = _build(db_url())
                _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def dialect_name() -> str:
    return get_engine().dialect.name


def reset_engine() -> None:
    """Drop the engine so the next use rebuilds it from the environment."""
    global _engine, _session_factory
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = None
        _session_factory = None


def new_session() -> Session:
    get_engine()
    assert _session_factory is not None
    return _session_factory()


@contextmanager
def session_scope() -> Iterator[Session]:
    """A session that commits on success and rolls back on error."""
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency. Handlers commit explicitly; anything left is rolled back."""
    session = new_session()
    try:
        yield session
    finally:
        session.close()
