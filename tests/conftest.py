"""
Shared fixtures.

Every test runs against a throwaway ``LIB_DATA_DIR``, so the suite never touches
a real catalog and the modules that cache a connection or a memmap are reset
between tests.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A fresh, isolated library package rooted at a temp directory."""
    monkeypatch.setenv("LIB_DATA_DIR", str(tmp_path / "data"))
    # Default to the dependency-free descriptor. The CLIP module raises this to
    # "clip" for its own duration via a module-scoped MonkeyPatch, which is
    # applied before this fixture runs and undone after.
    monkeypatch.setenv("LIB_EMBED_BACKEND", os.getenv("LIB_EMBED_BACKEND", "hash"))
    monkeypatch.setenv("LIB_PATH_TAG_PATTERN", r"(?P<year>\d{4})/(?P<site>[^/]+)")

    from library import settings

    # settings caches the env at import time, and the modules below capture
    # values from it, so both have to be reloaded for the temp dir to take.
    importlib.reload(settings)
    for name in ("library.db", "library.vectorstore", "library.indexer",
                 "library.search", "library.datasets", "library.tags", "library.embed"):
        module = sys.modules.get(name)
        if module:
            importlib.reload(module)

    from library import db, embed, vectorstore

    db.close()
    vectorstore.reset_store()
    embed.reset_embedder()
    db.init_db()

    # The application database (users, curation). SQLite in the temp dir unless
    # the run points OCEANFRAME_DB_URL elsewhere — the test-oracle compose
    # service runs this same suite against Oracle that way.
    if not os.getenv("OCEANFRAME_TEST_DB_URL"):
        monkeypatch.delenv("OCEANFRAME_DB_URL", raising=False)
    else:
        monkeypatch.setenv("OCEANFRAME_DB_URL", os.environ["OCEANFRAME_TEST_DB_URL"])
    monkeypatch.delenv("OCEANFRAME_AUTH", raising=False)
    _reset_app_db()

    yield tmp_path

    db.close()
    from appdb import reset_engine
    reset_engine()


def _reset_app_db():
    """Fresh application schema for each test, on whichever backend is configured."""
    from appdb import Base, get_engine, reset_engine
    from appdb.bootstrap import _import_models, _ledger_meta, init_app_db
    from auth import service
    from routers import auth as auth_router

    reset_engine()
    service.reset_cache()
    auth_router.reset_rate_limit()
    _import_models()
    engine = get_engine()
    if engine.dialect.name != "sqlite":
        # A shared server database: drop what the previous test left behind —
        # which is exactly why this refuses anything that is not plainly a test
        # database. Pointed at a real stack, it would erase every user and
        # every curation decision.
        if "test" not in (engine.url.username or "").lower():
            raise RuntimeError(
                f"Refusing to reset {engine.url.render_as_string(hide_password=True)}: the "
                "database user must contain 'test' (see the test-oracle service)."
            )
        Base.metadata.drop_all(engine)
        _ledger_meta.drop_all(engine)
    init_app_db()


def make_image(size=(320, 240), fill=(20, 90, 140), blobs=0, blur=0.0, scale=1.0):
    """A deterministic test image with controllable sharpness and content."""
    img = Image.new("RGB", size, fill)
    draw = ImageDraw.Draw(img)
    for i in range(blobs):
        x = 20 + (i * 47) % max(size[0] - 60, 1)
        y = 15 + (i * 31) % max(size[1] - 50, 1)
        draw.ellipse([x, y, x + 34, y + 22], fill=(250, 210, 60))
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    if scale != 1.0:
        img = img.point(lambda v: max(0, min(255, int(v * scale))))
    return img


@pytest.fixture
def tree(tmp_path):
    """A small folder tree of images with a known shape."""
    root = tmp_path / "survey"
    layout = {
        "2024/kaneohe": [("sharp_a.jpg", dict(blobs=6)), ("sharp_b.jpg", dict(blobs=4))],
        "2024/hanauma": [("soft.jpg", dict(blobs=5, blur=5.0))],
        "2023/kaneohe": [("dark.jpg", dict(blobs=3, scale=0.15))],
    }
    for folder, files in layout.items():
        target = root / folder
        target.mkdir(parents=True, exist_ok=True)
        for name, kwargs in files:
            make_image(**kwargs).save(target / name, quality=90)
    # A near-duplicate of sharp_a, the way burst-mode shooting produces them.
    make_image(blobs=6).filter(ImageFilter.GaussianBlur(0.3)).save(
        root / "2024/kaneohe" / "sharp_a_dup.jpg", quality=90
    )
    return root


@pytest.fixture(autouse=True)
def _isolated_image_cache(tmp_path, monkeypatch):
    """Curation image reads are cached on disk; keep that cache per test."""
    monkeypatch.setenv("CURATION_IMAGE_CACHE", str(tmp_path / "image_cache"))
    try:
        from curation import image_cache
    except Exception:  # curation not importable in this environment
        yield
        return
    image_cache.clear()
    yield
    image_cache.clear()


@pytest.fixture(autouse=True)
def _no_starter_data(monkeypatch):
    """Start-up must not pull the NOAA T3 data from the network during tests."""
    monkeypatch.setenv("CURATION_STARTER_DATA", "off")
