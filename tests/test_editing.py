"""Editing patches: geometry, remove/restore, batch edits and suggestions."""
from __future__ import annotations

import io

import pytest
from PIL import Image

from test_curation import P, _add, _item_payload, _project, _test_buckets, client, db_session  # noqa: F401


@pytest.fixture(autouse=True)
def _images(monkeypatch, tmp_path):
    """Every source image is a 1000×800 JPEG; anything named *missing* is absent."""
    from curation import image_cache, storage

    buffer = io.BytesIO()
    Image.new("RGB", (1000, 800), (30, 140, 90)).save(buffer, format="JPEG")
    payload = buffer.getvalue()

    def read(uri):
        if "missing" in uri:
            raise FileNotFoundError(uri)
        return payload

    monkeypatch.setenv("CURATION_IMAGE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(storage, "read_bytes", read)
    image_cache.clear()


def _point(client, pid, key="e1", label="ACBR", row=120, col=240, image="img-1"):
    payload = {**_item_payload(key, label), "row_px": row, "column_px": col,
               "source_image_uri": f"gs://nmfs/images/{image}.jpg"}
    return _add(client, pid, payload)


def test_move_and_resize_a_point(client):
    pid = _project(client, "Geometry")["id"]
    item = _point(client, pid)
    moved = client.patch(f"{P}/projects/{pid}/items/{item['id']}", json={
        "expected_revision": item["revision"], "row_px": 300, "column_px": 500, "crop_size": 160})
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert (body["row_px"], body["column_px"], body["crop_size"]) == (300, 500, 160)
    events = client.get(f"{P}/projects/{pid}/activity").json()
    latest = events[0] if isinstance(events, list) else events["events"][0]
    assert latest["previous_value"]["row_px"] == 120 and latest["new_value"]["row_px"] == 300


def test_points_must_stay_inside_the_image(client):
    pid = _project(client, "Bounds")["id"]
    item = _point(client, pid)
    out = client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                       json={"expected_revision": item["revision"], "row_px": 800, "column_px": 10})
    assert out.status_code == 422 and "inside the image" in out.json()["detail"]
    assert client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                        json={"expected_revision": item["revision"], "crop_size": 2000}).status_code == 422


def test_a_missing_source_image_refuses_geometry_edits_clearly(client):
    pid = _project(client, "Missing source")["id"]
    item = _point(client, pid, image="missing-1")
    out = client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                       json={"expected_revision": item["revision"], "row_px": 10, "column_px": 10})
    assert out.status_code == 422 and "not in the bucket" in out.json()["detail"]


def test_remove_hides_and_restore_brings_back(client):
    pid = _project(client, "Remove")["id"]
    item = _point(client, pid)
    removed = client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                           json={"expected_revision": item["revision"], "removed": True}).json()
    assert (removed["removed"], removed["included"]) == (True, False)
    assert client.get(f"{P}/projects/{pid}/items").json()["returned"] == 0
    assert client.get(f"{P}/projects/{pid}/items?removed=only").json()["returned"] == 1
    restored = client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                            json={"expected_revision": removed["revision"], "removed": False}).json()
    assert (restored["removed"], restored["included"]) == (False, True)
    assert client.get(f"{P}/projects/{pid}/items").json()["returned"] == 1


def test_patch_only_items_have_no_geometry_to_edit(client, db_session):
    from curation.models import CurationItem

    pid = _project(client, "Patch only")["id"]
    item = _point(client, pid)
    row = db_session.get(CurationItem, item["id"])
    row.row_px = row.column_px = None
    db_session.commit()
    out = client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                       json={"expected_revision": item["revision"], "crop_size": 160})
    assert out.status_code == 422 and "patch" in out.json()["detail"].lower()


def test_item_flag_migration_restores_both_columns(client):
    """Simulate a database created before P3a: drop the columns, then migrate."""
    from sqlalchemy import inspect, text

    from appdb.bootstrap import _item_flags
    from appdb.engine import get_engine

    engine = get_engine()
    pid = _project(client, "Flags")["id"]
    _point(client, pid)

    def columns():
        return {c["name"] for c in inspect(engine).get_columns("curation_items")}

    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE curation_items DROP COLUMN removed"))
            conn.execute(text("ALTER TABLE curation_items DROP COLUMN source_missing"))
        assert not {"removed", "source_missing"} & columns()

        with engine.begin() as conn:
            _item_flags(conn)
            _item_flags(conn)  # idempotent
        assert {"removed", "source_missing"} <= columns()
        with engine.begin() as conn:
            row = conn.execute(text("SELECT removed, source_missing FROM curation_items")).first()
        assert tuple(row) == (0, 0)
    finally:
        with engine.begin() as conn:
            _item_flags(conn)


def test_a_removed_point_cannot_be_included_without_restoring(client):
    pid = _project(client, "No sneaking back")["id"]
    item = _point(client, pid)
    removed = client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                           json={"expected_revision": item["revision"], "removed": True}).json()
    out = client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                       json={"expected_revision": removed["revision"], "included": True})
    assert out.status_code == 422 and "Restore" in out.json()["detail"]
    # Restoring and including in one request is fine.
    ok = client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                      json={"expected_revision": removed["revision"], "removed": False, "included": True})
    assert ok.status_code == 200 and ok.json()["included"] is True


def test_bulk_include_skips_removed_points_and_reports_them(client):
    pid = _project(client, "Bulk removed")["id"]
    a, b = _point(client, pid, "a1"), _point(client, pid, "b1", col=300)
    removed = client.patch(f"{P}/projects/{pid}/items/{a['id']}",
                           json={"expected_revision": a["revision"], "removed": True}).json()
    out = client.post(f"{P}/projects/{pid}/items/bulk",
                      json={"item_ids": [a["id"], b["id"]], "included": True}).json()
    assert out["updated"] == 1 and [c["id"] for c in out["conflicts"]] == [a["id"]]
    still = client.get(f"{P}/projects/{pid}/items?removed=only").json()["items"][0]
    assert still["included"] is False and still["revision"] == removed["revision"]


def test_quality_accept_does_not_bring_back_a_removed_point(client):
    pid = _project(client, "Quality removed")["id"]
    a = _point(client, pid, "a1")
    removed = client.patch(f"{P}/projects/{pid}/items/{a['id']}",
                           json={"expected_revision": a["revision"], "removed": True}).json()
    single = client.post(f"{P}/projects/{pid}/items/{a['id']}/quality",
                         json={"expected_revision": removed["revision"], "decision": "accept"})
    assert single.status_code == 422
    whole = client.post(f"{P}/projects/{pid}/source-images/{a['id']}/quality",
                        json={"expected_revision": removed["revision"], "decision": "accept"})
    assert whole.status_code == 200
    still = client.get(f"{P}/projects/{pid}/items?removed=only").json()["items"][0]
    assert still["included"] is False


def _image_items(client, pid):
    a = _point(client, pid, key="b1", label="ACBR", row=100, col=100)
    b = _point(client, pid, key="b2", label="POCS", row=400, col=600)
    other = _point(client, pid, key="b3", label="SAND", image="img-2")
    return a, b, other


def test_a_batch_moves_resizes_removes_and_adds(client):
    pid = _project(client, "Batch")["id"]
    a, b, _ = _image_items(client, pid)
    response = client.post(f"{P}/projects/{pid}/source-images/{a['id']}/edits", json={
        "reason": "fine-tune ACBR", "edits": [
            {"op": "move", "item_id": a["id"], "expected_revision": a["revision"], "row_px": 110, "column_px": 105},
            {"op": "resize", "item_id": b["id"], "expected_revision": b["revision"], "crop_size": 320},
        ],
        "added": [{"row_px": 150, "column_px": 150, "crop_size": 224, "label": "ACBR"},
                  {"row_px": 50, "column_px": 50, "crop_size": 224, "label": "ACBR"}]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] == 2 and len(body["added"]) == 2 and body["conflicts"] == []
    detail = client.get(f"{P}/projects/{pid}/source-images/{a['id']}").json()
    assert (detail["width"], detail["height"]) == (1000, 800)
    by_id = {item["id"]: item for item in detail["items"]}
    assert (by_id[a["id"]]["row_px"], by_id[b["id"]]["crop_size"]) == (110, 320)
    assert len(detail["items"]) == 4


def test_stale_items_are_skipped_and_the_rest_apply(client):
    pid = _project(client, "Batch conflict")["id"]
    a, b, _ = _image_items(client, pid)
    client.patch(f"{P}/projects/{pid}/items/{b['id']}", json={"expected_revision": b["revision"], "review_state": "reviewed"})
    body = client.post(f"{P}/projects/{pid}/source-images/{a['id']}/edits", json={"edits": [
        {"op": "remove", "item_id": a["id"], "expected_revision": a["revision"]},
        {"op": "remove", "item_id": b["id"], "expected_revision": b["revision"]},
    ]}).json()
    assert body["applied"] == 1 and [c["id"] for c in body["conflicts"]] == [b["id"]]


def test_batch_validation(client):
    pid = _project(client, "Batch rules")["id"]
    a, _, other = _image_items(client, pid)
    url = f"{P}/projects/{pid}/source-images/{a['id']}/edits"
    elsewhere = client.post(url, json={"edits": [{"op": "remove", "item_id": other["id"], "expected_revision": other["revision"]}]})
    assert elsewhere.status_code == 422 and "not on this image" in elsewhere.json()["detail"]
    outside = client.post(url, json={"added": [{"row_px": 900, "column_px": 10, "crop_size": 224, "label": "ACBR"}]})
    assert outside.status_code == 422
    bad_label = client.post(url, json={"added": [{"row_px": 10, "column_px": 10, "crop_size": 224, "label": "NOT-A-CODE"}]})
    assert bad_label.status_code == 422
    too_many = client.post(url, json={"added": [{"row_px": 10, "column_px": 10, "crop_size": 224, "label": "ACBR"}] * 201})
    assert too_many.status_code == 422
    nothing_applied = client.get(f"{P}/projects/{pid}/source-images/{a['id']}").json()
    assert len(nothing_applied["items"]) == 2  # a refused batch changes nothing


def test_suggestions_form_a_clipped_grid(client):
    pid = _project(client, "Suggest")["id"]
    a, _, _ = _image_items(client, pid)
    body = client.post(f"{P}/projects/{pid}/source-images/{a['id']}/suggest",
                       json={"item_id": a["id"], "grid": 3, "spacing_px": 150}).json()
    centres = {(c["row_px"], c["column_px"]) for c in body["candidates"]}
    assert (100, 100) not in centres  # the source patch itself is not a candidate
    assert all(0 <= r < 800 and 0 <= c < 1000 for r, c in centres)
    assert len(centres) == 3  # (100,100) at spacing 150: only +150 offsets stay inside on both axes
    assert body["model_available"] is False and all("hints" not in c for c in body["candidates"])
    five = client.post(f"{P}/projects/{pid}/source-images/{a['id']}/suggest",
                       json={"item_id": a["id"], "grid": 5, "spacing_px": 40}).json()
    assert len(five["candidates"]) == 24


def test_suggestion_hints_come_from_the_classifier(client, monkeypatch):
    from curation import scoring

    pid = _project(client, "Suggest hints")["id"]
    a, _, _ = _image_items(client, pid)
    monkeypatch.setattr(scoring, "classify_points", lambda uri, points, size, model: [
        [{"label": "POCS", "confidence": 0.9, "rank": 1}] for _ in points])
    body = client.post(f"{P}/projects/{pid}/source-images/{a['id']}/suggest",
                       json={"item_id": a["id"], "grid": 3, "spacing_px": 150, "model_name": "m.pt"}).json()
    assert body["model_available"] is True and body["candidates"][0]["hints"][0]["label"] == "POCS"


def test_editing_a_missing_image_is_a_clear_422(client):
    pid = _project(client, "Batch missing")["id"]
    item = _point(client, pid, image="missing-2")
    out = client.post(f"{P}/projects/{pid}/source-images/{item['id']}/edits",
                      json={"added": [{"row_px": 10, "column_px": 10, "crop_size": 224, "label": "ACBR"}]})
    assert out.status_code == 422 and "not in the bucket" in out.json()["detail"]
    detail = client.get(f"{P}/projects/{pid}/source-images/{item['id']}").json()
    assert detail["width"] is None and detail["source_missing"] is True


def test_a_moved_patch_never_shows_its_old_crop(client, monkeypatch):
    from curation import image_cache, storage

    pid = _project(client, "Fresh crop")["id"]
    item = _point(client, pid)
    old_crop = b"old-crop-bytes"
    real = storage.read_bytes
    monkeypatch.setattr(storage, "read_bytes", lambda uri: old_crop if uri == item["crop_uri"] else real(uri))
    image_cache.clear()
    assert client.get(f"{P}/projects/{pid}/items/{item['id']}/crop-preview").content == old_crop
    client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                 json={"expected_revision": item["revision"], "row_px": 300, "column_px": 300})
    fresh = client.get(f"{P}/projects/{pid}/items/{item['id']}/crop-preview")
    assert fresh.status_code == 200 and fresh.content != old_crop and fresh.content.startswith(b"\x89PNG")


def test_commit_summaries_name_geometry_edits(client):
    pid = _project(client, "Kinds")["id"]
    a, b, _ = _image_items(client, pid)
    client.post(f"{P}/projects/{pid}/source-images/{a['id']}/edits", json={"edits": [
        {"op": "move", "item_id": a["id"], "expected_revision": a["revision"], "row_px": 111, "column_px": 111},
        {"op": "resize", "item_id": b["id"], "expected_revision": b["revision"], "crop_size": 300},
    ], "added": [{"row_px": 20, "column_px": 20, "crop_size": 224, "label": "ACBR"}]})
    kinds = client.get(f"{P}/projects/{pid}/change-sets/pending?user=me").json()["summary"]["kinds"]
    assert kinds["move"] == 1 and kinds["resize"] == 1 and kinds["point"] == 1


def test_a_removal_reads_as_removed_not_also_inclusion(client):
    pid = _project(client, "Removal kinds")["id"]
    item = _point(client, pid)
    client.patch(f"{P}/projects/{pid}/items/{item['id']}", json={"expected_revision": item["revision"], "removed": True})
    kinds = client.get(f"{P}/projects/{pid}/change-sets/pending?user=me").json()["summary"]["kinds"]
    assert kinds.get("remove") == 1 and "inclusion" not in kinds


def test_several_edits_to_one_point_share_one_revision_check(client):
    pid = _project(client, "Batch same item")["id"]
    a, b, _ = _image_items(client, pid)
    url = f"{P}/projects/{pid}/source-images/{a['id']}/edits"
    body = client.post(url, json={"edits": [
        {"op": "move", "item_id": a["id"], "expected_revision": a["revision"], "row_px": 130, "column_px": 140},
        {"op": "resize", "item_id": a["id"], "expected_revision": a["revision"], "crop_size": 320},
    ]}).json()
    assert body["applied"] == 2 and body["conflicts"] == []
    moved = {item["id"]: item for item in client.get(f"{P}/projects/{pid}/source-images/{a['id']}").json()["items"]}[a["id"]]
    assert (moved["row_px"], moved["column_px"], moved["crop_size"]) == (130, 140, 320)

    client.patch(f"{P}/projects/{pid}/items/{b['id']}", json={"expected_revision": b["revision"], "review_state": "reviewed"})
    stale = client.post(url, json={"edits": [
        {"op": "move", "item_id": b["id"], "expected_revision": b["revision"], "row_px": 10, "column_px": 10},
        {"op": "resize", "item_id": b["id"], "expected_revision": b["revision"], "crop_size": 160},
    ]}).json()
    assert stale["applied"] == 0 and [c["id"] for c in stale["conflicts"]] == [b["id"]]


def test_an_item_with_mixed_revisions_in_one_batch_changes_not_at_all(client):
    pid = _project(client, "Batch mixed")["id"]
    a, _, _ = _image_items(client, pid)
    body = client.post(f"{P}/projects/{pid}/source-images/{a['id']}/edits", json={"edits": [
        {"op": "move", "item_id": a["id"], "expected_revision": a["revision"], "row_px": 130, "column_px": 140},
        {"op": "resize", "item_id": a["id"], "expected_revision": a["revision"] + 1, "crop_size": 320},
    ]}).json()
    assert body["applied"] == 0 and [c["id"] for c in body["conflicts"]] == [a["id"]]
    same = {item["id"]: item for item in client.get(f"{P}/projects/{pid}/source-images/{a['id']}").json()["items"]}[a["id"]]
    assert (same["row_px"], same["column_px"], same["revision"]) == (a["row_px"], a["column_px"], a["revision"])


def test_bulk_add_centres_on_the_position_the_editor_shows(client):
    pid = _project(client, "Suggest moved")["id"]
    a, _, _ = _image_items(client, pid)  # saved at (100, 100)
    url = f"{P}/projects/{pid}/source-images/{a['id']}/suggest"
    body = client.post(url, json={"item_id": a["id"], "grid": 3, "spacing_px": 100,
                                  "row_px": 400, "column_px": 500}).json()
    centres = {(c["row_px"], c["column_px"]) for c in body["candidates"]}
    assert len(centres) == 8 and (300, 400) in centres and (500, 600) in centres
    outside = client.post(url, json={"item_id": a["id"], "row_px": 800, "column_px": 10})
    assert outside.status_code == 422


def test_an_unreadable_source_image_is_a_clear_422_not_a_500(client, monkeypatch):
    from curation import image_cache, storage

    pid = _project(client, "Corrupt")["id"]
    item = _point(client, pid, image="corrupt")
    real = storage.read_bytes
    monkeypatch.setattr(storage, "read_bytes", lambda uri: b"not an image" if "corrupt" in uri else real(uri))
    image_cache.clear()
    base = f"{P}/projects/{pid}/source-images/{item['id']}"
    detail = client.get(base)
    assert detail.status_code == 422 and "could not be read" in detail.json()["detail"]
    edits = client.post(f"{base}/edits", json={"edits": [
        {"op": "move", "item_id": item["id"], "expected_revision": item["revision"], "row_px": 5, "column_px": 5}]})
    assert edits.status_code == 422 and "could not be read" in edits.json()["detail"]
    suggest = client.post(f"{base}/suggest", json={"item_id": item["id"]})
    assert suggest.status_code == 422


def test_an_image_outside_the_curated_buckets_is_never_read(client, db_session, monkeypatch):
    from curation import image_cache, storage
    from curation.models import CurationItem

    pid = _project(client, "Outside")["id"]
    item = _point(client, pid, key="o1")
    db_session.get(CurationItem, item["id"]).source_image_uri = "gs://elsewhere/images/o1.jpg"  # e.g. a direct DB edit
    db_session.commit()
    reads = []
    real = storage.read_bytes
    monkeypatch.setattr(storage, "read_bytes", lambda uri: reads.append(uri) or real(uri))
    image_cache.clear()
    base = f"{P}/projects/{pid}/source-images/{item['id']}"
    detail = client.get(base)
    assert detail.status_code == 200 and detail.json()["width"] is None
    assert client.post(f"{base}/suggest", json={"item_id": item["id"]}).status_code == 422
    assert not any("elsewhere" in uri for uri in reads)
