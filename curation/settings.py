"""
Curation settings. The defaults point at NOAA PIFSC's public CoralNet mirror,
which is what this tool was built around; every one is overridable so the same
code curates another programme's data.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from library import settings as library_settings

_HERE = Path(__file__).resolve().parent
_PIFSC = "gs://nmfs_odp_pifsc/PIFSC/ESD/ARP/pifsc-ai-data-repository/coralnet_mirror"

DEFAULT_IMPORT_SOURCES = [
    {
        "name": "Hawaii",
        "root_uri": f"{_PIFSC}/ESD_REA_HAWAII_v2/",
        "csv_uri": f"{_PIFSC}/ESD_REA_HAWAII_v2/ESD_REA_HAWAII_v2_annotations.csv",
    },
    {
        "name": "Mariana",
        "root_uri": f"{_PIFSC}/ESD_REA_MARIANA_v2/",
        "csv_uri": f"{_PIFSC}/ESD_REA_MARIANA_v2/ESD_REA_MARIANA_v2_annotations.csv",
    },
    {
        "name": "Samoa/PRIA",
        "root_uri": f"{_PIFSC}/ESD_REA_SAMOA_PRIA_v2/",
        "csv_uri": f"{_PIFSC}/ESD_REA_SAMOA_PRIA_v2/ESD_REA_SAMOA_PRIA_v2_annotation.csv",
    },
]


def taxonomy_file() -> Path:
    return Path(os.getenv("CURATION_TAXONOMY_FILE", str(_HERE / "data" / "taxonomy_tree.json")))


def eval_dir() -> Path:
    """Where pinned model-evaluation artifacts live (``<dir>/v0/*.json`` etc.)."""
    return Path(os.getenv("CURATION_EVAL_DIR", str(library_settings.DATA_DIR / "curation_eval")))


def import_sources() -> list[dict]:
    raw = os.getenv("CURATION_IMPORT_SOURCES", "").strip()
    if not raw:
        return [dict(source) for source in DEFAULT_IMPORT_SOURCES]
    sources = json.loads(raw)
    if not isinstance(sources, list):
        raise ValueError("CURATION_IMPORT_SOURCES must be a JSON list of {name, root_uri, csv_uri}")
    return sources


def t1_crop_root() -> str:
    return os.getenv("CURATION_T1_CROP_ROOT", f"{_PIFSC}/pacific_t1/").rstrip("/") + "/"


def t3_all_root() -> str:
    """Canonical per-class crop folder tried when an item's own crop is missing."""
    return os.getenv("CURATION_T3_ALL_ROOT", f"{_PIFSC}/pacific_all/").rstrip("/") + "/"


def split_root() -> str:
    """Root of the train/val/test class folders the pinned evaluation used."""
    return os.getenv("CURATION_SPLIT_ROOT", f"{_PIFSC}/pacific_t3_splits").rstrip("/")


def extra_image_buckets() -> set[str]:
    raw = os.getenv("CURATION_IMAGE_BUCKETS", "")
    return {bucket.strip() for bucket in raw.split(",") if bucket.strip()}


def allow_local_sources() -> bool:
    """Imports may read CSVs from the server's own disk only when this is on.

    Off in a deployment: otherwise anyone who can start an import could make
    the server read any file it can see.
    """
    return os.getenv("CURATION_ALLOW_LOCAL_SOURCES", "").strip().lower() in {"1", "true", "yes", "on"}


def versions_dir() -> Path:
    """Where published versions' manifests (and optional image folders) are written."""
    return Path(os.getenv("CURATION_VERSIONS_DIR", str(library_settings.DATA_DIR / "curation_versions")))


def publish_uri() -> str:
    """Optional gs:// prefix that published versions are also copied to (needs write access)."""
    return os.getenv("CURATION_PUBLISH_URI", "").strip()


def split_seed() -> int:
    try:
        return int(os.getenv("CURATION_SPLIT_SEED", "42"))
    except ValueError:
        return 42


def split_ratios() -> tuple[float, float, float]:
    """train/val/test fractions, e.g. CURATION_SPLIT_RATIOS=0.8,0.1,0.1."""
    try:
        parts = [float(v) for v in os.getenv("CURATION_SPLIT_RATIOS", "0.8,0.1,0.1").split(",")]
        if len(parts) == 3 and all(v >= 0 for v in parts) and sum(parts) > 0:
            total = sum(parts)
            return (parts[0] / total, parts[1] / total, parts[2] / total)
    except ValueError:
        pass
    return (0.8, 0.1, 0.1)


def rare_min() -> int:
    """CURATION_RARE_MIN: a class (or region…) is rare when val or test holds fewer items than this."""
    try:
        return max(0, int(os.getenv("CURATION_RARE_MIN", "10")))
    except ValueError:
        return 10


def seed_noaa_pacific() -> bool:
    """Create and import the NOAA Pacific T3 project on first start (CURATION_SEED=noaa-pacific)."""
    return os.getenv("CURATION_SEED", "").strip().lower() in {"noaa-pacific", "noaa", "yes", "on", "1", "true"}


_WARNED: set = set()


def starter_data() -> str:
    """What a start-up pulls in when it is missing: "off" (default), "noaa-t3", "regional" or "hub".

    Off unless asked for: the compose files and the installer opt in with
    CURATION_STARTER_DATA=noaa-t3. When it is unset the older
    CURATION_SEED=noaa-pacific still means the regional bucket project alone;
    an empty CURATION_SEED (an installer's "no") means off. An unknown value
    is reported once and means off.
    """
    raw = os.getenv("CURATION_STARTER_DATA", "").strip().lower()
    if not raw:
        return "regional" if seed_noaa_pacific() else "off"
    if raw in {"noaa-t3", "all", "on", "yes", "1", "true"}:
        return "noaa-t3"
    if raw in {"regional", "hub"}:
        return raw
    if raw not in {"off", "no", "none", "false", "0"} and raw not in _WARNED:
        _WARNED.add(raw)
        logging.getLogger(__name__).warning(
            "CURATION_STARTER_DATA=%r is not one of noaa-t3, regional, hub, off: starter data is off", raw)
    return "off"


def evaluation_spool() -> Path:
    return Path(os.getenv("CURATION_EVALUATION_SPOOL", str(eval_dir() / "worker")))


def trusted_checkpoints() -> bool:
    return os.getenv("CURATION_TRUSTED_CHECKPOINTS", "").lower() in {"1", "true", "yes", "on"}


def allow_local_models() -> bool:
    return os.getenv("CURATION_ALLOW_LOCAL_MODELS", "").lower() in {"1", "true", "yes", "on"}


def model_upload_limit() -> int:
    return max(1, min(int(os.getenv("CURATION_MODEL_MAX_BYTES", "1073741824")), 2147483647))


def hf_allowed_orgs() -> set[str]:
    """Optional CURATION_HF_ALLOWED_ORGS=NMFS-OSI,other: only these owners may be registered."""
    return {org.strip().lower() for org in os.getenv("CURATION_HF_ALLOWED_ORGS", "").split(",") if org.strip()}


def hf_endpoint() -> str:
    return os.getenv("CURATION_HF_ENDPOINT", "https://huggingface.co").rstrip("/")


def hub_cache_dir() -> Path:
    """Where pinned Hugging Face files (parquet, weights) are cached."""
    return Path(os.getenv("CURATION_HUB_CACHE", str(eval_dir() / "hub")))


def hub_max_bytes() -> int:
    """Largest single Hugging Face file the server will download (default 2 GiB)."""
    try:
        return max(1, int(os.getenv("CURATION_HUB_MAX_BYTES", str(2 * 1024 ** 3))))
    except ValueError:
        return 2 * 1024 ** 3


def evaluation_fetch_workers() -> int:
    """Patches fetched at once while staging an evaluation (CURATION_EVAL_FETCH_WORKERS, default 32).

    Patches are small (~100 KB); a sequential fetch spends most of its time
    waiting on each request, so parallel reads are what makes 100k-image
    splits practical.
    """
    try:
        return max(1, min(64, int(os.getenv("CURATION_EVAL_FETCH_WORKERS", "32"))))
    except ValueError:
        return 32


def evaluation_max_missing() -> float:
    """Largest share of patches an evaluation may leave unscored before it fails (default 5%)."""
    try:
        return max(0.0, min(1.0, float(os.getenv("CURATION_EVAL_MAX_MISSING", "0.05"))))
    except ValueError:
        return 0.05


def patch_cache_dir() -> Path | None:
    """Patches downloaded for evaluations, kept so the next run reuses them. 'off' disables.

    Keyed by URI (a patch cut from its source also by its geometry) and never
    revalidated: delete the directory if objects at the same URIs are replaced.
    """
    raw = os.getenv("CURATION_PATCH_CACHE", "").strip()
    if raw.lower() in {"off", "0", "no", "false"}:
        return None
    return Path(raw) if raw else eval_dir() / "patch_cache"


def image_cache_dir() -> Path:
    """CURATION_IMAGE_CACHE: local disk cache of source images and cut patches."""
    return Path(os.getenv("CURATION_IMAGE_CACHE", str(library_settings.DATA_DIR / "curation_cache")))


def image_cache_mb() -> int:
    """CURATION_IMAGE_CACHE_MB: total size cap for that cache (default 2048)."""
    try:
        return max(0, int(os.getenv("CURATION_IMAGE_CACHE_MB", "2048")))
    except ValueError:
        return 2048


def create_role() -> str:
    """CURATION_CREATE_ROLE: the lowest role that may add things (default curator).

    Covers registering Hugging Face datasets and models, creating projects,
    importing, registering and validating evaluation models, and running
    evaluations. Deleting projects, publishing versions and applying a link to
    an existing project stay with leads whatever this says. An unknown value
    falls back to lead rather than opening anything up.
    """
    from auth.models import ROLE_RANK  # noqa: PLC0415

    role = os.getenv("CURATION_CREATE_ROLE", "curator").strip().lower() or "curator"
    return role if role in ROLE_RANK else "lead"


def background_workers() -> bool:
    """OCEANFRAME_BACKGROUND_WORKERS=off: serve requests only; another process coordinates.

    One coordinator per database. A second app process on the same database
    (the T3 review) must not start the import and evaluation pools, or run
    their start-up recovery: it would mark the main app's live imports,
    version builds and evaluations as interrupted, and claim its queued jobs.
    """
    return os.getenv("OCEANFRAME_BACKGROUND_WORKERS", "on").strip().lower() not in {"off", "0", "no", "false"}
