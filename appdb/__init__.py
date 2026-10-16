"""
The application database: users, login sessions, curation and its audit trail.

This is the *authoritative* store — unlike the library catalog
(``library/db.py``), which is derived from the images and can be re-crawled, the
rows here are people's work. It runs on Oracle Free in the deployed stack and on
SQLite for local development and the unit tests, through one SQLAlchemy layer.

There is deliberately no automatic fallback between the two: if
``OCEANFRAME_DB_URL`` names Oracle and Oracle is unreachable, start-up fails
rather than quietly writing everyone's work to a local file nobody backs up.
"""
from appdb.base import Base, JSONText, Str, utcnow
from appdb.engine import db_url, dialect_name, get_engine, get_db, reset_engine, session_scope

__all__ = [
    "Base", "JSONText", "Str", "utcnow",
    "db_url", "dialect_name", "get_engine", "get_db", "reset_engine", "session_scope",
]
