"""
Tag helpers.

Tags are the join point between everything: manual curation, YOLO classes
(``class:fish``), SAM 3 concepts (``concept:bleached coral``), and folder-path
rules (``site:kaneohe``).  Keeping them in one namespace means one facet list,
one filter grammar, and one dataset selector.
"""
from __future__ import annotations

import re
import sqlite3

from library import db, settings

VALID_KINDS = {"manual", "class", "concept", "path"}


def normalise(name: str) -> str:
    """Collapse whitespace and lowercase the key half of a ``key:value`` tag."""
    name = " ".join(name.strip().split())
    if ":" in name:
        key, _, value = name.partition(":")
        return f"{key.strip().lower()}:{value.strip()}"
    return name


def ensure_tag(conn: sqlite3.Connection, name: str, kind: str = "manual") -> int:
    name = normalise(name)
    if not name:
        raise ValueError("Tag name cannot be empty")
    kind = kind if kind in VALID_KINDS else "manual"
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute("INSERT INTO tags(name, kind) VALUES (?, ?)", (name, kind))
    return int(cur.lastrowid)


def add_tags(
    conn: sqlite3.Connection,
    asset_ids: list[int],
    names: list[str],
    kind: str = "manual",
    origin: str = "manual",
    score: float = 1.0,
) -> int:
    if not asset_ids or not names:
        return 0
    tag_ids = [ensure_tag(conn, n, kind) for n in names if n.strip()]
    pairs = [(a, t, score, origin) for a in asset_ids for t in tag_ids]
    conn.executemany(
        "INSERT INTO asset_tags(asset_id, tag_id, score, origin) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(asset_id, tag_id) DO UPDATE SET score=excluded.score, origin=excluded.origin",
        pairs,
    )
    db.fts_refresh(conn, asset_ids)
    return len(pairs)


def remove_tags(conn: sqlite3.Connection, asset_ids: list[int], names: list[str]) -> int:
    if not asset_ids or not names:
        return 0
    normalised = [normalise(n) for n in names]
    marks_a = ",".join("?" * len(asset_ids))
    marks_t = ",".join("?" * len(normalised))
    cur = conn.execute(
        f"DELETE FROM asset_tags WHERE asset_id IN ({marks_a}) AND tag_id IN "
        f"(SELECT id FROM tags WHERE name IN ({marks_t}))",
        [*asset_ids, *normalised],
    )
    db.fts_refresh(conn, asset_ids)
    return cur.rowcount


def clear_origin(conn: sqlite3.Connection, asset_ids: list[int], origin: str) -> None:
    """Drop model-written tags before a re-run so stale classes do not accumulate."""
    if not asset_ids:
        return
    marks = ",".join("?" * len(asset_ids))
    conn.execute(
        f"DELETE FROM asset_tags WHERE origin = ? AND asset_id IN ({marks})", [origin, *asset_ids]
    )


def list_tags(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT t.id, t.name, t.kind, COUNT(at.asset_id) AS n
          FROM tags t
          LEFT JOIN asset_tags at ON at.tag_id = t.id
         GROUP BY t.id
         ORDER BY n DESC, t.name
        """
    ).fetchall()
    return [{"id": r["id"], "name": r["name"], "kind": r["kind"], "count": r["n"]} for r in rows]


# ── Path-derived tags ─────────────────────────────────────────────────────────

_pattern_cache: dict[str, re.Pattern | None] = {}


def _compiled_pattern(pattern: str) -> re.Pattern | None:
    if pattern not in _pattern_cache:
        try:
            _pattern_cache[pattern] = re.compile(pattern) if pattern else None
        except re.error:
            _pattern_cache[pattern] = None
    return _pattern_cache[pattern]


def path_tags(relative_key: str, pattern: str | None = None) -> list[str]:
    """
    Turn a path into ``key:value`` tags using the configured regex.

    ``(?P<year>\\d{4})/(?P<site>[^/]+)/(?P<transect>T\\d+)`` applied to
    ``2024/kaneohe/T03/img_0912.jpg`` yields
    ``['year:2024', 'site:kaneohe', 'transect:T03']``.
    """
    # None = fall back to the global default; "" = this source has no rule.
    compiled = _compiled_pattern(settings.PATH_TAG_PATTERN if pattern is None else pattern)
    if compiled is None:
        return []
    match = compiled.search(relative_key)
    if not match:
        return []
    return [
        normalise(f"{key}:{value}")
        for key, value in (match.groupdict() or {}).items()
        if value
    ]
