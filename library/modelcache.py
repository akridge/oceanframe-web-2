"""
Resolve a model reference to a local file.

Domain models matter more than convenience here.  A COCO-pretrained checkpoint
is worse than useless on reef imagery — it labels fish "airplane" and coral
"skateboard" with high confidence — so the fix is to point the annotator at a
model trained on this domain.  NOAA publishes one in the same open bucket as
the imagery::

    LIB_YOLO_MODEL=gs://nmfs_odp_pifsc/PIFSC/ESD/ARP/pifsc-ai-data-repository/models/yolo11-esa-icra-detector.pt

so this module accepts a ``gs://`` reference and caches the weights on disk.
"""
from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path

from library import settings

log = logging.getLogger(__name__)
_lock = threading.Lock()
_warned = False


def _cache_dirs() -> tuple[Path, ...]:
    """Every directory a cached weight file may sit in, in lookup order."""
    return (
        settings.MODEL_DIR,
        settings.DATA_DIR / "models",
        Path(tempfile.gettempdir()) / "oceanframe-models",
    )


def _is_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".oceanframe-write-test"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def _writable_cache_dir() -> Path:
    """
    Where downloaded weights are written.

    ``LIB_MODEL_DIR`` is frequently a read-only or differently-owned mount — a
    bind-mounted ./models owned by the host user while the container runs as
    another uid is the common case — so fall back to the data volume, which
    survives the container being recreated, and only then to a temp dir.
    Weights already present in the configured directory are still used from
    there; only downloads move.
    """
    global _warned
    for directory in (settings.MODEL_DIR, settings.DATA_DIR / "models"):
        if _is_writable(directory):
            return directory
    fallback = Path(tempfile.gettempdir()) / "oceanframe-models"
    fallback.mkdir(parents=True, exist_ok=True)
    if not _warned:
        _warned = True
        log.warning(
            "Neither %s nor %s is writable; caching downloaded weights in %s instead. "
            "They will be re-downloaded when the container restarts.",
            settings.MODEL_DIR, settings.DATA_DIR / "models", fallback,
        )
    return fallback


NOAA_ICRA = (
    "gs://nmfs_odp_pifsc/PIFSC/ESD/ARP/pifsc-ai-data-repository/models/"
    "yolo11-esa-icra-detector.pt"
)

# Models the UI is allowed to offer and the API is allowed to load.
#
# This list is the security boundary as much as it is a convenience: a model
# reference names a pickle that gets unpickled by torch.load, so accepting an
# arbitrary one from a request body is remote code execution.  Operators can
# extend the list with LIB_EXTRA_MODELS (an env var, therefore trusted); the
# browser can only pick from what is already here.
BUILTIN_MODELS: list[dict] = [
    {
        "ref": "yolo11n.pt", "kind": "yolo", "label": "YOLO11n — COCO (general)",
        "note": "80 everyday classes. Confidently wrong on reef imagery: it labels "
                "fish 'airplane' and coral 'skateboard'. Useful only as a smoke test.",
    },
    {
        "ref": "yolo11s.pt", "kind": "yolo", "label": "YOLO11s — COCO (general)",
        "note": "Larger COCO model. Same domain mismatch as YOLO11n.",
    },
    {
        "ref": NOAA_ICRA, "kind": "yolo", "label": "NOAA ESA/ICRA coral detector",
        "note": "Trained by PIFSC on this imagery. Detects Isopora crateriformis "
                "(ESA-listed). The right default for coral collections.",
    },
    {
        "ref": "sam3.pt", "kind": "sam3", "label": "SAM 3 — concept segmentation",
        "note": "Open vocabulary: prompt with noun phrases such as 'bleached coral'. "
                "Runs on a selection only — measured at ~2 s/image on an RTX 4070 SUPER "
                "and ~5 s/image on CPU, against well under a second for a YOLO pass, and the "
                "concepts should suit the images in front of you. "
                "Weights are access-gated: download sam3.pt into the model directory.",
        # Never auto-fetched by Ultralytics, unlike the yolo* checkpoints.
        "auto_download": False,
        "selection_only": True,
    },
]


def _extra_models() -> list[dict]:
    """Operator-supplied refs from LIB_EXTRA_MODELS (comma separated)."""
    out = []
    for ref in (r.strip() for r in settings.EXTRA_MODELS.split(",")):
        if ref:
            out.append({
                "ref": ref, "kind": "sam3" if "sam3" in ref.lower() else "yolo",
                "label": Path(ref).name, "note": "From LIB_EXTRA_MODELS.",
            })
    return out


def catalog() -> list[dict]:
    """
    Every model the UI may offer: the curated list, operator additions, and
    whatever is already sitting in the model directory.
    """
    entries: dict[str, dict] = {}
    for entry in BUILTIN_MODELS + _extra_models():
        cached = _is_cached(entry["ref"])
        entries[entry["ref"]] = {
            **entry,
            "cached": cached,
            "selection_only": entry.get("selection_only", False),
            # Usable now, or only after someone supplies the weights.  A model
            # Ultralytics will fetch on demand counts as usable; SAM 3 does not.
            "usable": cached or entry.get("auto_download", True),
        }

    for found in cached_models():
        ref = found["path"]
        if ref in entries or found["name"] in {Path(e).name for e in entries}:
            continue
        is_sam3 = "sam3" in found["name"].lower()
        entries[ref] = {
            "ref": ref, "kind": "sam3" if is_sam3 else "yolo",
            "label": found["name"], "note": "Found in the model directory.",
            "cached": True, "usable": True, "selection_only": is_sam3,
        }
    return list(entries.values())


def allowed_refs() -> set[str]:
    return {entry["ref"] for entry in catalog()}


def is_allowed(ref: str) -> bool:
    """Whether a caller-supplied reference may be loaded."""
    return bool(ref) and ref in allowed_refs()


def _is_cached(ref: str) -> bool:
    """Whether the weights are already on disk.  Not the same as 'usable'."""
    name = Path(ref).name
    for directory in _cache_dirs():
        candidate = directory / name
        if candidate.exists() and candidate.stat().st_size > 0:
            return True
    # An absolute path that does not exist is simply missing.
    return False


def resolve(ref: str) -> str:
    """
    Turn a model reference into something Ultralytics can open.

    * ``gs://bucket/key``      → downloaded into the model cache, path returned
    * an existing local path   → returned unchanged
    * a bare name already sitting in the model cache (sam3.pt) → the full path,
      so a checkpoint that is never auto-fetched is still found there
    * any other bare name (yolo11n.pt) → a path in the model cache, where
      Ultralytics downloads it
    """
    ref = (ref or "").strip()
    if not ref.startswith("gs://"):
        if ref and Path(ref).name == ref:
            for directory in _cache_dirs():
                candidate = directory / ref
                if candidate.exists() and candidate.stat().st_size > 0:
                    return str(candidate)
            # Not cached yet. Hand Ultralytics a path inside the cache: it
            # downloads its own release assets (yolo11n.pt, …) to exactly that
            # path, instead of the working directory a recreated container loses.
            return str(_writable_cache_dir() / ref)
        return ref

    name = Path(ref).name

    with _lock:
        # Prefer weights already supplied in the configured directory, even when
        # it is read-only — that is how you ship sam3.pt into a container.
        supplied = settings.MODEL_DIR / name
        if supplied.exists() and supplied.stat().st_size > 0:
            return str(supplied)

        target = _writable_cache_dir() / name
        if target.exists() and target.stat().st_size > 0:
            return str(target)

        from library.storage import get_backend  # noqa: PLC0415

        bucket_root = "gs://" + ref[5:].split("/", 1)[0]
        data = get_backend(bucket_root).read_bytes(ref)
        # Write via a temp name so an interrupted download is never mistaken
        # for a cached model on the next run.
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.with_suffix(target.suffix + ".partial")
        staging.write_bytes(data)
        staging.replace(target)
        return str(target)


def cached_models() -> list[dict]:
    """What is already on disk, for the UI's model panel."""
    seen: dict[str, dict] = {}
    for directory in _cache_dirs():
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.pt")):
            seen.setdefault(path.name, {
                "name": path.name, "path": str(path), "bytes": path.stat().st_size,
            })
    return list(seen.values())
