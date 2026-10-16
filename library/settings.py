"""
Environment-driven settings for the image library.

Everything is overridable with a ``LIB_`` prefixed environment variable so the
same image runs locally, in Docker, and on a Cloud Workstation without a config
file.
"""
from __future__ import annotations

import os
from pathlib import Path

from config import BASE_DIR


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


# ── Paths ─────────────────────────────────────────────────────────────────────

DATA_DIR   = Path(_env("LIB_DATA_DIR", str(BASE_DIR / "library_data")))
DB_PATH    = DATA_DIR / "catalog.sqlite"
VEC_PATH   = DATA_DIR / "vectors.f32"
VEC_META   = DATA_DIR / "vectors.json"
THUMB_DIR  = DATA_DIR / "thumbs"
PREVIEW_DIR = DATA_DIR / "previews"
EXPORT_DIR = DATA_DIR / "exports"


def ensure_dirs() -> None:
    for path in (DATA_DIR, THUMB_DIR, PREVIEW_DIR, EXPORT_DIR):
        path.mkdir(parents=True, exist_ok=True)


# ── Source ────────────────────────────────────────────────────────────────────

# Default source to offer in the UI, e.g. "gs://my-survey-bucket/2024" or a
# local directory.  Blank means "ask the user".
DEFAULT_SOURCE = _env("LIB_SOURCE", "")

# How to authenticate to GCS:
#   auto — Application Default Credentials, falling back to an anonymous client
#          so public buckets (NOAA's open-data buckets, for one) just work
#   yes  — always anonymous
#   no   — require ADC and fail loudly when it is missing
GCS_ANONYMOUS = _env("LIB_GCS_ANONYMOUS", "auto")

# Compared case-insensitively by the storage backends, which matters: NOAA's
# camera output is ".JPG" and ".PNG" while its ML datasets are ".jpg".
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}

# Crawl limits.  0 = unlimited.
MAX_ASSETS      = _env_int("LIB_MAX_ASSETS", 0)
CRAWL_WORKERS   = _env_int("LIB_CRAWL_WORKERS", 8)
INDEX_BATCH     = _env_int("LIB_INDEX_BATCH", 32)

# ── Thumbnails ────────────────────────────────────────────────────────────────

THUMB_WIDTH   = _env_int("LIB_THUMB_WIDTH", 320)
THUMB_QUALITY = _env_int("LIB_THUMB_QUALITY", 78)

# Detail-view preview.  Serving the original there means pulling a 13 MB
# 6000x4000 photogrammetry frame through the app on every click — measured at
# 7s — so the viewer gets a cached downscale instead and the original stays one
# click away behind "Open full size".
PREVIEW_MAX_EDGE = _env_int("LIB_PREVIEW_MAX_EDGE", 1600)
PREVIEW_QUALITY  = _env_int("LIB_PREVIEW_QUALITY", 82)
# Longest edge the pipeline decodes to before metrics/embedding.  Keeps memory
# bounded on 8000px drone frames.
WORK_MAX_EDGE = _env_int("LIB_WORK_MAX_EDGE", 1024)

# ── Embeddings ────────────────────────────────────────────────────────────────

# "clip" (semantic text+image search, needs torch) or "hash" (no deps).
# "auto" picks clip when importable, otherwise hash.
EMBED_BACKEND = _env("LIB_EMBED_BACKEND", "auto")
CLIP_MODEL    = _env("LIB_CLIP_MODEL", "ViT-B-32")
# A pretrained tag, or "random" for an untrained model.  Empty is rejected:
# open_clip silently initialises randomly and only logs a warning, which turns
# every embedding meaningless without anything failing.
CLIP_PRETRAIN = _env("LIB_CLIP_PRETRAINED", "laion2b_s34b_b79k")
CLIP_HF_MODEL = _env("LIB_CLIP_HF_MODEL", "openai/clip-vit-base-patch32")
CLIP_DEVICE   = _env("LIB_CLIP_DEVICE", "auto")
EMBED_BATCH   = _env_int("LIB_EMBED_BATCH", 16)

# Rows scored per chunk when scanning the full matrix.  Tune down on tiny VMs.
VEC_CHUNK_ROWS = _env_int("LIB_VEC_CHUNK_ROWS", 65536)
# Above this many rows an unfiltered query will use hnswlib when installed.
ANN_MIN_ROWS   = _env_int("LIB_ANN_MIN_ROWS", 200_000)

# ── Models ────────────────────────────────────────────────────────────────────

# Where gs:// model weights are cached, and where you drop your own .pt files.
MODEL_DIR   = Path(_env("LIB_MODEL_DIR", str(BASE_DIR / "models")))

# A bare name lets Ultralytics fetch it; a gs:// URI is downloaded into
# MODEL_DIR.  The COCO default is a placeholder — see library/modelcache.py for
# why a domain checkpoint matters on underwater imagery.
YOLO_MODEL  = _env("LIB_YOLO_MODEL", "yolo11n.pt")
YOLO_CONF   = _env_float("LIB_YOLO_CONF", 0.25)
YOLO_IMGSZ  = _env_int("LIB_YOLO_IMGSZ", 640)
SAM3_MODEL  = _env("LIB_SAM3_MODEL", "sam3.pt")
SAM3_CONF   = _env_float("LIB_SAM3_CONF", 0.25)
# Default noun phrases offered in the SAM 3 prompt box.
SAM3_PROMPTS = [
    p.strip() for p in _env("LIB_SAM3_PROMPTS", "fish,coral,diver,sea urchin,algae").split(",") if p.strip()
]

# Extra model references to offer in the UI, comma separated.  This is an env
# var and therefore trusted: a model reference names a pickle that torch.load
# will execute, so the browser may only choose from the resulting allowlist and
# never supply a path of its own.
EXTRA_MODELS = _env("LIB_EXTRA_MODELS", "")

# ── Search / dedupe ───────────────────────────────────────────────────────────

DUPE_DISTANCE = _env_int("LIB_DUPE_DISTANCE", 6)   # phash Hamming, 0-64
PAGE_SIZE     = _env_int("LIB_PAGE_SIZE", 120)
MAX_PAGE_SIZE = _env_int("LIB_MAX_PAGE_SIZE", 500)

# ── Folder → tag rules ────────────────────────────────────────────────────────

# Regex with named groups applied to each asset's path relative to the source
# root.  Every group that matches becomes a "name:value" tag of kind "path".
# Example: (?P<year>\d{4})/(?P<site>[^/]+)/(?P<transect>T\d+)
PATH_TAG_PATTERN = _env("LIB_PATH_TAG_PATTERN", "")

# ── Serving ───────────────────────────────────────────────────────────────────

# When true, full-resolution views use a GCS V4 signed URL (needs a service
# account with a private key or the IAM Credentials API).  When false the app
# proxies the bytes itself, which always works but costs app bandwidth.
USE_SIGNED_URLS = _env_bool("LIB_SIGNED_URLS", False)
SIGNED_URL_TTL  = _env_int("LIB_SIGNED_URL_TTL", 900)
