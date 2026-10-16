"""
OceanFrame Web — FastAPI entry point.

Two surfaces share one deployment:

* ``/``        — the frame analyser: upload a video or image sequence, score
                 every frame, export the keepers.
* ``/library`` — the image library: index a GCS bucket (or a local tree),
                 search it by similarity, tag it, and build datasets.

With ``OCEANFRAME_AUTH=on`` every page and API needs a login (see ``auth/``);
off, the app is open and runs everything as a built-in local user.
"""
import logging
import os
import time
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import THUMBNAIL_DIR, UPLOAD_DIR, STATIC_DIR, TEMPLATE_DIR
from appdb.bootstrap import init_app_db
from appdb.engine import backend_name, describe
from auth import config as auth_config
from auth import service as auth_service
from auth.deps import require_admin
from auth.middleware import AuthMiddleware, safe_next
from curation import settings as curation_settings
from curation.importer import curation_import_pool
from curation.evaluation_runner import evaluation_pool
from curation.registry_worker import registry_worker
from curation import starter_data
from library import jobs as library_jobs
from library import settings as library_settings
from routers import auth, collab, curation, export, library, registry, releases, stream, upload

logger = logging.getLogger("oceanframe")

UPLOAD_DIR.mkdir(exist_ok=True)
THUMBNAIL_DIR.mkdir(parents=True, exist_ok=True)
library_settings.ensure_dirs()

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Any job still marked running in the catalog died with the previous
    # process; say so rather than leaving a permanent phantom progress bar.
    library_jobs.mark_interrupted()
    # Schema, migrations, the bootstrap admin. In the container the entrypoint
    # has already done this once the database answered; here it is a no-op
    # check, and it is what sets things up for a plain `python launch.py`.
    await run_in_threadpool(init_app_db)
    logger.info("App database: %s", describe())
    if not auth_config.load().enabled and backend_name() == "oracle":
        logger.warning("Sign-in is off: everyone who can reach this app acts as an admin on the shared database")
    # Curation CSV imports run on one worker thread, claimed from the database.
    # One coordinator per database: a second app on the same Oracle (the T3
    # review) runs with OCEANFRAME_BACKGROUND_WORKERS=off.
    workers = curation_settings.background_workers()
    if workers:
        curation_import_pool.start()
        evaluation_pool.start()
        registry_worker.start()
        # Starter data (NOAA T3) fills in behind the app; coordinator only, never blocks start-up.
        starter_data.start_background()
    else:
        logger.info("Background workers are off: imports and evaluations run in another process")
    yield
    if workers:
        await run_in_threadpool(starter_data.stop_background)
        await run_in_threadpool(registry_worker.stop)
        await run_in_threadpool(evaluation_pool.stop)
        await run_in_threadpool(curation_import_pool.stop, 10.0)


app = FastAPI(title="OceanFrame", docs_url=None, redoc_url=None, lifespan=lifespan)

app.add_middleware(AuthMiddleware)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATE_DIR)
# Cache-busting for first-party CSS/JS: a deploy never pairs new HTML with stale assets.
def _asset_version() -> str:
    """APP_VERSION, else the newest static file mtime (same for every worker), in integer seconds."""
    env = os.getenv("APP_VERSION")
    if env:
        return env
    latest = 0
    for path in Path(STATIC_DIR).rglob("*"):
        try:
            if path.is_file():
                latest = max(latest, int(path.stat().st_mtime))
        except OSError:
            continue
    return str(latest or int(time.time()))


templates.env.globals["asset_v"] = _asset_version()

app.include_router(upload.router, prefix="/api")
app.include_router(stream.router, prefix="/api")
app.include_router(export.router, prefix="/api")
app.include_router(library.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(curation.router, prefix="/api")
app.include_router(collab.router, prefix="/api")
app.include_router(releases.router, prefix="/api")
app.include_router(registry.router, prefix="/api")


@app.get("/healthz")
async def healthz() -> dict:
    """
    Liveness probe for container orchestration.

    Deliberately touches neither the catalog nor the models: a health check that
    opens SQLite would report unhealthy during a long write, and one that loads
    CLIP would never answer inside the start period.

    Names the configured app database without connecting to it.
    """
    return {"status": "ok", "app_db": backend_name()}


@app.get("/")
async def index(request: Request):
    """The library is the landing page — it is where most of the work happens."""
    return templates.TemplateResponse(
        request=request,
        name="library.html",
        context={"default_source": library_settings.DEFAULT_SOURCE},
    )


# Kept so older links, bookmarks and the docs still land in the right place.
@app.get("/library")
async def library_alias() -> RedirectResponse:
    return RedirectResponse("/", status_code=308)


@app.get("/frames")
async def frames_page(request: Request):
    """The original video/image-sequence frame analyser."""
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/collections")
async def collections_page(request: Request):
    return templates.TemplateResponse(request=request, name="collections.html")


@app.get("/collections/new")
async def collection_new_page(request: Request):
    return templates.TemplateResponse(
        request=request, name="collection_form.html", context={"source_id": ""},
    )


@app.get("/collections/{source_id}/edit")
async def collection_edit_page(request: Request, source_id: int):
    return templates.TemplateResponse(
        request=request, name="collection_form.html", context={"source_id": source_id},
    )


@app.get("/curation")
async def curation_page(request: Request):
    """Classification curation: interactive confusion matrix, relabelling, review."""
    return templates.TemplateResponse(request=request, name="curation.html",
                                      context={"app_db": backend_name(),
                                               "split_root": curation_settings.split_root(),
                                               "create_role": curation_settings.create_role()})


# ── Login and accounts ──────────────────────────────────────────────────────────

@app.get("/login")
async def login_page(request: Request, next: str = "/curation"):
    target = safe_next(next)
    if not auth_config.load().enabled:
        return RedirectResponse(target, status_code=303)
    cfg = auth_config.load()
    return templates.TemplateResponse(request=request, name="login.html", context={
        "next": target, "allow_signup": cfg.allow_signup, "signup_role": cfg.signup_role,
    })


@app.get("/logout")
async def logout_page(request: Request) -> RedirectResponse:
    cfg = auth_config.load()
    await run_in_threadpool(auth_service.revoke_token, request.cookies.get(cfg.cookie_name))
    response = RedirectResponse("/login" if cfg.enabled else "/", status_code=303)
    response.delete_cookie(cfg.cookie_name, path="/")
    return response


@app.get("/account")
async def account_page(request: Request):
    return templates.TemplateResponse(request=request, name="account.html")


@app.get("/users", dependencies=[Depends(require_admin)])
async def users_page(request: Request):
    return templates.TemplateResponse(request=request, name="users.html")
