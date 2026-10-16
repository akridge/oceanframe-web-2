"""
Image library API — /api/library/*

Long operations (crawl, embed, annotate, export) return a job id immediately and
report progress over SSE; everything else is a normal request/response.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from PIL import Image
from pydantic import BaseModel, Field, field_validator

from auth.deps import require_curator, require_lead
from auth.service import CurrentUser
from library import datasets as datasets_mod
from library import db, indexer, inport, jobs, modelcache, search, settings, tags as tags_mod
from library.annotate import annotator_statuses
from library.embed import embedder_status, get_embedder, warm_embedder
from library.models import asset_to_dict
from library.quality import breakdown
from library.vectorstore import get_store

router = APIRouter(prefix="/library", tags=["library"])

# Reads are open to every signed-in user (viewer and up). Writes are gated:
# curators tag, annotate a selection and build datasets; leads own the
# collections themselves — adding, describing, indexing and removing them.
CURATOR = [Depends(require_curator)]
LEAD = [Depends(require_lead)]


# ── Request bodies ────────────────────────────────────────────────────────────

class SourceMetaIn(BaseModel):
    label:       str | None = Field(None, max_length=300)
    year_start:  int | None = Field(None, ge=1800, le=2200)
    year_end:    int | None = Field(None, ge=1800, le=2200)
    survey:      str | None = Field(None, max_length=500)
    region:      str | None = Field(None, max_length=500)
    contact:     str | None = Field(None, max_length=500)
    description: str | None = Field(None, max_length=20000)
    extras:      dict[str, str] | None = None
    inport_url:  str | None = Field(None, max_length=500)

    @field_validator("extras")
    @classmethod
    def _cap_extras(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return value
        if len(value) > 40:
            raise ValueError("At most 40 extra fields.")
        for key, item in value.items():
            if len(key) > 100 or len(item) > 2000:
                raise ValueError("Extra field names are capped at 100 characters, values at 2000.")
        return value


class SourceIn(BaseModel):
    root:  str = Field(..., description="gs://bucket/prefix or a local directory")
    label: str = ""
    tag_pattern: str | None = Field(
        None, description="Regex whose named groups become key:value tags for this source"
    )
    model_ref: str | None = Field(
        None, description="Detection model this collection is scanned with"
    )
    meta: SourceMetaIn | None = None


class IndexIn(BaseModel):
    root:  str = ""
    force: bool = False
    limit: int = 0
    label: str = ""
    prune: bool = True
    tag_pattern: str | None = None
    model_ref: str | None = None
    meta: SourceMetaIn | None = None


class InportLookupIn(BaseModel):
    url: str = Field(..., max_length=500)


class EmbedIn(BaseModel):
    rebuild: bool = False


class AnnotateIn(BaseModel):
    annotator: str = "yolo"
    query:     dict | None = None
    asset_ids: list[int] = Field(default_factory=list)
    # Scan a whole collection without listing its ids.  Falls back to the
    # collection's own configured model when `model` is left blank.
    source_id: int | None = None
    prompts:   list[str] = Field(default_factory=list)
    model:     str = ""
    replace:   bool = True
    limit:     int = 0


class TagIn(BaseModel):
    asset_ids: list[int] = Field(default_factory=list)
    query:     dict | None = None
    names:     list[str]
    kind:      str = "manual"


class DatasetIn(BaseModel):
    name:       str
    query:      dict | None = None
    asset_ids:  list[int] = Field(default_factory=list)
    notes:      str = ""
    split_mode: str = "by_folder"
    ratios:     dict | None = None


class ExportIn(BaseModel):
    kind:           str = "yolo-detect"
    include_images: bool = True
    conf:           float = 0.25
    labels:         list[str] = Field(default_factory=list)
    tag_prefix:     str = ""


class SplitIn(BaseModel):
    asset_ids: list[int]
    split:     str


class SavedQueryIn(BaseModel):
    name:  str
    query: dict


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/status")
def status() -> dict:
    db.init_db()
    embedder_state = embedder_status()
    if embedder_state.get("backend") is None:
        # First call: build the embedder in the background so the banner can
        # say what search modes are live. Never inline: CLIP may download its
        # weights, and this page must answer straight away.
        warm_embedder()
        embedder_state = embedder_status()
    return {
        "stats":      search.stats(),
        "sources":    indexer.list_sources(),
        "embedder":   embedder_state,
        "annotators": annotator_statuses(),
        "jobs":       jobs.list_jobs(8),
        "config": {
            "default_source":  settings.DEFAULT_SOURCE,
            "page_size":       settings.PAGE_SIZE,
            "dupe_distance":   settings.DUPE_DISTANCE,
            "sam3_prompts":    settings.SAM3_PROMPTS,
            "path_tag_pattern": settings.PATH_TAG_PATTERN,
            "signed_urls":     settings.USE_SIGNED_URLS,
            "data_dir":        str(settings.DATA_DIR),
            "yolo_model":      settings.YOLO_MODEL,
            "sam3_model":      settings.SAM3_MODEL,
            "models":          modelcache.cached_models(),
        },
    }


# ── Sources & indexing ────────────────────────────────────────────────────────

@router.get("/sources")
def get_sources() -> dict:
    db.init_db()
    return {"sources": indexer.list_sources()}


@router.post("/sources", dependencies=LEAD)
def add_source(body: SourceIn) -> dict:
    db.init_db()
    if body.model_ref and not modelcache.is_allowed(body.model_ref):
        raise HTTPException(400, f"Model '{body.model_ref}' is not in the allowlist.")
    source_id, root = indexer.ensure_source(
        body.root, body.label, body.tag_pattern, body.model_ref
    )
    if body.meta is not None:
        _apply_meta(source_id, body.meta)
    return {"id": source_id, "root": root}


def _apply_meta(source_id: int, meta: SourceMetaIn) -> dict:
    patch = meta.model_dump(exclude_unset=True)
    if "inport_url" in patch:
        url = (patch["inport_url"] or "").strip()
        if url:
            try:
                patch["inport_url"] = inport.item_url(inport.parse_item_id(url))
            except inport.InportError as exc:
                raise HTTPException(400, str(exc)) from exc
            current = indexer.get_source(source_id)
            if current and current["inport_url"] != patch["inport_url"]:
                patch["inport"] = {}
        else:
            patch["inport_url"] = ""
            patch["inport"] = {}
    for key in ("year_start", "year_end"):
        if key in patch and patch[key] is None:
            patch[key] = 0
    if patch.get("year_start") and patch.get("year_end") and patch["year_start"] > patch["year_end"]:
        raise HTTPException(400, "Start year is after end year.")
    updated = indexer.set_source_meta(source_id, patch)
    if updated is None:
        raise HTTPException(404, f"No collection {source_id}")
    return updated


@router.get("/sources/{source_id}")
def get_source(source_id: int) -> dict:
    db.init_db()
    source = indexer.get_source(source_id)
    if source is None:
        raise HTTPException(404, f"No collection {source_id}")
    return source


@router.patch("/sources/{source_id}/meta", dependencies=LEAD)
def update_source_meta(source_id: int, body: SourceMetaIn) -> dict:
    db.init_db()
    return _apply_meta(source_id, body)


@router.post("/inport/lookup")
def inport_lookup(body: InportLookupIn) -> dict:
    """Preview an InPort record and the form values it suggests.  Nothing is saved."""
    try:
        return inport.lookup(body.url)
    except inport.InportError as exc:
        raise HTTPException(400 if isinstance(exc, inport.InportUrlError) else 502, str(exc)) from exc


@router.post("/sources/{source_id}/inport/refresh", dependencies=LEAD)
def refresh_source_inport(source_id: int) -> dict:
    """Re-fetch the linked InPort record and store its summary."""
    db.init_db()
    source = indexer.get_source(source_id)
    if source is None:
        raise HTTPException(404, f"No collection {source_id}")
    if not source["inport_url"]:
        raise HTTPException(400, "This collection has no InPort link.")
    try:
        result = inport.lookup(source["inport_url"])
    except inport.InportError as exc:
        raise HTTPException(502, str(exc)) from exc
    return indexer.set_source_meta(source_id, {"inport": result["summary"]}) or {}


@router.delete("/sources/{source_id}", dependencies=LEAD)
def remove_source(source_id: int) -> dict:
    """Forget a source and its catalog rows.  Bucket objects are never touched."""
    with db.write() as conn:
        conn.execute("DELETE FROM assets WHERE source_id = ?", (source_id,))
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        conn.execute("DELETE FROM sources_fts WHERE rowid = ?", (source_id,))
    return {"ok": True}


@router.post("/index")
def start_index(body: IndexIn, user: CurrentUser = Depends(require_lead)) -> dict:
    db.init_db()
    root = body.root or settings.DEFAULT_SOURCE
    if not root:
        raise HTTPException(400, "No source given. Set LIB_SOURCE or pass a root.")
    if jobs.active_job("index"):
        raise HTTPException(409, "An index job is already running.")
    if body.model_ref and not modelcache.is_allowed(body.model_ref):
        raise HTTPException(400, f"Model '{body.model_ref}' is not in the allowlist.")
    if body.meta is not None:
        source_id, _ = indexer.ensure_source(root, body.label, body.tag_pattern, body.model_ref)
        _apply_meta(source_id, body.meta)
    job = jobs.start_job(
        "index",
        {"root": root, "force": body.force, "limit": body.limit, "user": user.username},
        lambda j: indexer.index_source(
            j, root, force=body.force, limit=body.limit, label=body.label,
            prune=body.prune, tag_pattern=body.tag_pattern, model_ref=body.model_ref,
        ),
    )
    return job.to_dict()


@router.post("/embed", dependencies=LEAD)
def start_embed(body: EmbedIn) -> dict:
    db.init_db()
    if jobs.active_job("embed"):
        raise HTTPException(409, "An embed job is already running.")
    job = jobs.start_job("embed", body.model_dump(), lambda j: indexer.embed_missing(j, rebuild=body.rebuild))
    return job.to_dict()


@router.post("/annotate")
def start_annotate(body: AnnotateIn, user: CurrentUser = Depends(require_curator)) -> dict:
    db.init_db()

    # SAM 3 is deliberately selection-only. It is an order of magnitude slower
    # than a YOLO pass and needs concepts chosen for the images in front of you,
    # so "segment this collection" is nearly always a mistake rather than an
    # intent — you want it on the handful you are actually looking at.
    if body.annotator == "sam3" and not body.asset_ids:
        raise HTTPException(
            400,
            "SAM 3 runs on a selection, not a whole collection. Select the images "
            "you want segmented, then choose SAM 3 and give it concepts to find.",
        )

    # Scope: explicit ids, a whole collection, or whatever the query matches.
    if body.asset_ids:
        ids = body.asset_ids
    elif body.source_id is not None:
        ids = search.candidate_ids({"source_id": body.source_id, "status": "ok"})
    else:
        ids = search.candidate_ids(body.query or {})
    if body.limit:
        ids = ids[: body.limit]
    if not ids:
        raise HTTPException(400, "Nothing selected to annotate.")
    if jobs.active_job("annotate"):
        raise HTTPException(409, "An annotation job is already running.")

    # A blank model means "use this collection's own", then the global default.
    model_ref = body.model.strip()
    if not model_ref and body.source_id is not None:
        model_ref = indexer.source_model_ref(body.source_id)

    # A model reference names a pickle that torch.load will execute, so the
    # browser may only pick from the server's allowlist.  Operators extend it
    # with LIB_EXTRA_MODELS or by dropping weights in the model directory.
    if model_ref and not modelcache.is_allowed(model_ref):
        raise HTTPException(
            400,
            f"Model '{model_ref}' is not in the allowlist. Choose one of the offered "
            "models, drop the weights into the model directory, or add it to "
            "LIB_EXTRA_MODELS.",
        )

    # Fail fast with the installer/weights message rather than starting a job
    # that dies on its first batch.
    from library.annotate import get_annotator  # noqa: PLC0415

    engine = get_annotator(body.annotator, model_ref or None)
    state = engine.status()
    if not state.available:
        raise HTTPException(400, state.detail)

    job = jobs.start_job(
        "annotate",
        # Report the annotator's own resolved reference: falling back to
        # YOLO_MODEL here labelled SAM 3 runs with a YOLO checkpoint path.
        {"annotator": body.annotator, "count": len(ids), "prompts": body.prompts,
         "model": getattr(engine, "model_ref", model_ref), "source_id": body.source_id,
         "user": user.username},
        lambda j: indexer.annotate_assets(
            j, ids, annotator=body.annotator, prompts=body.prompts or None,
            model_ref=model_ref or None, replace=body.replace,
        ),
    )
    return job.to_dict()


@router.get("/models")
def get_models() -> dict:
    """Models the UI may offer, and which of them are already on disk."""
    return {"models": modelcache.catalog()}


# ── Jobs ──────────────────────────────────────────────────────────────────────

@router.get("/jobs")
def get_jobs(limit: int = 20) -> dict:
    return {"jobs": jobs.list_jobs(limit)}


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"No job {job_id}")
    return job.to_dict()


@router.get("/jobs/{job_id}/stream")
def stream_job(job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"No job {job_id}")
    return StreamingResponse(
        jobs.stream(job),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, user: CurrentUser = Depends(require_curator)) -> dict:
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"No job {job_id}")
    # Whoever started a job can stop it; stopping someone else's needs a lead.
    if job.params.get("user") != user.username and not user.has_role("lead"):
        raise HTTPException(403, "Only the person who started this job, or a lead, can cancel it")
    job.cancel()
    return job.to_dict()


# ── Search ────────────────────────────────────────────────────────────────────

@router.post("/search")
def post_search(query: dict = Body(default_factory=dict)) -> dict:
    db.init_db()
    return search.run(query)


@router.post("/facets")
def post_facets(query: dict = Body(default_factory=dict)) -> dict:
    db.init_db()
    return search.facets(query)


@router.post("/folders")
def post_folders(body: dict = Body(default_factory=dict)) -> dict:
    db.init_db()
    return search.folder_children(body.get("query") or {}, body.get("prefix") or "")


@router.post("/duplicates")
def post_duplicates(body: dict = Body(default_factory=dict)) -> dict:
    db.init_db()
    return {"groups": search.duplicate_groups(body.get("query") or {}, int(body.get("limit") or 60))}


@router.post("/search-by-image")
def search_by_image(file: UploadFile = File(...), query: str = Query("{}")) -> dict:
    """Reverse image search: embed an uploaded file and rank the catalog by it."""
    db.init_db()
    raw = file.file.read()
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            opened.load()
            image = opened.convert("RGB")
    except Exception as exc:
        raise HTTPException(415, f"Could not read that image: {exc}") from exc

    vector = get_embedder().embed_images([image])[0]
    try:
        base = json.loads(query) or {}
    except ValueError:
        base = {}
    return search.run({**base, "vector": vector.tolist(), "similar_to": None, "text": ""})


# ── Assets ────────────────────────────────────────────────────────────────────

@router.get("/asset/{asset_id}")
def get_asset(asset_id: int, neighbours: int = 12) -> dict:
    db.init_db()
    conn = db.connect()
    row = conn.execute(
        """
        SELECT a.*, s.root, s.kind AS source_kind,
               (SELECT GROUP_CONCAT(t.name, char(31)) FROM asset_tags at JOIN tags t ON t.id = at.tag_id
                 WHERE at.asset_id = a.id) AS tags,
               (SELECT GROUP_CONCAT(DISTINCT d.label) FROM detections d WHERE d.asset_id = a.id) AS det_labels
          FROM assets a JOIN sources s ON s.id = a.source_id WHERE a.id = ?
        """,
        (asset_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, f"No asset {asset_id}")

    detail = asset_to_dict(row)
    detail["source_root"] = row["root"]
    detail["source_kind"] = row["source_kind"]
    detail["quality_breakdown"] = breakdown(
        row["blur"], row["brightness"], row["contrast"], row["color_cast"]
    )
    detail["detections"] = [
        {"label": d["label"], "conf": round(d["conf"], 4),
         "box": [d["x"], d["y"], d["w"], d["h"]], "model": d["model"],
         "mask": json.loads(d["mask"]) if d["mask"] else None}
        for d in conn.execute(
            "SELECT label, conf, x, y, w, h, model, mask FROM detections WHERE asset_id = ? "
            "ORDER BY conf DESC", (asset_id,))
    ]
    if neighbours and row["embed_row"] >= 0:
        detail["similar"] = search.run(
            {"similar_to": asset_id, "page_size": neighbours, "sort": "relevance"}
        )["items"]
    else:
        detail["similar"] = []
    return detail


@router.get("/thumb/{asset_id}")
def get_thumb(asset_id: int):
    conn = db.connect()
    row = conn.execute("SELECT thumb_path FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not row or not row["thumb_path"]:
        raise HTTPException(404, "No thumbnail for that asset")
    path = Path(indexer.thumb_abs_path(row["thumb_path"]))
    if not path.exists():
        raise HTTPException(404, "Thumbnail file is missing — re-run indexing")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@router.get("/preview/{asset_id}")
def get_preview(asset_id: int):
    """Mid-size cached JPEG for the detail viewer."""
    try:
        path = indexer.ensure_preview(asset_id)
    except KeyError:
        raise HTTPException(404, f"No asset {asset_id}") from None
    except Exception as exc:
        raise HTTPException(502, f"Could not build a preview: {exc}") from exc
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


@router.get("/image/{asset_id}")
def get_image(asset_id: int):
    """
    Full resolution.  Redirects to a GCS signed URL when signing is enabled
    (keeps the bytes off the app), otherwise proxies them.
    """
    url = indexer.signed_url_for(asset_id)
    if url:
        return RedirectResponse(url, status_code=302)
    try:
        data, mime = indexer.asset_bytes(asset_id)
    except KeyError:
        raise HTTPException(404, f"No asset {asset_id}") from None
    except Exception as exc:
        raise HTTPException(502, f"Could not fetch the source object: {exc}") from exc
    return StreamingResponse(io.BytesIO(data), media_type=mime,
                             headers={"Cache-Control": "private, max-age=3600"})


# ── Tags ──────────────────────────────────────────────────────────────────────

@router.get("/tags")
def get_tags() -> dict:
    db.init_db()
    return {"tags": tags_mod.list_tags(db.connect())}


@router.post("/tags", dependencies=CURATOR)
def add_tags(body: TagIn) -> dict:
    db.init_db()
    ids = body.asset_ids or search.candidate_ids(body.query or {})
    if not ids:
        raise HTTPException(400, "Nothing selected to tag.")
    with db.write() as conn:
        added = tags_mod.add_tags(conn, ids, body.names, kind=body.kind, origin="manual")
    return {"assets": len(ids), "pairs": added}


# POST rather than DELETE: a DELETE body is legal but is dropped by enough
# proxies and clients that it is not worth the ambiguity.
@router.post("/tags/remove", dependencies=CURATOR)
def remove_tags(body: TagIn) -> dict:
    db.init_db()
    ids = body.asset_ids or search.candidate_ids(body.query or {})
    if not ids:
        raise HTTPException(400, "Nothing selected.")
    with db.write() as conn:
        removed = tags_mod.remove_tags(conn, ids, body.names)
    return {"assets": len(ids), "removed": removed}


# ── Saved queries ─────────────────────────────────────────────────────────────

@router.get("/saved-queries")
def get_saved_queries() -> dict:
    db.init_db()
    rows = db.connect().execute("SELECT * FROM saved_queries ORDER BY created_at DESC").fetchall()
    return {"queries": [
        {"id": r["id"], "name": r["name"], "query": json.loads(r["query_json"]), "created_at": r["created_at"]}
        for r in rows
    ]}


@router.post("/saved-queries", dependencies=CURATOR)
def save_query(body: SavedQueryIn) -> dict:
    import time  # noqa: PLC0415

    db.init_db()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO saved_queries(name, query_json, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET query_json=excluded.query_json, created_at=excluded.created_at",
            (body.name, json.dumps(body.query), time.time()),
        )
    return {"ok": True, "name": body.name}


@router.delete("/saved-queries/{query_id}", dependencies=CURATOR)
def delete_saved_query(query_id: int) -> dict:
    with db.write() as conn:
        conn.execute("DELETE FROM saved_queries WHERE id = ?", (query_id,))
    return {"ok": True}


# ── Datasets ──────────────────────────────────────────────────────────────────

@router.get("/datasets")
def get_datasets() -> dict:
    db.init_db()
    return {"datasets": datasets_mod.list_datasets()}


@router.post("/datasets", dependencies=CURATOR)
def create_dataset(body: DatasetIn) -> dict:
    db.init_db()
    try:
        return datasets_mod.create(
            body.name, query=body.query, asset_ids=body.asset_ids or None, notes=body.notes,
            split_mode=body.split_mode, ratios=body.ratios,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: int) -> dict:
    db.init_db()
    try:
        return datasets_mod.describe(dataset_id)
    except KeyError:
        raise HTTPException(404, f"No dataset {dataset_id}") from None


@router.get("/datasets/{dataset_id}/items")
def get_dataset_items(dataset_id: int, split: str = "", limit: int = 200, offset: int = 0) -> dict:
    db.init_db()
    return datasets_mod.items(dataset_id, split, limit, offset)


@router.post("/datasets/{dataset_id}/split", dependencies=CURATOR)
def update_split(dataset_id: int, body: SplitIn) -> dict:
    db.init_db()
    try:
        return {"updated": datasets_mod.set_split(dataset_id, body.asset_ids, body.split)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/datasets/{dataset_id}", dependencies=CURATOR)
def delete_dataset(dataset_id: int) -> dict:
    datasets_mod.delete(dataset_id)
    return {"ok": True}


@router.post("/datasets/{dataset_id}/export")
def export_dataset(
    dataset_id: int, body: ExportIn, user: CurrentUser = Depends(require_curator),
) -> dict:
    db.init_db()
    try:
        datasets_mod.describe(dataset_id)
    except KeyError:
        raise HTTPException(404, f"No dataset {dataset_id}") from None
    job = jobs.start_job(
        "export",
        {"dataset_id": dataset_id, **body.model_dump(), "user": user.username},
        lambda j: datasets_mod.export(
            j, dataset_id, kind=body.kind, include_images=body.include_images,
            conf=body.conf, labels=body.labels or None, tag_prefix=body.tag_prefix,
        ),
    )
    return job.to_dict()


@router.get("/exports")
def list_exports() -> dict:
    settings.ensure_dirs()
    files = sorted(settings.EXPORT_DIR.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {"exports": [
        {"file": p.name, "bytes": p.stat().st_size, "mtime": p.stat().st_mtime,
         "url": f"/api/library/exports/{p.name}"}
        for p in files[:50]
    ]}


@router.get("/exports/{filename}")
def download_export(filename: str):
    # Resolve inside the export dir so a crafted name cannot escape it.
    target = (settings.EXPORT_DIR / filename).resolve()
    if not str(target).startswith(str(settings.EXPORT_DIR.resolve())) or not target.is_file():
        raise HTTPException(404, "No such export")
    return FileResponse(target, media_type="application/zip", filename=target.name)


# ── Maintenance ───────────────────────────────────────────────────────────────

@router.get("/vectors")
def vector_stats() -> dict:
    return get_store().stats()
