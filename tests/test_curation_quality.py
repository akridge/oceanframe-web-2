"""Server quality scores: the scorer matches the browser's thresholds, and the job is resumable."""
from __future__ import annotations

import io

import numpy as np
from PIL import Image

from test_curation import P, _add, _item_payload, _project, _test_buckets, client, team  # noqa: F401


def test_score_image_follows_the_browser_thresholds():
    from curation.quality import score_image

    black = score_image(Image.new("RGB", (224, 224), (0, 0, 0)))
    assert black["flags"] == ["blurry", "too_dark", "low_contrast"]
    assert (black["score"], black["suggestion"]) == (22, "reject")
    # Flat blue water: blurry, no contrast and a blue cast (brightness 73: not too dark).
    blue = score_image(Image.new("RGB", (224, 224), (50, 60, 200)))
    assert blue["flags"] == ["blurry", "low_contrast", "blue_cast"] and blue["score"] == 32
    assert blue["metrics"]["brightness"] == 73 and blue["metrics"]["color_cast"] == 4.0
    texture = np.kron(np.random.default_rng(7).integers(30, 230, size=(28, 28, 1)), np.ones((8, 8, 3))).astype(np.uint8)
    clean = score_image(Image.fromarray(texture))
    assert clean["flags"] == ["clean"] and (clean["score"], clean["suggestion"]) == (100, "accept")
    assert score_image(None) == {"score": 0, "flags": ["analysis_failed"], "suggestion": "manual_review", "metrics": None}


def _png(array) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format="PNG")
    return buffer.getvalue()


def test_the_job_scores_every_patch_once_and_quality_filters_use_it(client, monkeypatch):
    from appdb import session_scope
    from curation import quality, storage
    from curation.models import CurationImportOperation

    pid = _project(client, "Scored")["id"]
    dark = _add(client, pid, _item_payload("dark-1", "ACBR"))
    sharp = _add(client, pid, _item_payload("sharp-1", "POCS"))
    texture = np.kron(np.random.default_rng(3).integers(30, 230, size=(28, 28, 1)), np.ones((8, 8, 3))).astype(np.uint8)
    reads = []

    def read(uri):
        reads.append(uri)
        if "missing" in uri:
            raise FileNotFoundError(uri)
        return _png(np.zeros((224, 224, 3), np.uint8)) if "dark" in uri else _png(texture)
    monkeypatch.setattr(storage, "read_bytes", read)

    response = client.post(f"{P}/projects/{pid}/quality-scores")
    assert response.status_code == 202, response.text
    assert client.post(f"{P}/projects/{pid}/quality-scores").json()["id"] == response.json()["id"]  # one at a time
    assert client.get(f"{P}/projects/{pid}/imports").json() == []  # not an import
    with session_scope() as db:
        operation = db.get(CurationImportOperation, response.json()["id"])
        assert quality.run(db, operation) == {"checked": 2, "scored": 2, "failed": 0}

    summary = client.get(f"{P}/projects/{pid}/quality-scores").json()
    assert summary["scored"] == 2 and summary["by_suggestion"] == {"reject": 1, "accept": 1}
    assert summary["flags"]["too_dark"] == 1 and summary["flags"]["clean"] == 1
    scores = client.get(f"{P}/projects/{pid}/quality-scores", params={"item_ids": f"{dark['id']},{sharp['id']}"}).json()
    assert scores["scores"][str(dark["id"])]["suggestion"] == "reject"
    assert scores["scores"][str(sharp["id"])]["flags"] == ["clean"]

    def ids(**params):
        return [item["id"] for item in client.get(f"{P}/projects/{pid}/items", params=params).json()["items"]]
    assert ids(quality_suggestion="attention") == [dark["id"]]
    assert ids(quality_flag="too_dark") == [dark["id"]]
    assert ids(quality_max_score=50) == [dark["id"]]
    assert ids(quality_suggestion="accept") == [sharp["id"]]

    # Again: nothing left to do, nothing fetched.
    reads.clear()
    with session_scope() as db:
        assert quality.run(db, db.get(CurationImportOperation, response.json()["id"]))["scored"] == 0
    assert reads == []

    # A resized point is scored again; the other is not.
    resized = client.patch(f"{P}/projects/{pid}/items/{sharp['id']}",
                           json={"expected_revision": sharp["revision"], "crop_size": 150})
    assert resized.status_code == 200, resized.text
    assert client.get(f"{P}/projects/{pid}/quality-scores").json()["scored"] == 1
    with session_scope() as db:
        assert quality.run(db, db.get(CurationImportOperation, response.json()["id"]))["scored"] == 1


def test_only_leads_prepare_quality_scores(team):
    pid = _project(team["ana"], "Lead only")["id"]
    assert team["ben"].post(f"{P}/projects/{pid}/quality-scores").status_code == 403  # a curator, not its lead
    assert team["vic"].get(f"{P}/projects/{pid}/quality-scores").status_code == 200
    assert team["admin"].post(f"{P}/projects/{pid}/quality-scores").status_code == 202
