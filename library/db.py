"""
SQLite catalog: schema, migrations, and connection helpers.

One file, WAL mode, no server.  The catalog is *derived* state — it can always
be rebuilt from the bucket — so it is safe to delete and re-crawl.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from library import settings

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id        INTEGER PRIMARY KEY,
    kind      TEXT NOT NULL,              -- 'gcs' | 'local'
    root      TEXT NOT NULL UNIQUE,       -- 'gs://bucket/prefix' | '/abs/path'
    label     TEXT NOT NULL DEFAULT '',
    -- Regex whose named groups become key:value tags for this source.  Per
    -- source, not global: one bucket routinely holds collections with quite
    -- different path conventions.
    tag_pattern TEXT NOT NULL DEFAULT '',
    -- Detection model this collection is scanned with.  Per collection because
    -- a coral photogrammetry set and a fish-camera set want different models.
    model_ref   TEXT NOT NULL DEFAULT '',
    added_at  REAL NOT NULL,
    scanned_at REAL NOT NULL DEFAULT 0,
    -- User-entered collection metadata: not derived from the bucket, so a
    -- catalog rebuild loses it.
    year_start  INTEGER NOT NULL DEFAULT 0,
    year_end    INTEGER NOT NULL DEFAULT 0,
    survey      TEXT NOT NULL DEFAULT '',
    region      TEXT NOT NULL DEFAULT '',
    contact     TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    extras_json TEXT NOT NULL DEFAULT '{}',
    inport_url  TEXT NOT NULL DEFAULT '',
    inport_json TEXT NOT NULL DEFAULT '{}',
    inport_fetched_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS assets (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    uri         TEXT NOT NULL UNIQUE,     -- gs://bucket/key or file:///abs/path
    folder      TEXT NOT NULL DEFAULT '', -- POSIX prefix relative to source root
    name        TEXT NOT NULL,
    ext         TEXT NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    etag        TEXT NOT NULL DEFAULT '', -- GCS generation, or 'mtime:size'
    mtime       REAL NOT NULL DEFAULT 0,
    width       INTEGER NOT NULL DEFAULT 0,
    height      INTEGER NOT NULL DEFAULT 0,
    phash       TEXT NOT NULL DEFAULT '',
    blur        REAL NOT NULL DEFAULT 0,
    brightness  REAL NOT NULL DEFAULT 0,
    contrast    REAL NOT NULL DEFAULT 0,
    color_cast  REAL NOT NULL DEFAULT 1,
    quality     REAL NOT NULL DEFAULT 0,
    thumb_path  TEXT NOT NULL DEFAULT '',
    embed_row   INTEGER NOT NULL DEFAULT -1,
    embed_model TEXT NOT NULL DEFAULT '',
    annotated   TEXT NOT NULL DEFAULT '', -- comma-joined annotator names
    indexed_at  REAL NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | ok | error
    error       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_assets_folder  ON assets(folder);
CREATE INDEX IF NOT EXISTS idx_assets_quality ON assets(quality);
CREATE INDEX IF NOT EXISTS idx_assets_phash   ON assets(phash);
CREATE INDEX IF NOT EXISTS idx_assets_source  ON assets(source_id, etag);
CREATE INDEX IF NOT EXISTS idx_assets_status  ON assets(status);
CREATE INDEX IF NOT EXISTS idx_assets_embed   ON assets(embed_row);

CREATE TABLE IF NOT EXISTS tags (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE,
    kind  TEXT NOT NULL DEFAULT 'manual',  -- manual | class | concept | path
    color TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS asset_tags (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags(id)   ON DELETE CASCADE,
    score    REAL NOT NULL DEFAULT 1.0,
    origin   TEXT NOT NULL DEFAULT 'manual',  -- manual | yolo | sam3 | path
    PRIMARY KEY (asset_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_asset_tags_tag ON asset_tags(tag_id);

CREATE TABLE IF NOT EXISTS detections (
    id       INTEGER PRIMARY KEY,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    label    TEXT NOT NULL,
    conf     REAL NOT NULL DEFAULT 0,
    x        REAL NOT NULL DEFAULT 0,   -- normalised cx, cy, w, h (YOLO order)
    y        REAL NOT NULL DEFAULT 0,
    w        REAL NOT NULL DEFAULT 0,
    h        REAL NOT NULL DEFAULT 0,
    model    TEXT NOT NULL DEFAULT '',
    mask     TEXT NOT NULL DEFAULT ''   -- JSON: normalised polygon(s), YOLO-seg ready
);

CREATE INDEX IF NOT EXISTS idx_det_asset ON detections(asset_id);
CREATE INDEX IF NOT EXISTS idx_det_label ON detections(label, conf);

CREATE TABLE IF NOT EXISTS datasets (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    notes      TEXT NOT NULL DEFAULT '',
    spec_json  TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_items (
    dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    asset_id   INTEGER NOT NULL REFERENCES assets(id)   ON DELETE CASCADE,
    split      TEXT NOT NULL DEFAULT 'train',
    PRIMARY KEY (dataset_id, asset_id)
);

CREATE INDEX IF NOT EXISTS idx_ds_items_asset ON dataset_items(asset_id);

CREATE TABLE IF NOT EXISTS saved_queries (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    query_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,            -- running | done | error | cancelled
    total       INTEGER NOT NULL DEFAULT 0,
    done        INTEGER NOT NULL DEFAULT 0,
    message     TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}',
    started_at  REAL NOT NULL,
    finished_at REAL NOT NULL DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS assets_fts USING fts5(
    name, folder, tags
);

-- rowid = sources.id; lets a keyword search match a collection's metadata.
CREATE VIRTUAL TABLE IF NOT EXISTS sources_fts USING fts5(
    label, survey, region, contact, description, extra
);
"""

_local = threading.local()
_write_lock = threading.Lock()


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")


def connect() -> sqlite3.Connection:
    """Thread-local connection.  SQLite objects are not shareable across threads."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        settings.ensure_dirs()
        conn = sqlite3.connect(settings.DB_PATH, timeout=15.0)
        _configure(conn)
        _local.conn = conn
    return conn


@contextmanager
def write() -> Iterator[sqlite3.Connection]:
    """
    Serialised write transaction.

    SQLite allows exactly one writer; taking the lock in-process turns
    contention into a short wait instead of a SQLITE_BUSY error under the
    indexer's thread pool.
    """
    conn = connect()
    with _write_lock:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def init_db() -> None:
    conn = connect()
    with _write_lock:
        _migrate_fts(conn)
        conn.executescript(_SCHEMA)
        _add_missing_columns(conn)
        _backfill_sources_fts(conn)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()


def close() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


# ── FTS maintenance ───────────────────────────────────────────────────────────
#
# assets_fts is a regular FTS5 table keyed by rowid = assets.id.  It is
# maintained explicitly (delete + insert) rather than with triggers, because the
# tags column has to be refreshed whenever *tags* change, not only on asset
# write — and triggers on asset_tags cannot see the resulting aggregate.
#
# It deliberately does not use ``content=''``: a contentless FTS5 table rejects
# DELETE unless the SQLite build has contentless_delete (3.43+), and the three
# short columns it stores are a rounding error next to the thumbnails.


def _migrate_fts(conn: sqlite3.Connection) -> None:
    """Rebuild assets_fts if it was created by an older contentless schema."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='assets_fts'"
    ).fetchone()
    if row and "content=" in (row["sql"] or ""):
        conn.execute("DROP TABLE assets_fts")
        conn.execute("CREATE VIRTUAL TABLE assets_fts USING fts5(name, folder, tags)")
        conn.execute(
            """
            INSERT INTO assets_fts(rowid, name, folder, tags)
            SELECT a.id, a.name, a.folder,
                   COALESCE((SELECT GROUP_CONCAT(t.name, ' ') FROM asset_tags at
                              JOIN tags t ON t.id = at.tag_id WHERE at.asset_id = a.id), '')
              FROM assets a
            """
        )


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """
    Additive migrations for catalogs created by an earlier build.

    CREATE TABLE IF NOT EXISTS leaves an existing table alone, so new columns
    have to be added explicitly rather than by editing _SCHEMA.
    """
    for table, column, ddl in (
        ("sources", "tag_pattern", "ALTER TABLE sources ADD COLUMN tag_pattern TEXT NOT NULL DEFAULT ''"),
        ("sources", "model_ref", "ALTER TABLE sources ADD COLUMN model_ref TEXT NOT NULL DEFAULT ''"),
        ("sources", "year_start", "ALTER TABLE sources ADD COLUMN year_start INTEGER NOT NULL DEFAULT 0"),
        ("sources", "year_end", "ALTER TABLE sources ADD COLUMN year_end INTEGER NOT NULL DEFAULT 0"),
        ("sources", "survey", "ALTER TABLE sources ADD COLUMN survey TEXT NOT NULL DEFAULT ''"),
        ("sources", "region", "ALTER TABLE sources ADD COLUMN region TEXT NOT NULL DEFAULT ''"),
        ("sources", "contact", "ALTER TABLE sources ADD COLUMN contact TEXT NOT NULL DEFAULT ''"),
        ("sources", "description", "ALTER TABLE sources ADD COLUMN description TEXT NOT NULL DEFAULT ''"),
        ("sources", "extras_json", "ALTER TABLE sources ADD COLUMN extras_json TEXT NOT NULL DEFAULT '{}'"),
        ("sources", "inport_url", "ALTER TABLE sources ADD COLUMN inport_url TEXT NOT NULL DEFAULT ''"),
        ("sources", "inport_json", "ALTER TABLE sources ADD COLUMN inport_json TEXT NOT NULL DEFAULT '{}'"),
        ("sources", "inport_fetched_at", "ALTER TABLE sources ADD COLUMN inport_fetched_at REAL NOT NULL DEFAULT 0"),
    ):
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(ddl)


def _backfill_sources_fts(conn: sqlite3.Connection) -> None:
    """Index any collection that has no sources_fts row yet."""
    for row in conn.execute(
        "SELECT * FROM sources WHERE id NOT IN (SELECT rowid FROM sources_fts)"
    ).fetchall():
        source_fts_upsert(conn, row)


def source_fts_upsert(conn: sqlite3.Connection, row: sqlite3.Row | dict) -> None:
    """Refresh one collection's searchable metadata from a sources row."""
    try:
        extras = json.loads(row["extras_json"] or "{}")
    except ValueError:
        extras = {}
    try:
        inport = json.loads(row["inport_json"] or "{}")
    except ValueError:
        inport = {}
    extra = " ".join(
        [*(f"{k} {v}" for k, v in extras.items()),
         inport.get("title", ""), inport.get("short_name", ""), " ".join(inport.get("keywords", []))]
    )
    conn.execute("DELETE FROM sources_fts WHERE rowid = ?", (row["id"],))
    conn.execute(
        "INSERT INTO sources_fts(rowid, label, survey, region, contact, description, extra) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (row["id"], row["label"], row["survey"], row["region"], row["contact"],
         row["description"], extra),
    )


def fts_upsert(conn: sqlite3.Connection, asset_id: int, name: str, folder: str, tags: str) -> None:
    conn.execute("DELETE FROM assets_fts WHERE rowid = ?", (asset_id,))
    conn.execute(
        "INSERT INTO assets_fts(rowid, name, folder, tags) VALUES (?, ?, ?, ?)",
        (asset_id, name, folder, tags),
    )


def fts_refresh(conn: sqlite3.Connection, asset_ids: list[int]) -> None:
    """Rebuild FTS rows for the given assets from current tag state."""
    for start in range(0, len(asset_ids), 900):      # SQLite bound-variable limit
        chunk = asset_ids[start:start + 900]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT a.id, a.name, a.folder,
                   COALESCE(GROUP_CONCAT(t.name, ' '), '') AS tags
              FROM assets a
              LEFT JOIN asset_tags at ON at.asset_id = a.id
              LEFT JOIN tags t        ON t.id = at.tag_id
             WHERE a.id IN ({marks})
             GROUP BY a.id
            """,
            chunk,
        ).fetchall()
        for row in rows:
            fts_upsert(conn, row["id"], row["name"], row["folder"], row["tags"])
