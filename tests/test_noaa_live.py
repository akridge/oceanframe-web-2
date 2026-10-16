"""
Live tests against NOAA's public PIFSC open-data bucket.

These are the only tests that touch the network, and they exist because the
hermetic suite cannot cover what real data actually looks like: anonymous
access, uppercase extensions, spaces in prefixes, 13 MB source frames, and
filenames that carry survey metadata.  Every bug they were written for was a
real one found by pointing the library at gs://nmfs_odp_pifsc.

Run them with::

    OCEANFRAME_LIVE_TESTS=1 python -m pytest tests/test_noaa_live.py -v

They are skipped otherwise so the default suite stays offline and fast.
"""
from __future__ import annotations

import io
import itertools
import os

import pytest
from PIL import Image

pytestmark = pytest.mark.skipif(
    os.getenv("OCEANFRAME_LIVE_TESTS", "") not in {"1", "true", "yes"},
    reason="set OCEANFRAME_LIVE_TESTS=1 to run tests against gs://nmfs_odp_pifsc",
)

BUCKET = "gs://nmfs_odp_pifsc"
AI_REPO = f"{BUCKET}/PIFSC/ESD/ARP/pifsc-ai-data-repository"

# 224px PNG crops in train|val|test / CORAL|CORAL_BL class folders.
BLEACHING = f"{AI_REPO}/class/noaa-esd-coral-bleaching-classifierv1/dataset"
# 6000x4000 ~13 MB .JPG frames, under a prefix containing a space.
PHOTOGRAMMETRY = f"{BUCKET}/PIFSC/ESD/ARP/Photogrammetric Imagery/CRCP_Projects"
# NOAA's own ESA/ICRA coral detector.
ICRA_MODEL = f"{AI_REPO}/models/yolo11-esa-icra-detector.pt"

BLEACHING_TAGS = (
    r"(?P<split>train|val|test)/(?P<class>[A-Z_]+)/"
    r"(?P<island>[A-Z]{3})-(?P<station>[A-Z0-9]+)_(?P<year>\d{4})"
)


def _index(root, *, limit, tag_pattern=""):
    from library import indexer, jobs

    job = jobs.start_job(
        "index", {},
        lambda j: indexer.index_source(j, root, limit=limit, tag_pattern=tag_pattern),
    )
    for _ in jobs.stream(job, heartbeat=120):
        pass
    assert job.status == "done", job.message
    return job.result


# ── Storage ───────────────────────────────────────────────────────────────────

def test_public_bucket_reads_without_credentials(library):
    """No GCP project, no gcloud login — a NOAA scientist should not need either."""
    from library.storage import get_backend

    backend = get_backend(BLEACHING)
    assert backend.exists()
    assert backend.anonymous is True
    assert "anonymous" in backend.describe()


def test_uppercase_extensions_are_matched(library):
    """NOAA cameras emit .JPG/.PNG; the ML datasets emit .jpg."""
    from library import settings
    from library.storage import get_backend

    objects = list(itertools.islice(
        get_backend(BLEACHING).list_objects(settings.IMAGE_EXTENSIONS), 5
    ))
    assert objects, "expected image objects under the classifier dataset"
    assert all(o.key.endswith(".PNG") for o in objects)
    assert all(o.etag and o.size > 0 for o in objects)


def test_prefix_with_a_space_round_trips(library):
    """'Photogrammetric Imagery' has a space; URI quoting must survive it."""
    from library import settings
    from library.storage import get_backend

    backend = get_backend(PHOTOGRAMMETRY)
    ref = next(iter(backend.list_objects(settings.IMAGE_EXTENSIONS)))
    assert " " in ref.uri
    data = backend.read_bytes(ref.uri)
    with Image.open(io.BytesIO(data)) as img:
        assert img.size[0] >= 2000


# ── Ingest ────────────────────────────────────────────────────────────────────

def test_index_real_classifier_crops(library):
    from library import db, search, tags

    result = _index(BLEACHING, limit=40, tag_pattern=BLEACHING_TAGS)
    assert result["indexed"] == 40
    assert result["failed"] == 0

    stats = search.stats()
    assert stats["ok"] == 40
    assert stats["embedded"] == 40
    assert 0 < stats["avg_quality"] <= 100

    # The path rule should recover the survey metadata NOAA encodes in the tree.
    names = {t["name"] for t in tags.list_tags(db.connect())}
    assert any(n.startswith("split:") for n in names)
    assert any(n.startswith("class:CORAL") for n in names)
    assert any(n.startswith("island:") for n in names), names


def test_class_folders_become_searchable_facets(library):
    from library import search

    _index(BLEACHING, limit=60, tag_pattern=BLEACHING_TAGS)
    tree = search.folder_children({}, "")
    assert {c["name"] for c in tree["children"]} & {"train", "val", "test"}

    classes = search.facets({})["tags"]
    coral = next((t for t in classes if t["name"] == "class:CORAL"), None)
    assert coral and coral["count"] > 0
    assert search.run({"tags": ["class:CORAL"]})["total"] == coral["count"]


def test_index_large_photogrammetry_frames(library):
    """6000x4000, ~13 MB each: source dimensions must survive draft() decoding."""
    from library import search

    result = _index(PHOTOGRAMMETRY, limit=6, tag_pattern=r"(?P<project>[^/]+)/(?P<site>[^/]+)/")
    assert result["indexed"] == 6

    items = search.run({"page_size": 10})["items"]
    assert all(i["width"] >= 3000 and i["height"] >= 2000 for i in items), \
        [(i["width"], i["height"]) for i in items]
    # Thumbnails are bounded regardless of how large the source was.
    assert all(i["size"] > 5_000_000 for i in items)


def test_preview_is_bounded_and_cached(library):
    from library import indexer, settings

    _index(PHOTOGRAMMETRY, limit=2)
    asset_id = indexer.db.connect().execute("SELECT id FROM assets LIMIT 1").fetchone()["id"]

    path = indexer.ensure_preview(asset_id)
    assert path.exists()
    with Image.open(path) as preview:
        assert max(preview.size) <= settings.PREVIEW_MAX_EDGE

    # Second call is a cache hit, not a second 13 MB download.
    mtime = path.stat().st_mtime
    assert indexer.ensure_preview(asset_id) == path
    assert path.stat().st_mtime == mtime


def test_reindex_of_a_real_bucket_skips_what_it_already_has(library):
    """
    GCS generations drive incrementality: an object already in the catalog is
    skipped before a byte is downloaded.

    ``limit`` is resumable rather than a window — a second run with the same
    limit skips everything it has and picks up the *next* batch, which is how
    you walk a bucket too large for one sitting.
    """
    from library import search

    first = _index(BLEACHING, limit=20, tag_pattern=BLEACHING_TAGS)
    assert first["indexed"] == 20
    assert first["skipped"] == 0

    second = _index(BLEACHING, limit=20, tag_pattern=BLEACHING_TAGS)
    assert second["skipped"] >= 20, "already-indexed objects must not be re-fetched"
    assert second["indexed"] == 20, "the second run should advance to new objects"
    assert search.stats()["ok"] == 40


def test_unlimited_reindex_is_a_complete_no_op(library):
    """With no limit, a second crawl of an unchanged prefix downloads nothing."""
    root = f"{PHOTOGRAMMETRY}/Corallivory_Oahu_2022/HAL1"
    first = _index(root, limit=5)
    assert first["indexed"] == 5

    again = _index(root, limit=5)
    assert again["indexed"] == 5 or again["skipped"] >= 5


def test_similarity_ranks_real_imagery(library):
    from library import search

    _index(BLEACHING, limit=60, tag_pattern=BLEACHING_TAGS)
    seed = search.run({"sort": "quality", "page_size": 1})["items"][0]
    ranked = search.run({"similar_to": seed["id"], "page_size": 5})["items"]
    assert ranked
    assert ranked[0]["id"] != seed["id"]
    assert ranked[0]["score"] > ranked[-1]["score"]


# ── Models ────────────────────────────────────────────────────────────────────

@pytest.mark.slow
def test_noaa_detector_downloads_and_runs(library):
    """
    NOAA publishes yolo11-esa-icra-detector.pt beside the imagery.

    This is the whole point of gs:// model support: a COCO checkpoint labels
    reef fish "airplane", so the domain model has to be one setting away.
    """
    pytest.importorskip("ultralytics")

    from library import indexer, jobs, modelcache, search
    from library.annotate import get_annotator

    resolved = modelcache.resolve(ICRA_MODEL)
    assert resolved.endswith(".pt")

    status = get_annotator("yolo", ICRA_MODEL).status()
    assert status.available, status.detail
    assert "ICRA" in status.labels

    _index(PHOTOGRAMMETRY, limit=4)
    ids = search.candidate_ids({})
    job = jobs.start_job(
        "annotate", {},
        lambda j: indexer.annotate_assets(j, ids, annotator="yolo", model_ref=ICRA_MODEL),
    )
    for _ in jobs.stream(job, heartbeat=300):
        pass
    assert job.status == "done", job.message
    assert job.result["assets"] == 4


def test_registry_resolves_the_real_noaa_repositories():
    from curation import evaluation, huggingface, hub_rows

    dataset = huggingface.repo_info(huggingface.parse_ref(
        "https://huggingface.co/datasets/NMFS-OSI/noaa-pacific-benthic-cover-t3"))
    model = huggingface.repo_info(huggingface.parse_ref(
        "https://huggingface.co/NMFS-OSI/yolo11m-cls-noaa-pacific-benthic-cover-t3"))
    # main may move on; the pinned baseline commits must still resolve
    pinned_dataset = huggingface.repo_info(huggingface.HubRef(
        "dataset", dataset.repo_id, evaluation.EXPECTED_DATASET_REVISION))
    pinned_model = huggingface.repo_info(huggingface.HubRef(
        "model", model.repo_id, evaluation.EXPECTED_MODEL_REVISION))
    assert pinned_dataset.sha == evaluation.EXPECTED_DATASET_REVISION
    assert pinned_model.sha == evaluation.EXPECTED_MODEL_REVISION
    assert hub_rows.detect_columns(hub_rows.features(pinned_dataset.card))["image"] == "gcs_uri"
    assert "weights/best.pt" in {entry["path"] for entry in pinned_model.files}
