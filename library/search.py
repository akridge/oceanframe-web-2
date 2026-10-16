"""
Query planner.

A query has two halves: **structured predicates** (folder prefix, quality band,
tags, detected classes, extension, date) and an optional **vector query**
(semantic text, or "more like this asset").  The planner always runs the
structured half first in SQLite, then ranks only the surviving rows by cosine
similarity.

That order matters.  An ANN index cannot see `folder LIKE '2024/kaneohe/%' AND
quality > 60 AND has fish at conf >= 0.4`, so ranking first and filtering
afterwards silently loses recall on exactly the selective queries a survey
library is built for.  Filtering first keeps the ranking exact and the result
complete.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from library import db, settings
from library.embed import get_embedder
from library.models import asset_to_dict
from library.quality import phash_distance
from library.vectorstore import get_store

SORTS = {
    "relevance": None,                      # only meaningful with a vector query
    "quality":   "a.quality DESC, a.id",
    "worst":     "a.quality ASC, a.id",
    "newest":    "a.mtime DESC, a.id",
    "oldest":    "a.mtime ASC, a.id",
    "name":      "a.folder, a.name",
    "sharpest":  "a.blur DESC, a.id",
    "random":    "RANDOM()",
}

# Facets over a semantic result set describe the top of the ranking, not the
# whole catalog; this caps how deep that summary goes.
FACET_DEPTH = 1000


@dataclass
class Query:
    text:        str = ""
    mode:        str = "auto"          # auto | semantic | keyword
    keywords:    str = ""              # always FTS, can accompany a semantic text
    similar_to:  int | None = None
    vector:      list[float] | None = None   # pre-computed (uploaded image)
    folder:      str = ""
    folder_exact: bool = False
    source_id:   int | None = None
    # Collection metadata filters: match assets whose collection carries them.
    year:        int | None = None
    survey:      str = ""
    region:      str = ""
    tags:        list[str] = field(default_factory=list)
    tags_any:    bool = False
    exclude_tags: list[str] = field(default_factory=list)
    labels:      list[str] = field(default_factory=list)
    labels_any:  bool = True
    label_conf:  float = 0.0
    label_min_count: int = 1
    ext:         list[str] = field(default_factory=list)
    quality_min: float | None = None
    quality_max: float | None = None
    blur_min:    float | None = None
    brightness_min: float | None = None
    brightness_max: float | None = None
    status:      str = "ok"
    dataset_id:  int | None = None
    untagged:    bool = False
    unannotated: bool = False
    dedupe:      bool = False
    sort:        str = "relevance"
    page:        int = 0
    page_size:   int = 0

    @classmethod
    def from_dict(cls, raw: dict) -> "Query":
        known = {f for f in cls.__dataclass_fields__}
        query = cls(**{k: v for k, v in (raw or {}).items() if k in known and v is not None})
        query.page_size = min(
            query.page_size or settings.PAGE_SIZE, settings.MAX_PAGE_SIZE
        )
        query.page = max(0, int(query.page))
        query.folder = (query.folder or "").strip("/")
        return query

    def is_vector(self) -> bool:
        return self.vector is not None or self.similar_to is not None or bool(
            self.text and self.mode != "keyword"
        )

    def has_filters(self) -> bool:
        return bool(
            self.folder or self.tags or self.exclude_tags or self.labels or self.ext
            or self.keywords or self.dataset_id or self.untagged or self.unannotated
            or self.source_id is not None
            or self.year is not None or self.survey or self.region
            or self.quality_min is not None or self.quality_max is not None
            or self.blur_min is not None
            or self.brightness_min is not None or self.brightness_max is not None
            or self.status != "ok"
        )


# ── SQL construction ──────────────────────────────────────────────────────────

def _where(query: Query) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    if query.status and query.status != "any":
        clauses.append("a.status = ?")
        params.append(query.status)

    if query.source_id is not None:
        clauses.append("a.source_id = ?")
        params.append(query.source_id)

    if query.year is not None:
        # A collection with only a start year covers just that year.
        clauses.append(
            "a.source_id IN (SELECT id FROM sources WHERE year_start > 0 AND year_start <= ? "
            "AND (CASE WHEN year_end > 0 THEN year_end ELSE year_start END) >= ?)"
        )
        params.extend([int(query.year), int(query.year)])
    if query.survey:
        clauses.append("a.source_id IN (SELECT id FROM sources WHERE survey = ?)")
        params.append(query.survey)
    if query.region:
        clauses.append("a.source_id IN (SELECT id FROM sources WHERE region = ?)")
        params.append(query.region)

    if query.folder:
        if query.folder_exact:
            clauses.append("a.folder = ?")
            params.append(query.folder)
        else:
            # Match the folder itself and everything beneath it, without the
            # LIKE wildcard escaping hazards of a user-supplied prefix.
            clauses.append("(a.folder = ? OR a.folder GLOB ?)")
            params.extend([query.folder, f"{_glob_escape(query.folder)}/*"])

    if query.ext:
        marks = ",".join("?" * len(query.ext))
        clauses.append(f"a.ext IN ({marks})")
        params.extend([e if e.startswith(".") else f".{e}" for e in (x.lower() for x in query.ext)])

    for column, value, op in (
        ("a.quality", query.quality_min, ">="), ("a.quality", query.quality_max, "<="),
        ("a.blur", query.blur_min, ">="),
        ("a.brightness", query.brightness_min, ">="), ("a.brightness", query.brightness_max, "<="),
    ):
        if value is not None:
            clauses.append(f"{column} {op} ?")
            params.append(float(value))

    if query.tags:
        marks = ",".join("?" * len(query.tags))
        if query.tags_any:
            clauses.append(
                f"EXISTS (SELECT 1 FROM asset_tags at JOIN tags t ON t.id = at.tag_id "
                f"WHERE at.asset_id = a.id AND t.name IN ({marks}))"
            )
            params.extend(query.tags)
        else:
            # All-of: count distinct matching tags and require the full set.
            clauses.append(
                f"(SELECT COUNT(DISTINCT t.name) FROM asset_tags at JOIN tags t ON t.id = at.tag_id "
                f"WHERE at.asset_id = a.id AND t.name IN ({marks})) = ?"
            )
            params.extend([*query.tags, len(set(query.tags))])

    if query.exclude_tags:
        marks = ",".join("?" * len(query.exclude_tags))
        clauses.append(
            f"NOT EXISTS (SELECT 1 FROM asset_tags at JOIN tags t ON t.id = at.tag_id "
            f"WHERE at.asset_id = a.id AND t.name IN ({marks}))"
        )
        params.extend(query.exclude_tags)

    if query.labels:
        marks = ",".join("?" * len(query.labels))
        count_sql = (
            f"SELECT COUNT(*) FROM detections d WHERE d.asset_id = a.id "
            f"AND d.label IN ({marks}) AND d.conf >= ?"
        )
        if query.labels_any:
            clauses.append(f"({count_sql}) >= ?")
            params.extend([*query.labels, float(query.label_conf), max(1, query.label_min_count)])
        else:
            distinct_sql = (
                f"SELECT COUNT(DISTINCT d.label) FROM detections d WHERE d.asset_id = a.id "
                f"AND d.label IN ({marks}) AND d.conf >= ?"
            )
            clauses.append(f"({distinct_sql}) = ?")
            params.extend([*query.labels, float(query.label_conf), len(set(query.labels))])

    if query.untagged:
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM asset_tags at JOIN tags t ON t.id = at.tag_id "
            "WHERE at.asset_id = a.id AND t.kind = 'manual')"
        )
    if query.unannotated:
        clauses.append("NOT EXISTS (SELECT 1 FROM detections d WHERE d.asset_id = a.id)")

    if query.dataset_id is not None:
        clauses.append("EXISTS (SELECT 1 FROM dataset_items di WHERE di.asset_id = a.id AND di.dataset_id = ?)")
        params.append(query.dataset_id)

    if query.keywords or (query.text and query.mode == "keyword"):
        needle = _fts_query(query.keywords or query.text)
        clauses.append(
            "(a.id IN (SELECT rowid FROM assets_fts WHERE assets_fts MATCH ?) "
            "OR a.source_id IN (SELECT rowid FROM sources_fts WHERE sources_fts MATCH ?))"
        )
        params.extend([needle, needle])

    return (" AND ".join(clauses) if clauses else "1=1"), params


def _glob_escape(value: str) -> str:
    return value.replace("[", "[[]").replace("*", "[*]").replace("?", "[?]")


def _fts_query(raw: str) -> str:
    """
    Turn free text into a safe FTS5 expression.

    Every token is quoted (so ``site:kaneohe`` is not read as a column filter)
    and given a prefix wildcard, which is what people expect from a filename box.
    """
    tokens = [t for t in "".join(c if c.isalnum() or c in "-_." else " " for c in raw).split() if t]
    return " AND ".join(f'"{t}"*' for t in tokens) if tokens else '""'


_SELECT = """
SELECT a.*, s.label AS source_label,
       (SELECT GROUP_CONCAT(t.name, char(31)) FROM asset_tags at JOIN tags t ON t.id = at.tag_id
         WHERE at.asset_id = a.id) AS tags,
       (SELECT GROUP_CONCAT(DISTINCT d.label) FROM detections d WHERE d.asset_id = a.id) AS det_labels
  FROM assets a
  JOIN sources s ON s.id = a.source_id
"""


def _rows_for_ids(conn: sqlite3.Connection, ids: list[int]) -> dict[int, sqlite3.Row]:
    if not ids:
        return {}
    out: dict[int, sqlite3.Row] = {}
    for start in range(0, len(ids), 900):        # SQLite's variable limit
        chunk = ids[start:start + 900]
        marks = ",".join("?" * len(chunk))
        for row in conn.execute(f"{_SELECT} WHERE a.id IN ({marks})", chunk):
            out[row["id"]] = row
    return out


# ── Search ────────────────────────────────────────────────────────────────────

def run(raw: dict) -> dict:
    query = Query.from_dict(raw)
    conn = db.connect()
    where, params = _where(query)

    total = int(conn.execute(f"SELECT COUNT(*) FROM assets a WHERE {where}", params).fetchone()[0])

    if query.is_vector():
        items, scored_total, note = _vector_search(conn, query, where, params, total)
    else:
        items, scored_total, note = _sorted_search(conn, query, where, params, total)

    return {
        "total": total,
        "matched": scored_total,
        "page": query.page,
        "page_size": query.page_size,
        "items": items,
        "note": note,
        "sort": query.sort,
        "vector": query.is_vector(),
    }


def _sorted_search(conn, query: Query, where: str, params: list, total: int) -> tuple[list[dict], int, str]:
    order = SORTS.get(query.sort) or SORTS["quality"]
    limit = query.page_size
    offset = query.page * query.page_size

    if query.dedupe:
        # Dedupe changes which rows survive, so it has to happen before the page
        # is cut.  Scan a bounded window rather than the whole catalog.
        window = min(total, max(offset + limit, 1) * 4 + 500)
        rows = conn.execute(f"{_SELECT} WHERE {where} ORDER BY {order} LIMIT ?", [*params, window]).fetchall()
        kept = _dedupe(rows)
        page = kept[offset:offset + limit]
        return [asset_to_dict(r) for r in page], len(kept), _dedupe_note(len(rows), len(kept), window < total)

    rows = conn.execute(
        f"{_SELECT} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", [*params, limit, offset]
    ).fetchall()
    return [asset_to_dict(r) for r in rows], total, ""


def _vector_search(conn, query: Query, where: str, params: list, total: int) -> tuple[list[dict], int, str]:
    vector, note = _query_vector(conn, query)
    if vector is None:
        # No usable vector (no CLIP for a text query): fall back to keyword so
        # the user still gets results, and say so.
        fallback = Query.from_dict({**query.__dict__, "mode": "keyword",
                                    "keywords": query.keywords or query.text,
                                    "sort": query.sort if query.sort != "relevance" else "quality"})
        fb_where, fb_params = _where(fallback)
        fb_total = int(conn.execute(f"SELECT COUNT(*) FROM assets a WHERE {fb_where}", fb_params).fetchone()[0])
        items, matched, _ = _sorted_search(conn, fallback, fb_where, fb_params, fb_total)
        return items, matched, note

    store = get_store()
    candidate_rows = None
    row_to_id: dict[int, int] = {}
    if query.has_filters() or total < store.rows:
        pairs = conn.execute(
            f"SELECT a.id, a.embed_row FROM assets a WHERE {where} AND a.embed_row >= 0", params
        ).fetchall()
        candidate_rows = [int(r["embed_row"]) for r in pairs]
        row_to_id = {int(r["embed_row"]): int(r["id"]) for r in pairs}
        if not candidate_rows:
            return [], 0, note

    depth = max(FACET_DEPTH, (query.page + 1) * query.page_size * (4 if query.dedupe else 1) + 100)
    ranked = store.search(vector, top_k=depth, candidate_rows=candidate_rows)
    if candidate_rows is None:
        row_to_id = {
            int(r["embed_row"]): int(r["id"])
            for r in conn.execute(
                "SELECT id, embed_row FROM assets WHERE embed_row IN ({})".format(
                    ",".join(str(int(r)) for r, _ in ranked) or "-1"
                )
            )
        }

    ordered = [(row_to_id[row], score) for row, score in ranked if row in row_to_id]
    if query.similar_to is not None:
        ordered = [(i, s) for i, s in ordered if i != query.similar_to]

    lookup = _rows_for_ids(conn, [i for i, _ in ordered])
    rows = [(lookup[i], s) for i, s in ordered if i in lookup]

    if query.dedupe:
        kept = _dedupe([r for r, _ in rows])
        keep_ids = {r["id"] for r in kept}
        rows = [(r, s) for r, s in rows if r["id"] in keep_ids]

    if query.sort not in {"relevance", ""}:
        rows = _resort(rows, query.sort)

    offset = query.page * query.page_size
    page = rows[offset:offset + query.page_size]
    return [asset_to_dict(r, score=s) for r, s in page], len(rows), note


def _resort(rows: list[tuple[sqlite3.Row, float]], sort: str) -> list[tuple[sqlite3.Row, float]]:
    keys = {
        "quality":  (lambda p: -p[0]["quality"]),
        "worst":    (lambda p: p[0]["quality"]),
        "newest":   (lambda p: -p[0]["mtime"]),
        "oldest":   (lambda p: p[0]["mtime"]),
        "name":     (lambda p: (p[0]["folder"], p[0]["name"])),
        "sharpest": (lambda p: -p[0]["blur"]),
    }
    key = keys.get(sort)
    return sorted(rows, key=key) if key else rows


def _query_vector(conn, query: Query) -> tuple[np.ndarray | None, str]:
    if query.vector is not None:
        return np.asarray(query.vector, dtype=np.float32), ""

    if query.similar_to is not None:
        row = conn.execute("SELECT embed_row FROM assets WHERE id = ?", (query.similar_to,)).fetchone()
        if not row or row["embed_row"] < 0:
            return None, "That asset has no embedding yet — run an embed pass."
        vectors = get_store().get([int(row["embed_row"])])
        if vectors.shape[0] == 0:
            return None, "That asset's vector is missing from the store."
        return vectors[0], ""

    embedder = get_embedder()
    if not embedder.supports_text:
        return None, (
            "Text search needs the CLIP backend; matched on filenames, folders and tags instead. "
            "Install torch + open_clip_torch and set LIB_EMBED_BACKEND=clip, then re-embed."
        )
    return embedder.embed_text([query.text])[0], ""


# ── Near-duplicate collapsing ─────────────────────────────────────────────────

def _dedupe(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """
    Greedy phash grouping: keep the first row of each visual group.

    Rows arrive already ordered (by relevance or by the chosen sort), so "first"
    is "best" — a burst of 30 near-identical transect frames collapses to the
    one the user actually wants.
    """
    kept: list[sqlite3.Row] = []
    seen: list[str] = []
    for row in rows:
        phash = row["phash"] or ""
        if phash and any(phash_distance(phash, other) <= settings.DUPE_DISTANCE for other in seen):
            continue
        kept.append(row)
        if phash:
            seen.append(phash)
    return kept


def _dedupe_note(scanned: int, kept: int, truncated: bool) -> str:
    dropped = scanned - kept
    note = f"Collapsed {dropped:,} near-duplicate{'s' if dropped != 1 else ''}"
    return note + (" (within the scanned window)" if truncated else "")


def duplicate_groups(raw: dict, limit: int = 100) -> list[dict]:
    """Explicit duplicate report: groups of two or more visually identical assets."""
    query = Query.from_dict(raw)
    conn = db.connect()
    where, params = _where(query)
    rows = conn.execute(
        f"{_SELECT} WHERE {where} AND a.phash != '' ORDER BY a.quality DESC LIMIT 20000", params
    ).fetchall()

    groups: list[dict] = []
    assigned: set[int] = set()
    for row in rows:
        if row["id"] in assigned:
            continue
        members = [row]
        assigned.add(row["id"])
        for other in rows:
            if other["id"] in assigned:
                continue
            if phash_distance(row["phash"], other["phash"]) <= settings.DUPE_DISTANCE:
                members.append(other)
                assigned.add(other["id"])
        if len(members) > 1:
            groups.append({
                "keep": asset_to_dict(members[0]),
                "duplicates": [asset_to_dict(m) for m in members[1:]],
                "size": len(members),
            })
        if len(groups) >= limit:
            break
    return sorted(groups, key=lambda g: -g["size"])


# ── Facets ────────────────────────────────────────────────────────────────────

def facets(raw: dict, top: int = 40) -> dict:
    """Tag, class, extension and quality-band counts over the filtered set."""
    query = Query.from_dict(raw)
    conn = db.connect()
    where, params = _where(query)
    scope = f"SELECT a.id FROM assets a WHERE {where}"

    tag_rows = conn.execute(
        f"SELECT t.name, t.kind, COUNT(*) AS n FROM asset_tags at "
        f"JOIN tags t ON t.id = at.tag_id WHERE at.asset_id IN ({scope}) "
        f"GROUP BY t.id ORDER BY n DESC LIMIT ?", [*params, top]
    ).fetchall()
    # Count at the same confidence the filter will apply, otherwise a facet
    # reading "kite 12" hands back 10 results the moment you click it.
    label_rows = conn.execute(
        f"SELECT d.label, COUNT(DISTINCT d.asset_id) AS n FROM detections d "
        f"WHERE d.asset_id IN ({scope}) AND d.conf >= ? GROUP BY d.label ORDER BY n DESC LIMIT ?",
        [*params, float(query.label_conf), top]
    ).fetchall()
    source_rows = conn.execute(
        f"SELECT a.source_id, s.label, s.root, s.year_start, s.year_end, s.survey, s.region, "
        f"COUNT(*) AS n FROM assets a "
        f"JOIN sources s ON s.id = a.source_id WHERE {where} "
        f"GROUP BY a.source_id ORDER BY n DESC", params
    ).fetchall()
    ext_rows = conn.execute(
        f"SELECT a.ext, COUNT(*) AS n FROM assets a WHERE {where} GROUP BY a.ext ORDER BY n DESC", params
    ).fetchall()
    bands = conn.execute(
        f"""SELECT
              SUM(CASE WHEN a.quality < 25 THEN 1 ELSE 0 END) AS poor,
              SUM(CASE WHEN a.quality >= 25 AND a.quality < 50 THEN 1 ELSE 0 END) AS fair,
              SUM(CASE WHEN a.quality >= 50 AND a.quality < 70 THEN 1 ELSE 0 END) AS good,
              SUM(CASE WHEN a.quality >= 70 THEN 1 ELSE 0 END) AS excellent
            FROM assets a WHERE {where}""", params
    ).fetchone()

    return {
        "sources": [
            {"id": r["source_id"], "label": r["label"] or r["root"], "root": r["root"], "count": r["n"]}
            for r in source_rows
        ],
        "tags":   [{"name": r["name"], "kind": r["kind"], "count": r["n"]} for r in tag_rows],
        "labels": [{"name": r["label"], "count": r["n"]} for r in label_rows],
        "ext":    [{"name": r["ext"], "count": r["n"]} for r in ext_rows],
        "quality": {k: (bands[k] or 0) for k in ("poor", "fair", "good", "excellent")},
        **_collection_facets(source_rows),
    }


def _collection_facets(source_rows: list[sqlite3.Row]) -> dict:
    """Year / survey / region counts, rolled up from per-collection asset counts."""
    years: dict[int, int] = {}
    surveys: dict[str, int] = {}
    regions: dict[str, int] = {}
    for r in source_rows:
        start = r["year_start"] or 0
        end = r["year_end"] or start
        if start:
            for year in range(start, min(end, start + 100) + 1):
                years[year] = years.get(year, 0) + r["n"]
        if r["survey"]:
            surveys[r["survey"]] = surveys.get(r["survey"], 0) + r["n"]
        if r["region"]:
            regions[r["region"]] = regions.get(r["region"], 0) + r["n"]
    return {
        "years":   [{"name": y, "count": n} for y, n in sorted(years.items(), reverse=True)],
        "surveys": [{"name": k, "count": n} for k, n in sorted(surveys.items())],
        "regions": [{"name": k, "count": n} for k, n in sorted(regions.items())],
    }


def folder_children(raw: dict, prefix: str = "") -> dict:
    """
    Immediate sub-folders of ``prefix`` with recursive counts, scoped to the
    current query — so the tree doubles as a facet, not just a file browser.
    """
    query = Query.from_dict({**(raw or {}), "folder": "", "folder_exact": False})
    conn = db.connect()
    where, params = _where(query)

    prefix = (prefix or "").strip("/")
    offset = len(prefix) + 1 if prefix else 0
    scope_sql = where
    scope_params = list(params)
    if prefix:
        scope_sql += " AND (a.folder = ? OR a.folder GLOB ?)"
        scope_params.extend([prefix, f"{_glob_escape(prefix)}/*"])

    rows = conn.execute(
        f"""
        SELECT CASE
                 WHEN INSTR(SUBSTR(a.folder, ?), '/') > 0
                 THEN SUBSTR(a.folder, ?, INSTR(SUBSTR(a.folder, ?), '/') - 1)
                 ELSE SUBSTR(a.folder, ?)
               END AS segment,
               COUNT(*) AS n
          FROM assets a
         WHERE {scope_sql}
         GROUP BY segment
         HAVING segment != ''
         ORDER BY segment
        """,
        [offset + 1, offset + 1, offset + 1, offset + 1, *scope_params],
    ).fetchall()

    here = conn.execute(
        f"SELECT COUNT(*) FROM assets a WHERE {where}" + (" AND a.folder = ?" if prefix else " AND a.folder = ''"),
        [*params, prefix] if prefix else params,
    ).fetchone()[0]

    return {
        "prefix": prefix,
        "here": here,
        "children": [
            {"name": r["segment"], "path": f"{prefix}/{r['segment']}" if prefix else r["segment"], "count": r["n"]}
            for r in rows
        ],
    }


def candidate_ids(raw: dict, cap: int = 200_000) -> list[int]:
    """Every asset id matching a query — the selection a dataset is built from."""
    query = Query.from_dict(raw)
    conn = db.connect()
    where, params = _where(query)
    order = SORTS.get(query.sort) or SORTS["quality"]

    if query.is_vector():
        result = run({**raw, "page": 0, "page_size": settings.MAX_PAGE_SIZE})
        # A vector query is inherently top-k; take the ranked ids as the set.
        return [item["id"] for item in result["items"]]

    rows = conn.execute(f"SELECT a.id FROM assets a WHERE {where} ORDER BY {order} LIMIT ?", [*params, cap])
    return [int(r["id"]) for r in rows]


def stats() -> dict:
    conn = db.connect()
    row = conn.execute(
        """
        SELECT COUNT(*) AS n,
               SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END)      AS ok,
               SUM(CASE WHEN status='error' THEN 1 ELSE 0 END)   AS failed,
               SUM(CASE WHEN status='missing' THEN 1 ELSE 0 END) AS missing,
               SUM(CASE WHEN embed_row >= 0 THEN 1 ELSE 0 END)   AS embedded,
               COALESCE(SUM(size), 0)                            AS bytes,
               COALESCE(AVG(CASE WHEN status='ok' THEN quality END), 0) AS avg_quality
          FROM assets
        """
    ).fetchone()
    folders = conn.execute("SELECT COUNT(DISTINCT folder) FROM assets").fetchone()[0]
    detections = conn.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
    tag_count = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    return {
        "assets": row["n"] or 0,
        "ok": row["ok"] or 0,
        "failed": row["failed"] or 0,
        "missing": row["missing"] or 0,
        "embedded": row["embedded"] or 0,
        "bytes": row["bytes"] or 0,
        "avg_quality": round(row["avg_quality"] or 0, 1),
        "folders": folders,
        "detections": detections,
        "tags": tag_count,
        "vectors": get_store().stats(),
    }
