"""HTTP surface: the routes the UI depends on."""
from __future__ import annotations

import io

import pytest
from PIL import Image


@pytest.fixture
def client(library, tree):
    from fastapi.testclient import TestClient

    from library import indexer, jobs

    job = jobs.start_job("index", {}, lambda j: indexer.index_source(j, str(tree)))
    for _ in jobs.stream(job, heartbeat=30):
        pass

    import main

    return TestClient(main.app)


def test_status_reports_backends(client):
    body = client.get("/api/library/status").json()
    assert body["stats"]["ok"] == 5
    assert body["embedder"]["backend"]
    assert {a["name"] for a in body["annotators"]} == {"yolo", "sam3"}


def test_status_never_loads_a_model_inline(client, monkeypatch):
    """A slow first load (yolo11n.pt downloading) once froze every page."""
    import threading

    from library import annotate
    from library.annotate.base import Annotator
    from library.models import AnnotatorStatus

    release = threading.Event()
    load_started = threading.Event()

    class SlowAnnotator(Annotator):
        name = "yolo"

        def __init__(self):
            self.done = False

        def loaded(self):
            return self.done

        def status(self):
            load_started.set()
            release.wait(10)
            self.done = True
            return AnnotatorStatus(self.name, True, "slow · ready")

        def annotate(self, images, prompts=None):
            return [[] for _ in images]

    slow = SlowAnnotator()
    monkeypatch.setitem(annotate._cache, "yolo:", slow)
    monkeypatch.setitem(annotate._cache, "sam3:", SlowAnnotator())
    try:
        body = client.get("/api/library/status").json()  # would hang 10 s if loaded inline
        yolo = next(a for a in body["annotators"] if a["name"] == "yolo")
        assert yolo["loading"] is True and yolo["available"] is False
        assert load_started.wait(5), "the background load never started"
    finally:
        release.set()

    for _ in range(50):
        if slow.done and "yolo" not in annotate._warming:
            break
        threading.Event().wait(0.1)
    yolo = next(a for a in client.get("/api/library/status").json()["annotators"] if a["name"] == "yolo")
    assert yolo == {"name": "yolo", "available": True, "detail": "slow · ready", "labels": [], "loading": False}


def test_library_is_the_landing_page(client):
    """`/` is the library; the frame analyser moved to /frames."""
    root = client.get("/")
    assert root.status_code == 200
    assert "OceanFrame Library" in root.text

    frames = client.get("/frames")
    assert frames.status_code == 200
    assert "Marine Video Frame Analysis" in frames.text


def test_old_library_url_still_works(client):
    """Bookmarks and docs pointing at /library must not break."""
    redirect = client.get("/library", follow_redirects=False)
    assert redirect.status_code == 308
    assert redirect.headers["location"] == "/"
    assert "OceanFrame Library" in client.get("/library").text


def test_model_catalog_is_offered(client):
    body = client.get("/api/library/models").json()
    refs = {m["ref"] for m in body["models"]}
    assert "yolo11n.pt" in refs
    assert any(r.endswith("yolo11-esa-icra-detector.pt") for r in refs)
    assert all({"ref", "kind", "label", "cached"} <= set(m) for m in body["models"])


def test_arbitrary_model_reference_is_rejected(client):
    """
    A model reference names a pickle that torch.load will execute, so one
    arriving from a request body must never be loaded.
    """
    response = client.post(
        "/api/library/annotate",
        json={"annotator": "yolo", "query": {}, "model": "gs://attacker/payload.pt"},
    )
    assert response.status_code == 400
    assert "allowlist" in response.json()["detail"]


def test_sam3_refuses_a_whole_collection(client):
    """
    SAM 3 is selection-only: it is far slower than a YOLO pass and its concepts
    should suit the images in front of you, so a collection-wide run is almost
    always a mistake rather than an intent.
    """
    for body in ({"annotator": "sam3", "source_id": 1}, {"annotator": "sam3", "query": {}}):
        response = client.post("/api/library/annotate", json=body)
        assert response.status_code == 400
        assert "selection" in response.json()["detail"].lower()


def test_sam3_is_marked_selection_only_in_the_catalog(client):
    models = {m["ref"]: m for m in client.get("/api/library/models").json()["models"]}
    sam3 = models["sam3.pt"]
    assert sam3["kind"] == "sam3"
    assert sam3["selection_only"] is True
    # Ultralytics fetches yolo* on demand but never SAM 3, so "usable" tracks
    # whether the gated weights are actually present.
    assert sam3["usable"] is sam3["cached"]
    assert models["yolo11n.pt"]["usable"] is True


def test_a_model_that_never_ran_fails_the_job(library, tree, monkeypatch):
    """
    A run that errored on every image must not report "0 detections" — that is
    indistinguishable from a model that worked and found nothing, which is
    exactly how a broken SAM 3 looked like a working one.
    """
    from library import indexer, jobs
    from library.models import AnnotatorStatus

    job = jobs.start_job("index", {}, lambda j: indexer.index_source(j, str(tree)))
    for _ in jobs.stream(job, heartbeat=60):
        pass

    class Broken:
        name = "sam3"
        model_ref = "sam3.pt"
        runtime_error = "SAM 3 inference failed: text encoder missing"

        def status(self):
            return AnnotatorStatus("sam3", True, "ready")

        def annotate(self, images, prompts=None):
            return [[] for _ in images]

    monkeypatch.setattr("library.annotate.get_annotator", lambda *a, **k: Broken())
    ids = indexer.db.connect().execute("SELECT id FROM assets LIMIT 2").fetchall()

    run = jobs.start_job("annotate", {}, lambda j: indexer.annotate_assets(
        j, [r["id"] for r in ids], annotator="sam3", prompts=["coral"]))
    for _ in jobs.stream(run, heartbeat=60):
        pass
    assert run.status == "error"
    assert "text encoder missing" in run.message


def test_collection_remembers_its_own_model(client, tree):
    from library import indexer

    root = str(tree)
    client.post("/api/library/sources", json={"root": root, "model_ref": "yolo11s.pt"})
    stored = {s["root"]: s["model_ref"] for s in client.get("/api/library/sources").json()["sources"]}
    assert stored[indexer.normalise_root(root)] == "yolo11s.pt"

    # A blank model on an annotate call falls back to the collection's own.
    source_id = next(s["id"] for s in client.get("/api/library/sources").json()["sources"])
    assert indexer.source_model_ref(source_id) == "yolo11s.pt"


def test_search_and_asset_detail(client):
    results = client.post("/api/library/search", json={"sort": "quality", "page_size": 3}).json()
    assert results["total"] == 5

    asset_id = results["items"][0]["id"]
    detail = client.get(f"/api/library/asset/{asset_id}").json()
    assert set(detail["quality_breakdown"]) >= {"sharpness", "exposure", "contrast", "colour"}
    assert detail["similar"]

    assert client.get(f"/api/library/thumb/{asset_id}").headers["content-type"] == "image/jpeg"
    assert client.get(f"/api/library/image/{asset_id}").status_code == 200


def test_missing_asset_is_404(client):
    assert client.get("/api/library/asset/999999").status_code == 404
    assert client.get("/api/library/thumb/999999").status_code == 404


def test_tag_round_trip(client):
    ids = [i["id"] for i in client.post("/api/library/search", json={"page_size": 2}).json()["items"]]
    client.post("/api/library/tags", json={"asset_ids": ids, "names": ["review:keep"]})
    assert client.post("/api/library/search", json={"tags": ["review:keep"]}).json()["total"] == 2

    client.post("/api/library/tags/remove", json={"asset_ids": ids, "names": ["review:keep"]})
    assert client.post("/api/library/search", json={"tags": ["review:keep"]}).json()["total"] == 0


def test_search_by_uploaded_image(client):
    buffer = io.BytesIO()
    Image.new("RGB", (200, 150), (20, 90, 140)).save(buffer, "JPEG")
    response = client.post(
        "/api/library/search-by-image",
        files={"file": ("q.jpg", buffer.getvalue(), "image/jpeg")},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["items"] and "score" in body["items"][0]


def test_annotate_without_weights_reports_why(client, monkeypatch):
    """A missing model must come back as an actionable 400, not a dead job."""
    from library.models import AnnotatorStatus

    monkeypatch.setattr(
        "library.annotate.yolo.YoloAnnotator.status",
        lambda self: AnnotatorStatus("yolo", False, "ultralytics is not installed."),
    )
    response = client.post("/api/library/annotate", json={"annotator": "yolo", "query": {}})
    assert response.status_code == 400
    assert "ultralytics" in response.json()["detail"]


def test_export_download_rejects_path_traversal(client):
    assert client.get("/api/library/exports/..%2F..%2Fetc%2Fpasswd").status_code == 404


def test_dataset_create_and_export(client):
    dataset = client.post("/api/library/datasets", json={"name": "d1", "query": {"status": "ok"}}).json()
    assert dataset["size"] == 5

    job = client.post(
        f"/api/library/datasets/{dataset['id']}/export",
        json={"kind": "csv", "include_images": False},
    ).json()
    assert job["kind"] == "export"

    # Drain the job stream so the export finishes before we look for the file.
    with client.stream("GET", f"/api/library/jobs/{job['id']}/stream") as stream:
        for _ in stream.iter_lines():
            pass
    assert client.get("/api/library/exports").json()["exports"]


def test_every_page_has_the_same_main_navigation(client):
    import re

    # Pages on the shared shell show the rail; only /curation still shows the old header.
    # Either way the main apps appear in the same order on every page.
    orders = {}
    for path in ("/", "/collections", "/collections/new", "/frames", "/curation"):
        page = client.get(path)
        assert page.status_code == 200, path
        rail = re.findall(r'class="rail-label">([^<]+)<', page.text)
        legacy = re.findall(r'class="nav-link nav-main[^"]*"[^>]*>([^<]+)</a>', page.text)
        labels = [name for name in (rail or legacy) if name != "Collections"]
        orders[path] = tuple(labels[:3])
    assert set(orders.values()) == {("Library", "Curation", "Frame Analyser")}, orders
    assert re.search(r'aria-current="page"[^>]*>\s*<i[^>]*></i><span class="rail-label">Library<', client.get("/").text)
    assert 'id="open-datasets"' in client.get("/").text  # page-specific links survive
