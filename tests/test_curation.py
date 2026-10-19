"""
Classification curation.

The first part is OASIS's own suite (test/test_classification_curation.py),
ported to /api/curation — it is the contract the rebuilt schemas had to meet.
The second part covers what OASIS did not do: several people at once.
"""
from __future__ import annotations

import csv
import io
import json
import threading
from contextlib import contextmanager

import pytest
from PIL import Image

P = "/api/curation"

CSV_HEADER = (
    "Name,Date,Region,Island,Site,Priority,Analyst,Height (cm),Latitude,Longitude,"
    "Depth,Camera,Photographer,Water quality,Strobes,Framing gear used,White balance card,"
    "Comments,Row,Column,Label code,Label ID,Annotator,Date annotated,Machine suggestion 1,"
    "Machine confidence 1,Machine suggestion 2,Machine confidence 2,Machine suggestion 3,"
    "Machine confidence 3,Machine suggestion 4,Machine confidence 4,Machine suggestion 5,"
    "Machine confidence 5\n"
)


@pytest.fixture(autouse=True)
def _test_buckets(monkeypatch):
    # The payloads below use gs://nmfs/... and gs://bucket/...; name them as
    # curated rather than relaxing the bucket rule for the tests.
    monkeypatch.setenv("CURATION_IMAGE_BUCKETS", "nmfs,bucket")


@pytest.fixture
def client(library):
    from fastapi.testclient import TestClient

    import main

    return TestClient(main.app)


@pytest.fixture
def db_session(library):
    from appdb.engine import new_session

    session = new_session()
    yield session
    session.close()


def _item_payload(source_key, label, region="Hawaii"):
    return {
        "source_key": source_key,
        "source_image_uri": f"gs://nmfs/images/{source_key}.jpg",
        "crop_uri": f"gs://nmfs/crops/{source_key}.jpg",
        "file_name": f"{source_key}.jpg",
        "source_label": label,
        "row_px": 120,
        "column_px": 240,
        "region": region,
        "site": "SITE-01",
        "survey_year": 2024,
    }


def _project(client, name, mode="T3"):
    response = client.post(f"{P}/projects", json={"name": name, "dataset_mode": mode})
    assert response.status_code == 201, response.text
    return response.json()


def _add(client, project_id, payload):
    response = client.post(f"{P}/projects/{project_id}/items", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _fake_storage(monkeypatch, reader):
    from curation import storage

    monkeypatch.setattr(storage, "read_bytes", reader)


def _jpeg(size=(400, 300), color=(20, 120, 180)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color=color).save(buffer, format="JPEG")
    return buffer.getvalue()


# ── OASIS suite, ported ───────────────────────────────────────────────────────


def test_registered_classifier_is_hashed_and_never_loaded_in_web(db_session, monkeypatch):
    import hashlib
    from fastapi import HTTPException
    from auth.service import local_user
    from curation import model_registry, settings

    monkeypatch.setenv("CURATION_ALLOW_LOCAL_MODELS", "on")
    model = model_registry.register_stream(db_session, io.BytesIO(b"trusted-fixture"), name="Fixture",
                                          filename="C:\\supplied\\model.pt", trusted=True, user=local_user())
    assert model.sha256 == hashlib.sha256(b"trusted-fixture").hexdigest()
    assert model.status == "staged" and model.class_names == []
    assert model_registry.model_path(model).read_bytes() == b"trusted-fixture"
    assert not model_registry.worker_available()
    with pytest.raises(HTTPException) as failure:
        model_registry.submit_validation(db_session, model)
    assert failure.value.status_code == 503


@pytest.mark.parametrize("payload,filename,trusted,status", [
    (b"", "model.pt", True, 422), (b"12345", "model.pt", True, 413),
    (b"123", "model.py", True, 422), (b"123", "model.pt", False, 422),
])
def test_classifier_upload_rejects_invalid_inputs(db_session, monkeypatch, payload, filename, trusted, status):
    from fastapi import HTTPException
    from auth.service import local_user
    from curation import model_registry, settings

    monkeypatch.setenv("CURATION_ALLOW_LOCAL_MODELS", "on")
    monkeypatch.setenv("CURATION_MODEL_MAX_BYTES", "4")
    with pytest.raises(HTTPException) as failure:
        model_registry.register_stream(db_session, io.BytesIO(payload), name="Fixture", filename=filename,
                                       trusted=trusted, user=local_user())
    assert failure.value.status_code == status
    assert not list((settings.eval_dir() / "models").glob("*.partial"))

def test_crop_grid_offers_bulk_relabel_with_a_reason(client):
    page = client.get("/curation").text
    for element in ('id="bulk-label"', 'id="bulk-reason"', 'id="select-page-crops"', 'id="relabel-options"', '<option value="relabel">Relabel to…</option>'):
        assert element in page
    script = client.get("/static/curation/curation.js").text
    assert "function validRelabelCode(" in script


def test_bulk_relabel_records_the_reason_on_every_event(client, db_session):
    from curation.models import CurationEvent

    pid = _project(client, "Bulk relabel")["id"]
    items = [_add(client, pid, _item_payload(f"rl-{n}", "ACBR")) for n in range(3)]
    response = client.post(f"{P}/projects/{pid}/items/bulk", json={
        "items": [{"id": i["id"], "expected_revision": i["revision"]} for i in items],
        "current_label": "POCS", "reason": "split ACBR vs POCS per new guide"})
    assert response.status_code == 200 and response.json()["updated"] == 3
    events = db_session.query(CurationEvent).filter(CurationEvent.event_type == "bulk_item_updated").all()
    assert {e.reason for e in events} == {"split ACBR vs POCS per new guide"}
    assert len({e.operation_id for e in events}) == 1


def test_t1_bulk_relabel_accepts_a_tier_one_class_code(client):
    pid = _project(client, "T1 relabel", mode="T1")["id"]
    items = [_add(client, pid, _item_payload(f"t1rl-{n}", "ACBR")) for n in range(2)]
    response = client.post(f"{P}/projects/{pid}/items/bulk", json={
        "items": [{"id": i["id"], "expected_revision": i["revision"]} for i in items],
        "current_label": "SC", "reason": "T1 class"})
    assert response.status_code == 200, response.text
    assert response.json()["updated"] == 2
    listed = client.get(f"{P}/projects/{pid}/items").json()
    rows = listed["items"] if isinstance(listed, dict) else listed
    assert {row["current_label"] for row in rows} == {"SC"}
    script = client.get("/static/curation/curation.js").text
    # the relabel targets come from the target tier's own class codes, as the server projects them
    assert "node.tier === targetTier && node.code" in script


def test_status_view_has_a_splits_card(client):
    page = client.get("/curation").text
    for element in ('id="splits-group"', 'id="splits-rare-only"', 'id="splits-table"', 'id="splits-download"'):
        assert element in page
    script = client.get("/static/curation/curation.js").text
    assert "function renderBreakdownTable(" in script and "data-version-breakdown" in script


def test_curation_page_renders_and_every_scripted_element_exists(client):
    page = client.get("/curation")
    assert page.status_code == 200
    assert "Classification Curation" in page.text
    assert "/static/curation/curation.js" in page.text
    assert '<canvas id="crop-image"' in page.text
    assert page.text.index('data-quality-scope="patch"') < page.text.index('data-quality-scope="source"')
    assert 'value="t3_v0_test"' in page.text
    assert 'id="curator-name"' not in page.text  # identity comes from the session now

    script = client.get("/static/curation/curation.js")
    assert script.status_code == 200
    for needle in ("function fitMatrixToWidth()", "function scoreImageQuality(img)", "function setupGridLasso(",
                   "function qualityReviewEndpoint(item)", "expected_revision", "function reportConflicts("):
        assert needle in script.text
    assert "curator_name:" not in script.text and "reviewer:" not in script.text  # never sent
    # Every scripted id resolving, and no duplicate ids, is checked on the rendered page by tests/test_ui_integrity.py.


def test_remove_exif_orientation_preserves_raw_image_pixels():
    from curation.images import remove_exif_orientation

    source = Image.new("RGB", (12, 8), color=(20, 120, 180))
    exif = source.getexif()
    exif[274] = 6
    buffer = io.BytesIO()
    source.save(buffer, format="JPEG", exif=exif)

    cleaned = remove_exif_orientation(buffer.getvalue())
    with Image.open(io.BytesIO(buffer.getvalue())) as before, Image.open(io.BytesIO(cleaned)) as after:
        assert before.size == after.size == (12, 8)
        assert before.getexif().get(274) == 6
        assert after.getexif().get(274) is None
        assert before.convert("RGB").tobytes() == after.convert("RGB").tobytes()


def test_project_queue_edit_and_stats(client, db_session):
    from curation.models import CurationEvent

    project = _project(client, "Pacific T3")
    first = _add(client, project["id"], _item_payload("hawaii-001", "TURFH"))
    _add(client, project["id"], _item_payload("hawaii-002", "CCAH"))

    first_page = client.get(f"{P}/projects/{project['id']}/items", params={"limit": 1}).json()
    assert first_page["returned"] == 1
    assert first_page["next_cursor"] == first_page["items"][0]["id"]
    second_page = client.get(f"{P}/projects/{project['id']}/items",
                             params={"limit": 1, "after_id": first_page["next_cursor"]}).json()
    assert second_page["returned"] == 1
    assert second_page["next_cursor"] is None

    update = client.patch(f"{P}/projects/{project['id']}/items/{first['id']}", json={
        "expected_revision": first["revision"], "current_label": "SAND", "included": False,
        "review_state": "reviewed", "reason": "Incorrect turf annotation", "curator_name": "ignored",
    })
    assert update.status_code == 200
    assert update.json()["revision"] == 2
    assert update.json()["current_label"] == "SAND"

    stale = client.patch(f"{P}/projects/{project['id']}/items/{first['id']}",
                         json={"expected_revision": 1, "included": True})
    assert stale.status_code == 409

    stats = client.get(f"{P}/projects/{project['id']}/stats").json()
    assert stats == {"total": 2, "included": 1, "excluded": 1, "reviewed": 1, "unreviewed": 1,
                     "flagged": 0, "class_counts": {"CCAH": 1}}

    event = db_session.query(CurationEvent).filter_by(event_type="item_updated").one()
    assert event.previous_value["current_label"] == "TURFH"
    assert event.new_value["current_label"] == "SAND"
    assert event.reason == "Incorrect turf annotation"
    assert event.curator_name == "Local user"  # the signed-in user, not what the client claimed


def test_items_filter_by_exact_matrix_item_ids(client):
    project_id = _project(client, "Matrix item filter")["id"]
    ids = [_add(client, project_id, _item_payload(f"matrix-filter-{i}", "ACTA"))["id"] for i in range(6)]
    response = client.get(f"{P}/projects/{project_id}/items",
                          params={"item_ids": ",".join(map(str, ids[1:5])), "limit": 200})
    assert [item["id"] for item in response.json()["items"]] == ids[1:5]
    assert response.json()["returned"] == 4


def test_quality_review_route_persists_decision_for_source_image(client, db_session):
    from curation.models import CurationItem

    project_id = _project(client, "Quality review project")["id"]
    item_id = _add(client, project_id, _item_payload("quality-001", "TURFH"))["id"]
    second = _item_payload("quality-002", "ACBR")
    second["source_image_uri"] = _item_payload("quality-001", "TURFH")["source_image_uri"]
    second_id = _add(client, project_id, second)["id"]

    response = client.post(f"{P}/projects/{project_id}/source-images/{item_id}/quality", json={
        "decision": "reject", "score": 38, "flags": ["too_dark", "blurry"], "reviewer": "ignored",
    })
    assert response.status_code == 200
    payload = response.json()
    assert payload["quality_decision"] == "reject"
    assert payload["quality_score"] == 38
    assert payload["quality_flags"] == ["too_dark", "blurry"]
    assert payload["quality_reviewed_by"] == "Local user"
    assert payload["included"] is False

    for row_id in (item_id, second_id):
        stored = db_session.get(CurationItem, row_id)
        assert stored.quality_decision == "reject"
        assert stored.quality_score == 38
        assert stored.quality_flags == ["too_dark", "blurry"]
        assert stored.included is False


def test_patch_quality_review_only_updates_selected_item(client, db_session):
    from curation.models import CurationItem

    project_id = _project(client, "Patch quality project")["id"]
    first = _item_payload("patch-quality-001", "TURFH")
    first_id = _add(client, project_id, first)["id"]
    second = _item_payload("patch-quality-002", "ACBR")
    second["source_image_uri"] = first["source_image_uri"]
    second_id = _add(client, project_id, second)["id"]

    response = client.post(f"{P}/projects/{project_id}/items/{first_id}/quality",
                           json={"decision": "reject", "score": 31, "flags": ["blurry"]})
    assert response.status_code == 200
    first_row, second_row = db_session.get(CurationItem, first_id), db_session.get(CurationItem, second_id)
    assert first_row.quality_decision == "reject"
    assert first_row.metadata_json["patch_quality_review"]["score"] == 31
    assert first_row.included is False
    assert second_row.quality_decision is None
    assert second_row.included is True


HAWAII = {"name": "Hawaii", "root_uri": "gs://bucket/ESD_REA_HAWAII_v2/",
          "csv_uri": "gs://bucket/ESD_REA_HAWAII_v2/annotations.csv"}


def _operation(db_session, project_id, source, status="running"):
    from curation.models import CurationImportOperation

    operation = CurationImportOperation(project_id=project_id, status=status, sources=[source], batch_size=100)
    db_session.add(operation)
    db_session.commit()
    return operation


def test_regional_csv_import_derives_crops_predictions_and_is_idempotent(client, db_session):
    from curation.importer import import_csv_stream
    from curation.models import CurationItem

    csv_body = CSV_HEADER + (
        "KUR-4255_2024_A_17.JPG,2024-08-01,NWHI,Kure,KUR-4255,3,USER5,,28.4,-178.2,"
        ",,,,,,,,1654,495,*TURFH,1348,Analyst,2025-06-25,*TURFH,81,*EMA,6,,,,,,\n"
        "KUR-4255_2024_A_17.JPG,2024-08-01,NWHI,Kure,KUR-4255,3,USER5,,28.4,-178.2,"
        ",,,,,,,,2851,1911,*ACBR,1269,Analyst,2025-06-25,*POCS,80,*TURFH,5,,,,,,\n"
        "KUR-4255_2024_A_18.JPG,2024-08-01,NWHI,Kure,KUR-4255,3,USER5,,28.4,-178.2,"
        ",,,,,,,,bad,1911,*ACBR,1269,Analyst,2025-06-25,,,,,,,,,,\n"
    )
    project = _project(client, "Import test")
    operation = _operation(db_session, project["id"], HAWAII)
    import_csv_stream(db_session, operation, HAWAII, io.StringIO(csv_body), threading.Event())

    items = db_session.query(CurationItem).order_by(CurationItem.id).all()
    assert len(items) == 2
    assert (operation.processed_rows, operation.imported_rows, operation.rejected_rows) == (3, 2, 1)
    assert items[0].source_label == "TURFH"
    assert items[0].crop_uri.endswith("/crops/TURFH/TURFH_KUR-4255_2024_A_17_1.png")
    assert items[1].crop_uri.endswith("/crops/ACBR/ACBR_KUR-4255_2024_A_17_2.png")
    assert items[0].top_predictions == [
        {"label": "TURFH", "confidence": 0.81, "rank": 1}, {"label": "EMA", "confidence": 0.06, "rank": 2},
    ]
    assert items[0].survey_year == 2024
    assert items[0].island == "Kure" and items[0].annotator == "Analyst"

    second = _operation(db_session, project["id"], HAWAII)
    import_csv_stream(db_session, second, HAWAII, io.StringIO(csv_body), threading.Event())
    assert (second.imported_rows, second.skipped_rows) == (0, 2)
    assert db_session.query(CurationItem).count() == 2


def test_import_operation_api_queues_lists_and_cancels(client):
    project = _project(client, "Import API")
    sources = client.get(f"{P}/import-sources").json()["sources"]
    assert [source["name"] for source in sources] == ["Hawaii", "Mariana", "Samoa/PRIA"]

    created = client.post(f"{P}/projects/{project['id']}/imports", json={"sources": [sources[0]], "batch_size": 500})
    assert created.status_code == 202
    operation = created.json()
    assert operation["status"] == "queued"
    assert operation["batch_size"] == 500
    assert operation["sources"][0]["name"] == "Hawaii"
    assert operation["created_by_name"] == "Local user"

    listed = client.get(f"{P}/projects/{project['id']}/imports").json()
    assert [row["id"] for row in listed] == [operation["id"]]

    cancelled = client.post(f"{P}/projects/{project['id']}/imports/{operation['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "canceled"


def test_import_worker_completes_from_stream_and_updates_project(client, db_session, monkeypatch):
    from curation import importer, storage
    from curation.models import CurationEvent, CurationImportOperation, CurationProject

    csv_body = CSV_HEADER + (
        "BAK-02_2018_A_01.JPG,2018-06-15,PRIAs,BAK,BAK-02,3,USER1,100,0.1,-176.4,"
        ",,,,,,,,1378,727,*ACBR,1269,Analyst,2019-05-21,,,,,,,,,,\n"
    )
    source = {"name": "Samoa/PRIA", "root_uri": "gs://bucket/ESD_REA_SAMOA_PRIA_v2/",
              "csv_uri": "gs://bucket/ESD_REA_SAMOA_PRIA_v2/annotations.csv"}

    @contextmanager
    def open_text(uri):
        assert uri == source["csv_uri"]
        yield io.StringIO(csv_body)

    monkeypatch.setattr(storage, "open_text", open_text)
    project = _project(client, "Worker import")
    operation_id = _operation(db_session, project["id"], source, status="starting").id

    importer.run_import(operation_id, threading.Event())

    db_session.expire_all()
    operation = db_session.get(CurationImportOperation, operation_id)
    assert operation.status == "completed"
    assert (operation.processed_rows, operation.imported_rows) == (1, 1)
    assert operation.current_source is None
    assert db_session.get(CurationProject, project["id"]).item_count == 1
    event = db_session.query(CurationEvent).filter_by(operation_id=operation_id).one()
    assert event.new_value == {"processed": 1, "imported": 1, "skipped": 0, "rejected": 0}


def test_crop_preview_uses_stored_crop(client, monkeypatch):
    def read(uri):
        assert "/crops/" in uri
        return b"stored-crop"

    _fake_storage(monkeypatch, read)
    project = _project(client, "Stored crop")
    item = _add(client, project["id"], _item_payload("stored-001", "TURFH"))
    response = client.get(f"{P}/projects/{project['id']}/items/{item['id']}/crop-preview")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == b"stored-crop"


def test_crop_preview_reconstructs_missing_crop_from_source(client, monkeypatch):
    source = _jpeg()

    def read(uri):
        if "/images/" in uri:
            return source
        raise FileNotFoundError(uri)

    _fake_storage(monkeypatch, read)
    project = _project(client, "Generated crop")
    item = _add(client, project["id"], _item_payload("generated-001", "ACBR"))
    response = client.get(f"{P}/projects/{project['id']}/items/{item['id']}/crop-preview")
    assert response.status_code == 200
    with Image.open(io.BytesIO(response.content)) as crop:
        assert crop.size == (224, 224)
        assert crop.format == "PNG"


def test_source_image_queue_groups_annotations_and_filters_descendant_codes(client):
    project = _project(client, "Grouped review")
    shared = "gs://nmfs/images/source-01.jpg"
    shared_ids = []
    for index, label in enumerate(["ACBR", "TURFH", "ACBR"], start=1):
        payload = _item_payload(f"source-01-{index}", label)
        payload.update(source_image_uri=shared, row_px=100 * index, column_px=200 * index)
        shared_ids.append(_add(client, project["id"], payload)["id"])
    other = _item_payload("source-02-1", "SAND")
    other["source_image_uri"] = "gs://nmfs/images/source-02.jpg"
    _add(client, project["id"], other)

    page = client.get(f"{P}/projects/{project['id']}/source-images").json()
    assert page["returned"] == 2
    assert page["images"][0]["annotation_count"] == 3
    assert page["images"][0]["labels"] == ["ACBR", "TURFH"]

    detail = client.get(f"{P}/projects/{project['id']}/source-images/{shared_ids[1]}").json()
    assert detail["first_item_id"] == shared_ids[0]
    assert detail["source_image_uri"] == shared
    assert [item["current_label"] for item in detail["items"]] == ["ACBR", "TURFH", "ACBR"]

    filtered = client.get(f"{P}/projects/{project['id']}/source-images", params={"labels": "ACBR,POCS"}).json()
    assert filtered["returned"] == 1
    assert filtered["images"][0]["annotation_count"] == 2
    assert filtered["images"][0]["labels"] == ["ACBR"]

    searched = client.get(f"{P}/projects/{project['id']}/source-images", params={"image_search": "source-02"}).json()
    assert searched["returned"] == 1
    assert searched["images"][0]["image_name"] == "source-02.jpg"

    linked = client.get(f"{P}/projects/{project['id']}/source-images", params={"item_ids": str(shared_ids[1])}).json()
    assert linked["returned"] == 1
    assert linked["images"][0]["annotation_count"] == 1

    unmatched = client.get(f"{P}/projects/{project['id']}/source-images", params={"item_ids": "0"}).json()
    assert unmatched == {"images": [], "next_cursor": None, "next_missing": 0, "returned": 0}


def test_taxonomy_tree_exposes_tier_descendant_codes():
    from curation.taxonomy import load_taxonomy_nodes

    nodes = load_taxonomy_nodes()
    coral = next(n for n in nodes if n["tier"] == "TIER_1" and n["code"] == "CORAL")
    branching = next(n for n in nodes if n["tier"] == "TIER_2" and n["code"] == "BR")
    assert "ACBR" in coral["codes"]
    assert "ACBR" in branching["codes"]
    assert coral["depth"] == 1


def test_t1_project_round_trips_and_import_projects_effective_label(client, db_session):
    from curation.importer import import_csv_stream
    from curation.models import CurationItem, CurationProject

    csv_body = CSV_HEADER + (
        "KUR-4255_2024_A_17.JPG,2024-08-01,NWHI,Kure,KUR-4255,3,USER5,,28.4,-178.2,"
        ",,,,,,,,2851,1911,*ACBR,1269,Analyst,2025-06-25,*POCS,80,*TURFH,5,,,,,,\n"
    )
    project = _project(client, "Pacific T1", mode="T1")
    assert project["dataset_mode"] == "T1"
    assert db_session.get(CurationProject, project["id"]).source_config["dataset_mode"] == "T1"

    operation = _operation(db_session, project["id"], HAWAII)
    import_csv_stream(db_session, operation, HAWAII, io.StringIO(csv_body), threading.Event(), "T1")
    item = db_session.query(CurationItem).one()
    assert item.source_label == "ACBR"
    assert item.current_label == "CORAL"
    assert item.crop_uri.endswith("/pacific_t1/CORAL/ACBR_KUR-4255_2024_A_17_1.png")


def test_crop_grid_item_filters_match_group_filters(client):
    project = _project(client, "Crop filters")
    for key, label in [("KUR-01", "ACBR"), ("KUR-02", "TURFH"), ("FFS-01", "SAND")]:
        _add(client, project["id"], _item_payload(key, label))
    page = client.get(f"{P}/projects/{project['id']}/items", params={"labels": "ACBR,TURFH", "image_search": "KUR"}).json()
    assert [item["current_label"] for item in page["items"]] == ["ACBR", "TURFH"]


@pytest.mark.parametrize("found_in", ["/pacific_all/ACBR/", "/pacific_t3_splits/test/ACBR/"])
def test_crop_preview_falls_back_to_canonical_and_split_crops(client, monkeypatch, found_in):
    requested = []

    def read(uri):
        requested.append(uri)
        if found_in in uri:
            return b"fallback-crop"
        raise FileNotFoundError(uri)

    _fake_storage(monkeypatch, read)
    project = _project(client, "Fallback " + found_in)
    item = _add(client, project["id"], _item_payload("KUR-4255_2024_A_17_7", "ACBR"))
    response = client.get(f"{P}/projects/{project['id']}/items/{item['id']}/crop-preview")
    assert response.status_code == 200
    assert response.content == b"fallback-crop"
    assert any(found_in in uri for uri in requested)


def test_facets_filtered_exports_and_guarded_project_deletion(client):
    project = _project(client, "Facet Export")
    for key, label, region, island, site, year in [
        ("KUR-01", "ACBR", "NWHI", "Kure", "SITE-A", 2024),
        ("KUR-02", "TURFH", "NWHI", "Kure", "SITE-A", 2024),
        ("GUA-01", "SAND", "Marianas", "Guam", "SITE-B", 2022),
    ]:
        payload = _item_payload(key, label, region)
        payload.update(site=site, survey_year=year, metadata={"island": island})
        _add(client, project["id"], payload)

    facets = client.get(f"{P}/projects/{project['id']}/facets").json()
    assert facets["regions"][0] == {"value": "NWHI", "count": 2}
    assert facets["islands"][0] == {"value": "Kure", "count": 2}
    assert {row["value"] for row in facets["years"]} == {"2022", "2024"}
    assert facets["sites"][0] == {"value": "SITE-A", "count": 2}
    assert {row["value"] for row in facets["labels"]} == {"ACBR", "TURFH", "SAND"}

    cascaded = client.get(f"{P}/projects/{project['id']}/facets", params={"region": "Marianas", "island": "Guam"}).json()
    assert cascaded["years"] == [{"value": "2022", "count": 1}]
    assert cascaded["sites"] == [{"value": "SITE-B", "count": 1}]
    assert cascaded["islands"] == [{"value": "Guam", "count": 1}]
    assert cascaded["regions"] == [{"value": "Marianas", "count": 1}]

    filters = {"labels": "ACBR,TURFH", "region": "NWHI", "island": "Kure", "year": 2024, "site": "SITE-A"}
    assert client.get(f"{P}/projects/{project['id']}/items", params=filters).json()["returned"] == 2
    assert client.get(f"{P}/projects/{project['id']}/source-images", params=filters).json()["returned"] == 2

    csv_response = client.get(f"{P}/projects/{project['id']}/export", params={**filters, "export_format": "csv"})
    assert csv_response.status_code == 200
    assert "Facet_Export_filtered.csv" in csv_response.headers["content-disposition"]
    assert "KUR-01" in csv_response.text and "KUR-02" in csv_response.text
    assert "GUA-01" not in csv_response.text

    jsonl = client.get(f"{P}/projects/{project['id']}/export", params={**filters, "export_format": "jsonl"})
    lines = [json.loads(line) for line in jsonl.text.splitlines()]
    assert {line["source_key"] for line in lines} == {"KUR-01", "KUR-02"}
    assert {line["island"] for line in lines} == {"Kure"}

    sources = client.get(f"{P}/import-sources").json()["sources"]
    operation = client.post(f"{P}/projects/{project['id']}/imports", json={"sources": [sources[0]], "batch_size": 500}).json()
    assert client.delete(f"{P}/projects/{project['id']}", params={"confirm_name": project["name"]}).status_code == 409
    client.post(f"{P}/projects/{project['id']}/imports/{operation['id']}/cancel")
    assert client.delete(f"{P}/projects/{project['id']}", params={"confirm_name": "wrong"}).status_code == 409
    assert client.delete(f"{P}/projects/{project['id']}", params={"confirm_name": project["name"]}).status_code == 200
    assert client.get(f"{P}/projects/{project['id']}").status_code == 404


def test_manual_point_bulk_update_provenance_filters_and_status(client, db_session):
    from curation.models import CurationEvent, CurationItem

    project = _project(client, "Point and bulk")
    payload = _item_payload("source-human", "POCS")
    payload["metadata"] = {"island": "Guam", "annotator": "Alleviate"}
    anchor = _add(client, project["id"], payload)

    point = client.post(f"{P}/projects/{project['id']}/source-images/{anchor['id']}/points", json={
        "row_px": 333, "column_px": 444, "label": "POCS", "crop_size": 224, "reason": "Nearby colony",
    })
    assert point.status_code == 201
    manual = point.json()
    assert manual["source_image_uri"] == anchor["source_image_uri"]
    assert manual["metadata"]["manual_point"] is True
    assert manual["metadata"]["annotator"] == "Local user"
    assert manual["review_state"] == "reviewed"

    bulk = client.post(f"{P}/projects/{project['id']}/items/bulk", json={
        "item_ids": [anchor["id"], manual["id"]], "included": False, "review_state": "flagged",
        "reason": "Targeted QA hold",
    })
    assert bulk.status_code == 200
    assert bulk.json()["updated"] == 2

    db_session.get(CurationItem, anchor["id"]).model_label = "POCS"
    db_session.commit()
    events = db_session.query(CurationEvent).filter_by(event_type="bulk_item_updated").all()
    assert len(events) == 2
    assert len({event.operation_id for event in events}) == 1

    def count(**params):
        return client.get(f"{P}/projects/{project['id']}/items", params=params).json()["returned"]

    assert count(annotator="Alleviate") == 1
    assert count(annotator="Local user") == 1
    assert count(label_origin="classifier") == 1

    facets = client.get(f"{P}/projects/{project['id']}/facets").json()
    assert {row["value"] for row in facets["annotators"]} == {"Alleviate", "Local user"}

    status = client.get(f"{P}/projects/{project['id']}/status").json()
    assert (status["total_items"], status["included_items"], status["excluded_items"]) == (2, 0, 2)
    assert status["source_images"] == 1
    assert status["classifier_predictions"] == 1
    assert status["labels"] == [{"label": "POCS", "total": 2, "included": 0, "reviewed": 0, "source_images": 1}]


def test_targeted_classifier_batches_dense_patches_and_rejects_detection_model(monkeypatch, library):
    from fastapi import HTTPException

    from curation import scoring
    from curation.schemas import CurationClassifyRequest

    _fake_storage(monkeypatch, lambda uri: _jpeg((500, 500)))

    class Probabilities:
        top5 = [1, 0]
        top5conf = type("Confidences", (), {"tolist": lambda self: [0.8, 0.2]})()

    class ClassificationResult:
        probs = Probabilities()
        names = {0: "TURFH", 1: "POCS"}

    class ClassificationModel:
        def predict(self, source, imgsz, verbose):
            assert len(source) == 4 and imgsz == 224
            return [ClassificationResult() for _ in source]

    monkeypatch.setattr(scoring, "load_model", lambda name: ClassificationModel())
    request = CurationClassifyRequest(row_px=250, column_px=250, model_name="classifier.pt",
                                      crop_size=224, radius=56, step=112)
    result = scoring.classify_neighborhood("gs://bucket/image.jpg", request)
    assert len(result) == 4
    assert result[0]["predictions"][0] == {"label": "POCS", "confidence": 0.8, "rank": 1}

    class DetectionResult:
        probs = None

    class DetectionModel:
        def predict(self, source, imgsz, verbose):
            return [DetectionResult() for _ in source]

    monkeypatch.setattr(scoring, "load_model", lambda name: DetectionModel())
    with pytest.raises(HTTPException) as caught:
        scoring.classify_neighborhood("gs://bucket/image.jpg", request)
    assert caught.value.status_code == 422
    assert "not an image-classification model" in caught.value.detail


def test_confusion_matrix_metrics_normalization_and_cell_drilldown(client, db_session):
    from curation.models import CurationItem

    project = _project(client, "Matrix metrics")
    ids = [_add(client, project["id"], _item_payload(f"matrix-{i}", label))["id"]
           for i, label in enumerate(["ACBR", "ACBR", "POCS", "POCS", "SAND"])]
    for item_id, prediction in zip(ids, ["ACBR", "POCS", "POCS", "ACBR", None]):
        db_session.get(CurationItem, item_id).model_label = prediction
    db_session.commit()

    matrix = client.get(f"{P}/projects/{project['id']}/confusion-matrix",
                        params={"normalization": "row", "actual_axis": "source"}).json()
    assert (matrix["scored_count"], matrix["unscored_count"], matrix["correct_count"]) == (4, 1, 2)
    assert matrix["accuracy"] == 0.5
    assert matrix["model"]["provenance_complete"] is False
    cells = {(cell["actual"], cell["predicted"]): cell for cell in matrix["cells"]}
    assert cells[("ACBR", "ACBR")] == {"actual": "ACBR", "predicted": "ACBR", "count": 1, "value": 0.5}
    assert {row["label"]: row for row in matrix["rows"]}["POCS"]["metric"] == 0.5
    assert {col["label"]: col for col in matrix["columns"]}["ACBR"]["metric"] == 0.5

    drilldown = client.get(f"{P}/projects/{project['id']}/items", params={"source_labels": "ACBR", "model_labels": "POCS"}).json()
    assert drilldown["returned"] == 1
    assert drilldown["items"][0]["id"] == ids[1]


def test_t1_confusion_matrix_projects_raw_predictions(client, db_session):
    from curation.models import CurationItem

    project = _project(client, "T1 matrix", mode="T1")
    item = _add(client, project["id"], _item_payload("t1-matrix", "ACBR"))
    db_session.get(CurationItem, item["id"]).model_label = "POCS"
    db_session.commit()
    matrix = client.get(f"{P}/projects/{project['id']}/confusion-matrix", params={"actual_axis": "current"}).json()
    assert matrix["accuracy"] == 1.0
    assert matrix["cells"] == [{"actual": "CORAL", "predicted": "CORAL", "count": 1, "value": 1.0}]


@pytest.fixture
def pinned_eval(library, tmp_path, monkeypatch):
    """A small, internally consistent stand-in for the real T3 v0 artifacts."""
    from curation import evaluation

    directory = tmp_path / "eval" / "v0"
    directory.mkdir(parents=True)
    labels = ["CCAH", "TURFH", "SAND"]
    rows = []
    for index in range(12):
        actual = labels[index % 3]
        predicted = "TURFH" if actual == "CCAH" else actual
        confidence = round(0.95 - index * 0.05, 2)
        rows.append({
            "relative_path": f"{actual}/{actual}_{index}.png", "gcs_uri": f"gs://bucket/test/{actual}/{actual}_{index}.png",
            "actual_label": actual, "predicted_label": predicted, "top1_confidence": confidence,
            "top5_correct": "True",
            **{f"top{r}_label": labels[(r + index) % 3] for r in range(2, 6)},
            **{f"top{r}_confidence": 0.01 for r in range(2, 6)},
        })
    counts = [[sum(1 for row in rows if row["actual_label"] == a and row["predicted_label"] == p) for p in labels]
              for a in labels]
    with (directory / "predictions.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (directory / "confusion_matrix.json").write_text(json.dumps({"labels": labels, "counts": counts}))
    metrics = {"images": len(rows), "top1_accuracy": 8 / 12, "top5_accuracy": 1.0, "balanced_accuracy": 0.66,
               **{key: 0.5 for key in ("macro_precision", "macro_recall", "macro_f1", "weighted_precision",
                                       "weighted_recall", "weighted_f1")}}
    (directory / "metrics.json").write_text(json.dumps(metrics))
    (directory / "provenance.json").write_text(json.dumps({
        "status": "completed", "model_revision": evaluation.EXPECTED_MODEL_REVISION,
        "dataset_revision": evaluation.EXPECTED_DATASET_REVISION, "class_names": labels,
        "model_repository": "NMFS-OSI/example", "model_sha256": "0" * 64, "dataset_repository": "NMFS-OSI/data",
        "dataset_split": "test", "evaluated_images": len(rows), "completed_at": "2026-01-01T00:00:00Z",
    }))
    monkeypatch.setenv("CURATION_EVAL_DIR", str(tmp_path / "eval"))
    return labels


def test_pinned_matrix_and_evaluation_prediction_drilldown(client, pinned_eval):
    project = _project(client, "V0 matrix host")
    assert client.get(f"{P}/evaluations").json()["evaluations"][0]["available"] is True
    matrix = client.get(f"{P}/projects/{project['id']}/confusion-matrix",
                        params={"matrix_source": "t3_v0_test", "normalization": "row"}).json()
    assert matrix["matrix_source"] == "t3_v0_test"
    assert matrix["evaluation_id"] == "t3-v0-test"
    assert matrix["drilldown_kind"] == "evaluation_predictions"
    assert matrix["scored_count"] == 12
    assert len(matrix["rows"]) == 3
    assert matrix["model"]["provenance_complete"] is True

    page = client.get(f"{P}/evaluations/t3-v0-test/predictions",
                      params={"actual_label": "CCAH", "predicted_label": "TURFH", "limit": 2}).json()
    assert page["returned"] == 2 and page["next_cursor"] is not None
    assert all(item["actual_label"] == "CCAH" and item["predicted_label"] == "TURFH" for item in page["items"])
    assert all(len(item["top_predictions"]) == 5 for item in page["items"])
    following = client.get(f"{P}/evaluations/t3-v0-test/predictions", params={
        "actual_label": "CCAH", "predicted_label": "TURFH", "limit": 2, "after_row": page["next_cursor"],
    }).json()
    assert not {i["row_id"] for i in page["items"]} & {i["row_id"] for i in following["items"]}


def test_pinned_matrix_reports_unavailable_rather_than_failing(client, monkeypatch, tmp_path):
    monkeypatch.setenv("CURATION_EVAL_DIR", str(tmp_path / "nothing-here"))
    project = _project(client, "No artifacts")
    assert client.get(f"{P}/evaluations").json()["evaluations"][0]["available"] is False
    response = client.get(f"{P}/projects/{project['id']}/confusion-matrix", params={"matrix_source": "t3_v0_test"})
    assert response.status_code == 404
    assert "not installed" in response.json()["detail"]


def test_matrix_split_assets_crosswalk_and_bulk_relabel(client, db_session, monkeypatch):
    from curation import evaluation
    from curation.models import CurationEvent, CurationItem

    project = _project(client, "Split crosswalk")

    def add(key, file_name, label):
        payload = _item_payload(key, label)
        payload["file_name"] = file_name
        return _add(client, project["id"], payload)

    unique = add("unique", "ACBR_unique.png", "ACBR")
    add("ambiguous-1", "POCS_ambiguous.png", "POCS")
    add("ambiguous-2", "POCS_ambiguous.png", "POCS")

    def asset(split, label, name):
        return {"asset_id": f"{split}:{label}:{name}", "split": split, "gcs_uri": f"gs://bucket/{split}/{label}/{name}",
                "file_name": name, "actual_label": label, "predicted_label": None, "top1_confidence": None,
                "top_predictions": []}

    def fake_split_crops(*, split, labels, cursor, limit):
        assert split in {"train", "val"} and labels == ["ACBR", "POCS"]
        return {"items": [asset(split, "ACBR", "ACBR_unique.png"), asset(split, "POCS", "POCS_ambiguous.png"),
                          asset(split, "ACBR", "ACBR_missing.png")], "next_cursor": "next-split-page", "returned": 3}

    monkeypatch.setattr(evaluation, "list_split_crops", fake_split_crops)
    train = client.get(f"{P}/projects/{project['id']}/matrix-assets",
                       params={"split": "train", "actual_label": "ACBR", "predicted_label": "POCS"}).json()
    assert train["next_cursor"] == "next-split-page"
    by_name = {item["file_name"]: item for item in train["items"]}
    assert by_name["ACBR_unique.png"]["mutable"] is True
    assert by_name["ACBR_unique.png"]["curation_link"]["item_id"] == unique["id"]
    assert by_name["ACBR_unique.png"]["curation_link"]["revision"] == unique["revision"]
    assert by_name["POCS_ambiguous.png"]["mutable"] is False
    assert by_name["POCS_ambiguous.png"]["curation_link"]["match_status"] == "ambiguous"
    assert by_name["ACBR_missing.png"]["curation_link"]["match_status"] == "unmatched"

    relabel = client.post(f"{P}/projects/{project['id']}/items/bulk", json={
        "item_ids": [unique["id"]], "current_label": "POCS", "reason": "Matrix train relabel",
    })
    assert relabel.status_code == 200
    assert db_session.get(CurationItem, unique["id"]).current_label == "POCS"
    event = db_session.query(CurationEvent).filter_by(event_type="bulk_item_updated").one()
    assert event.previous_value["current_label"] == "ACBR"
    assert event.new_value["current_label"] == "POCS"


def test_matrix_test_assets_are_immutable_but_can_link_source(client, monkeypatch, pinned_eval):
    from curation import evaluation

    project = _project(client, "Test evidence links")
    payload = _item_payload("test-link", "CCAH")
    payload["file_name"] = "CCAH_test.png"
    linked = _add(client, project["id"], payload)

    monkeypatch.setattr(evaluation, "list_evaluation_predictions", lambda *a, **k: {
        "items": [{"row_id": 7, "relative_path": "CCAH/CCAH_test.png", "gcs_uri": "gs://bucket/test/CCAH/CCAH_test.png",
                   "actual_label": "CCAH", "predicted_label": "TURFH", "top1_confidence": 0.91, "top5_correct": False,
                   "top_predictions": [{"rank": 1, "label": "TURFH", "confidence": 0.91}]}],
        "next_cursor": 9, "returned": 1,
    })
    page = client.get(f"{P}/projects/{project['id']}/matrix-assets",
                      params={"split": "test", "actual_label": "CCAH", "predicted_label": "TURFH"}).json()
    item = page["items"][0]
    assert item["mutable"] is False
    assert item["curation_link"]["match_status"] == "unique"
    assert item["curation_link"]["item_id"] == linked["id"]
    assert item["curation_link"]["source_image_uri"] == linked["source_image_uri"]
    assert page["next_cursor"] == "9"


def test_matrix_test_evidence_confidence_sort_and_pagination(client, pinned_eval):
    project = _project(client, "Evidence sorting")
    base = {"split": "test", "actual_label": "CCAH", "predicted_label": "TURFH", "limit": 2}
    high = client.get(f"{P}/projects/{project['id']}/matrix-assets", params={**base, "sort_by": "confidence_desc"}).json()
    low = client.get(f"{P}/projects/{project['id']}/matrix-assets", params={**base, "sort_by": "confidence_asc"}).json()
    following = client.get(f"{P}/projects/{project['id']}/matrix-assets",
                           params={**base, "sort_by": "confidence_desc", "cursor": high["next_cursor"]}).json()
    high_conf = [item["top1_confidence"] for item in high["items"]]
    low_conf = [item["top1_confidence"] for item in low["items"]]
    assert high_conf == sorted(high_conf, reverse=True)
    assert low_conf == sorted(low_conf)
    assert min(high_conf) > max(low_conf)
    assert not {i["asset_id"] for i in high["items"]} & {i["asset_id"] for i in following["items"]}


# ── Several people at once ────────────────────────────────────────────────────

ADMIN = ("lead-admin", "admin-pass-1")


@pytest.fixture
def team(library, monkeypatch):
    """Login on; an admin, two curators and a viewer, each with their own session."""
    from fastapi.testclient import TestClient

    from appdb.bootstrap import init_app_db

    monkeypatch.setenv("OCEANFRAME_AUTH", "on")
    monkeypatch.setenv("OCEANFRAME_ADMIN_USERNAME", ADMIN[0])
    monkeypatch.setenv("OCEANFRAME_ADMIN_PASSWORD", ADMIN[1])
    init_app_db()
    import main

    people = {}
    for name in ("admin", "ana", "ben", "vic"):
        people[name] = TestClient(main.app)
    people["admin"].post("/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]})
    for name, role in (("ana", "curator"), ("ben", "curator"), ("vic", "viewer")):
        response = people["admin"].post("/api/auth/admin/users", json={
            "username": name, "password": "pass-word-1", "role": role, "display_name": name.title(),
        })
        assert response.status_code == 200, response.text
        people[name].post("/api/auth/login", json={"username": name, "password": "pass-word-1"})
    return people


def _join(team, project_id, name, role="curator"):
    response = team["admin"].post(f"{P}/projects/{project_id}/members", json={"username": name, "role": role})
    assert response.status_code in (201, 409), response.text


def test_second_editor_gets_a_conflict_naming_the_first(team):
    project = _project(team["admin"], "Shared")
    _join(team, project["id"], "ana")
    _join(team, project["id"], "ben")
    item = _add(team["admin"], project["id"], _item_payload("shared-1", "ACBR"))
    url = f"{P}/projects/{project['id']}/items/{item['id']}"

    first = team["ana"].patch(url, json={"expected_revision": item["revision"], "current_label": "POCS"})
    assert first.status_code == 200
    assert first.json()["updated_by_name"] == "Ana"

    second = team["ben"].patch(url, json={"expected_revision": item["revision"], "current_label": "SAND"})
    assert second.status_code == 409
    detail = second.json()["detail"]
    assert detail["updated_by_name"] == "Ana"
    assert "Ana" in detail["message"]
    assert team["admin"].get(f"{P}/projects/{project['id']}/items").json()["items"][0]["current_label"] == "POCS"


def test_bulk_applies_fresh_items_and_reports_stale_ones(team):
    project = _project(team["admin"], "Bulk conflicts")
    _join(team, project["id"], "ana")
    _join(team, project["id"], "ben")
    a = _add(team["admin"], project["id"], _item_payload("bulk-a", "ACBR"))
    b = _add(team["admin"], project["id"], _item_payload("bulk-b", "ACBR"))
    team["ana"].patch(f"{P}/projects/{project['id']}/items/{b['id']}",
                      json={"expected_revision": b["revision"], "review_state": "flagged"})

    response = team["ben"].post(f"{P}/projects/{project['id']}/items/bulk", json={
        "items": [{"id": a["id"], "expected_revision": a["revision"]},
                  {"id": b["id"], "expected_revision": b["revision"]}],
        "included": False,
    })
    body = response.json()
    assert body["updated"] == 1
    assert body["conflicts"] == [{"id": b["id"], "revision": b["revision"] + 1, "updated_by_name": "Ana"}]


def test_quality_review_respects_the_revision_lock_and_is_audited(team, library):
    from appdb import session_scope
    from curation.models import CurationEvent

    project = _project(team["admin"], "Quality lock")
    _join(team, project["id"], "ana")
    _join(team, project["id"], "ben")
    item = _add(team["admin"], project["id"], _item_payload("q-1", "ACBR"))
    url = f"{P}/projects/{project['id']}/items/{item['id']}/quality"

    ok = team["ana"].post(url, json={"decision": "accept", "score": 90, "expected_revision": item["revision"]})
    assert ok.status_code == 200
    assert ok.json()["revision"] == item["revision"] + 1
    stale = team["ben"].post(url, json={"decision": "reject", "expected_revision": item["revision"]})
    assert stale.status_code == 409

    with session_scope() as s:
        event = s.query(CurationEvent).filter_by(event_type="quality_reviewed").one()
        assert event.curator_name == "Ana"
        assert event.new_value["quality_decision"] == "accept"


def test_events_and_activity_carry_the_real_people(team):
    project = _project(team["admin"], "Attribution")
    _join(team, project["id"], "ana")
    _join(team, project["id"], "ben")
    item = _add(team["admin"], project["id"], _item_payload("attr-1", "ACBR"))
    team["ana"].patch(f"{P}/projects/{project['id']}/items/{item['id']}",
                      json={"expected_revision": item["revision"], "review_state": "reviewed"})
    team["ben"].post(f"{P}/projects/{project['id']}/items/bulk",
                     json={"item_ids": [item["id"]], "included": False})

    activity = team["vic"].get(f"{P}/projects/{project['id']}/activity").json()
    names = [event["curator_name"] for event in activity["events"]]
    assert names[:2] == ["Ben", "Ana"]
    assert {c["name"]: c["changes"] for c in activity["curators"]} == {"Ana": 1, "Ben": 1}

    audit = team["vic"].get(f"{P}/projects/{project['id']}/events/export")
    assert audit.status_code == 200
    assert "Ana" in audit.text and "Ben" in audit.text

    exported = team["vic"].get(f"{P}/projects/{project['id']}/export", params={"export_format": "jsonl"})
    assert json.loads(exported.text.splitlines()[0])["updated_by"] == "Ben"


def test_roles_gate_curation_writes(team, monkeypatch):
    project = _project(team["admin"], "Roles")
    item = _add(team["admin"], project["id"], _item_payload("roles-1", "ACBR"))
    sources = team["ana"].get(f"{P}/import-sources").json()["sources"]

    # Viewers read and export, but change nothing.
    assert team["vic"].get(f"{P}/projects/{project['id']}/items").status_code == 200
    assert team["vic"].patch(f"{P}/projects/{project['id']}/items/{item['id']}",
                             json={"expected_revision": 1, "included": False}).status_code == 403
    assert team["vic"].post(f"{P}/projects", json={"name": "Nope"}).status_code == 403
    assert team["vic"].post(f"{P}/projects/{project['id']}/imports", json={"sources": sources[:1]}).status_code == 403
    # Curators curate and, by default, create projects; deleting stays with leads.
    assert team["ana"].post(f"{P}/projects", json={"name": "Ana's"}).status_code == 201
    assert team["ana"].delete(f"{P}/projects/{project['id']}", params={"confirm_name": "Roles"}).status_code == 403

    # CURATION_CREATE_ROLE raises the bar, read on every request.
    monkeypatch.setenv("CURATION_CREATE_ROLE", "lead")
    response = team["ana"].post(f"{P}/projects", json={"name": "Nope"})
    assert response.status_code == 403 and "lead" in response.json()["detail"]
    # Importing is project work: refused to a non-member once the project is members-only.
    assert team["admin"].put(f"{P}/projects/{project['id']}/access", json={"restricted": True}).status_code == 200
    assert team["ana"].post(f"{P}/projects/{project['id']}/imports", json={"sources": sources[:1]}).status_code == 403
    # An unknown value fails closed, to lead.
    monkeypatch.setenv("CURATION_CREATE_ROLE", "everyone")
    assert team["ana"].post(f"{P}/projects", json={"name": "Nope"}).status_code == 403


def test_deleting_a_project_takes_its_items_and_history(client, db_session):
    from curation.models import CurationEvent, CurationItem

    project = _project(client, "Doomed")
    item = _add(client, project["id"], _item_payload("doomed-1", "ACBR"))
    client.patch(f"{P}/projects/{project['id']}/items/{item['id']}", json={"expected_revision": 1, "included": False})
    assert client.delete(f"{P}/projects/{project['id']}", params={"confirm_name": "Doomed"}).status_code == 200
    db_session.expire_all()
    assert db_session.query(CurationItem).count() == 0
    assert db_session.query(CurationEvent).count() == 0


def test_labels_are_checked_against_the_project_tier(client):
    project = _project(client, "Tier checks")
    item = _add(client, project["id"], _item_payload("tier-1", "ACBR"))
    bad = client.patch(f"{P}/projects/{project['id']}/items/{item['id']}",
                       json={"expected_revision": 1, "current_label": "NOT-A-CODE"})
    assert bad.status_code == 422


def test_image_proxy_only_serves_curated_buckets(client, monkeypatch):
    _fake_storage(monkeypatch, lambda uri: b"\x89PNG fake")
    assert client.get(f"{P}/image", params={"uri": "gs://some-private-bucket/secret.png"}).status_code == 403
    allowed = client.get(f"{P}/image", params={"uri": "gs://nmfs_odp_pifsc/PIFSC/x.png"})
    assert allowed.status_code == 200
    assert allowed.headers["content-type"] == "image/png"


def test_interrupted_imports_are_marked_on_startup(client, db_session):
    from appdb.bootstrap import init_app_db
    from curation.models import CurationImportOperation

    project = _project(client, "Interrupted")
    operation = _operation(db_session, project["id"], HAWAII, status="running")
    init_app_db()
    db_session.expire_all()
    assert db_session.get(CurationImportOperation, operation.id).status == "interrupted"


def test_imports_cannot_read_server_files_unless_allowed(client, monkeypatch):
    project = _project(client, "Local sources")
    local = {"sources": [{"name": "x", "root_uri": "gs://bucket/x/", "csv_uri": "/etc/passwd"}]}
    assert client.post(f"{P}/projects/{project['id']}/imports", json=local).status_code == 422
    monkeypatch.setenv("CURATION_ALLOW_LOCAL_SOURCES", "yes")
    assert client.post(f"{P}/projects/{project['id']}/imports", json=local).status_code == 202


@pytest.mark.parametrize("uri", ["/proc/self/environ", "file:///etc/passwd", "gs://someone-elses-bucket/secret.png"])
def test_curators_cannot_point_items_at_files_or_other_buckets(client, monkeypatch, uri):
    """A curator-created item must not turn crop-preview into a file reader."""
    from curation import storage

    reads = []
    monkeypatch.setattr(storage, "read_bytes", lambda u: reads.append(u) or b"secret")
    project = _project(client, "Guarded " + uri[:12])
    for field in ("crop_uri", "source_image_uri"):
        payload = _item_payload(f"evil-{field}", "ACBR")
        payload[field] = uri
        response = client.post(f"{P}/projects/{project['id']}/items", json=payload)
        assert response.status_code == 422, response.text
    assert client.get(f"{P}/image", params={"uri": uri}).status_code == 403
    assert reads == []


def test_items_already_pointing_elsewhere_are_not_read(client, db_session, monkeypatch):
    """Even an item that got in some other way (direct DB edit, old data) is not read."""
    from curation import storage
    from curation.models import CurationItem

    reads = []

    def read(uri):
        reads.append(uri)
        raise FileNotFoundError(uri)  # nothing in the curated fallbacks either

    monkeypatch.setattr(storage, "read_bytes", read)
    project = _project(client, "Old data")
    item = _add(client, project["id"], _item_payload("old-1", "ACBR"))
    row = db_session.get(CurationItem, item["id"])
    row.crop_uri = "/proc/self/environ"
    row.source_image_uri = "gs://someone-elses-bucket/a.jpg"
    db_session.commit()
    assert client.get(f"{P}/projects/{project['id']}/items/{item['id']}/crop-preview").status_code == 422
    assert all(u.startswith("gs://nmfs_odp_pifsc/") for u in reads)  # only the curated fallbacks were tried


def test_storage_refuses_local_paths_unless_allowed(tmp_path, monkeypatch):
    from curation import storage

    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    monkeypatch.delenv("CURATION_ALLOW_LOCAL_SOURCES", raising=False)
    with pytest.raises(PermissionError):
        storage.read_bytes(str(secret))
    monkeypatch.setenv("CURATION_ALLOW_LOCAL_SOURCES", "yes")
    assert storage.read_bytes(str(secret)) == b"x"


# ── One coordinator per database (OCEANFRAME_BACKGROUND_WORKERS) ──────────────

def _running_import_and_building_version(client):
    from appdb import session_scope
    from curation.models import CurationDatasetVersion, CurationImportOperation

    project = _project(client, "Shared database")
    with session_scope() as db:
        operation = CurationImportOperation(project_id=project["id"], sources=[], batch_size=10, status="running")
        version = CurationDatasetVersion(project_id=project["id"], version=1, status="building")
        db.add_all([operation, version])
        db.flush()
        return operation.id, version.id


def _statuses(operation_id, version_id):
    from appdb import session_scope
    from curation.models import CurationDatasetVersion, CurationImportOperation

    with session_scope() as db:
        return (db.get(CurationImportOperation, operation_id).status,
                db.get(CurationDatasetVersion, version_id).status)


def test_with_background_workers_off_startup_recovery_leaves_the_queues_alone(client, monkeypatch):
    from appdb.bootstrap import init_app_db

    ids = _running_import_and_building_version(client)
    monkeypatch.setenv("OCEANFRAME_BACKGROUND_WORKERS", "off")
    init_app_db()
    assert _statuses(*ids) == ("running", "building")
    monkeypatch.setenv("OCEANFRAME_BACKGROUND_WORKERS", "on")
    init_app_db()
    assert _statuses(*ids) == ("interrupted", "interrupted")


def test_with_background_workers_off_the_seed_is_skipped(library, monkeypatch):
    from appdb.bootstrap import init_app_db
    from curation import importer

    monkeypatch.setenv("CURATION_SEED", "noaa-pacific")
    monkeypatch.setenv("OCEANFRAME_BACKGROUND_WORKERS", "off")
    monkeypatch.setattr(importer, "seed_noaa_pacific", lambda db: pytest.fail("the seed must not run"))
    init_app_db()


@pytest.mark.parametrize("flag,started", [("off", False), ("on", True)])
def test_the_lifespan_starts_the_pools_only_with_background_workers_on(library, monkeypatch, flag, started):
    from fastapi.testclient import TestClient

    import main

    calls = []
    for name in ("curation_import_pool", "evaluation_pool", "registry_worker"):
        pool = getattr(main, name)
        monkeypatch.setattr(pool, "start", lambda name=name: calls.append(f"{name}.start"))
        monkeypatch.setattr(pool, "stop", lambda *args, name=name: calls.append(f"{name}.stop"))
    monkeypatch.setenv("OCEANFRAME_BACKGROUND_WORKERS", flag)
    with TestClient(main.app) as client:
        assert client.get("/healthz").status_code == 200
    expected = ["curation_import_pool.start", "evaluation_pool.start", "registry_worker.start",
                "registry_worker.stop", "evaluation_pool.stop", "curation_import_pool.stop"]
    assert calls == (expected if started else [])


@pytest.mark.parametrize("backend,auth,warned", [("oracle", None, True), ("oracle", "on", False),
                                                 ("sqlite", None, False)])
def test_sign_in_off_on_a_shared_database_is_warned_about(library, monkeypatch, caplog, backend, auth, warned):
    import logging

    from fastapi.testclient import TestClient

    import main

    for name in ("curation_import_pool", "evaluation_pool", "registry_worker"):
        monkeypatch.setattr(getattr(main, name), "start", lambda: None)
        monkeypatch.setattr(getattr(main, name), "stop", lambda *args: None)
    monkeypatch.setattr(main, "backend_name", lambda: backend)
    if auth:
        monkeypatch.setenv("OCEANFRAME_AUTH", auth)
    with caplog.at_level(logging.WARNING, logger="oceanframe"):
        with TestClient(main.app):
            pass
    found = any("Sign-in is off: everyone who can reach this app acts as an admin on the shared database"
                in record.getMessage() for record in caplog.records)
    assert found is warned


def test_hold_keeps_inclusion_and_include_exclude_set_it(client):
    pid = _project(client, "Hold")["id"]
    item = _add(client, pid, _item_payload("hold-1", "TURFH"))
    url = f"{P}/projects/{pid}/items/{item['id']}/quality"
    held = client.post(url, json={"decision": "manual_review", "score": 50}).json()
    assert (held["included"], held["quality_decision"], held["review_state"]) == (True, "manual_review", "flagged")
    assert client.post(url, json={"decision": "reject"}).json()["included"] is False
    # Hold parks an item without changing it: an excluded one stays excluded.
    assert client.post(url, json={"decision": "manual_review"}).json()["included"] is False
    included = client.post(url, json={"decision": "accept"}).json()
    assert (included["included"], included["review_state"]) == (True, "reviewed")


def test_bulk_quality_applies_under_one_operation_and_reports_conflicts(client, db_session):
    from curation.models import CurationEvent, CurationItem

    pid = _project(client, "Bulk quality")["id"]
    a = _add(client, pid, _item_payload("bq-1", "TURFH"))
    b = _add(client, pid, _item_payload("bq-2", "ACBR"))
    c = _add(client, pid, _item_payload("bq-3", "SAND"))
    response = client.post(f"{P}/projects/{pid}/quality/bulk", json={"scope": "patch", "decisions": [
        {"item_id": a["id"], "decision": "accept", "score": 90, "expected_revision": a["revision"]},
        {"item_id": b["id"], "decision": "reject", "score": 20, "flags": ["blurry"], "expected_revision": b["revision"]},
        {"item_id": c["id"], "decision": "accept", "expected_revision": c["revision"] + 5},
    ]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] == 2
    assert [conflict["id"] for conflict in body["conflicts"]] == [c["id"]]
    db_session.expire_all()
    assert db_session.get(CurationItem, a["id"]).included is True
    assert db_session.get(CurationItem, b["id"]).included is False
    assert db_session.get(CurationItem, b["id"]).quality_flags == ["blurry"]
    assert db_session.get(CurationItem, c["id"]).quality_decision is None
    operations = {event.operation_id for event in db_session.query(CurationEvent)
                  .filter(CurationEvent.event_type == "quality_reviewed")}
    assert operations == {body["operation_id"]}


def test_bulk_quality_source_scope_covers_every_point_even_with_two_anchors_on_one_image(client, db_session):
    from curation.models import CurationItem

    pid = _project(client, "Bulk source quality")["id"]
    first = _item_payload("bs-1", "TURFH")
    a = _add(client, pid, first)
    second = {**_item_payload("bs-2", "ACBR"), "source_image_uri": first["source_image_uri"]}
    b = _add(client, pid, second)
    response = client.post(f"{P}/projects/{pid}/quality/bulk", json={"scope": "source", "decisions": [
        {"item_id": a["id"], "decision": "accept"}, {"item_id": b["id"], "decision": "reject"},
    ]})
    assert response.status_code == 200, response.text
    db_session.expire_all()
    # Both anchors are on one image: the later decision wins for every point on it.
    assert {db_session.get(CurationItem, i).included for i in (a["id"], b["id"])} == {False}


def test_bulk_quality_validates_its_input(client):
    pid = _project(client, "Bulk quality input")["id"]
    a = _add(client, pid, _item_payload("bv-1", "TURFH"))
    url = f"{P}/projects/{pid}/quality/bulk"
    assert client.post(url, json={"decisions": []}).status_code == 422
    twice = [{"item_id": a["id"], "decision": "accept"}] * 2
    assert client.post(url, json={"decisions": twice}).status_code == 422
    assert client.post(url, json={"decisions": [{"item_id": 999999, "decision": "accept"}]}).status_code == 404


def test_bulk_quality_needs_a_curator(team):
    pid = _project(team["admin"], "Bulk quality roles")["id"]
    _join(team, pid, "ana")
    item = _add(team["admin"], pid, _item_payload("br-1", "TURFH"))
    body = {"decisions": [{"item_id": item["id"], "decision": "accept"}]}
    assert team["vic"].post(f"{P}/projects/{pid}/quality/bulk", json=body).status_code == 403
    assert team["ana"].post(f"{P}/projects/{pid}/quality/bulk", json=body).status_code == 200


def test_bulk_quality_source_scope_sends_creation_revisions_without_false_conflicts(client, db_session):
    from curation.models import CurationItem

    pid = _project(client, "Bulk source revisions")["id"]
    first = _item_payload("bsr-1", "TURFH")
    a = _add(client, pid, first)
    b = _add(client, pid, {**_item_payload("bsr-2", "ACBR"), "source_image_uri": first["source_image_uri"]})
    response = client.post(f"{P}/projects/{pid}/quality/bulk", json={"scope": "source", "decisions": [
        {"item_id": a["id"], "decision": "accept", "expected_revision": a["revision"]},
        {"item_id": b["id"], "decision": "reject", "expected_revision": b["revision"]},
    ]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["applied"], body["conflicts"]) == (2, [])
    db_session.expire_all()
    # The later decision (reject on b) wins for every point on the shared image.
    for item_id in (a["id"], b["id"]):
        item = db_session.get(CurationItem, item_id)
        assert (item.included, item.quality_decision) == (False, "reject")


def test_quality_review_speaks_in_dataset_terms(client):
    page = client.get("/curation").text
    assert ">In dataset</option>" in page and ">Held</option>" in page and ">Excluded</option>" in page
    script = client.get("/static/curation/curation.js").text
    assert "/quality/bulk" in script
    for word in ("'Include'", "'Hold'", "'Exclude'"):
        assert word in script


def test_versions_view_has_a_compare_bar(client):
    page = client.get("/curation").text
    for element in ('id="compare-base"', 'id="compare-target"', 'id="compare-run"', 'id="compare-result"'):
        assert element in page
    assert "function renderCompare(" in client.get("/static/curation/curation.js").text


def test_curation_remembers_the_view_in_the_url(client):
    script = client.get("/static/curation/curation.js").text
    for needle in ("function readHash()", "function writeHash()", "history.replaceState", "'hashchange'"):
        assert needle in script


def test_curation_page_gates_project_work_by_project_role(client):
    page = client.get("/curation").text
    assert "/static/curation/collab.js" in page
    for element in ('id="members-button"', 'id="releases-button"', 'id="membership-notice"', 'id="members-dialog"'):
        assert element in page
    assert 'data-min-role="project-curator"' in page and 'data-min-role="project-lead"' in page
    assert 'id="publish-button" class="btn btn-primary" type="submit" data-min-role="project-lead"' in page
    script = client.get("/static/curation/curation.js").text
    assert "'project-curator'" in script and "'project-lead'" in script


def test_curation_page_has_commit_and_history(client):
    page = client.get("/curation").text
    for element in ('id="commit-pill"', 'id="commit-dialog"', 'id="commit-message"', 'id="commit-summary"',
                    'id="commit-confirm"', 'id="history-list"', 'id="history-person"', 'id="history-since"',
                    'id="history-uncommitted"'):
        assert element in page
    script = client.get("/static/curation/collab.js").text
    assert "function refreshCommitPill(" in script and "function loadHistory(" in script and "/change-sets" in script


def test_curation_page_has_people_compare_and_releases(client):
    page = client.get("/curation").text
    for element in ('id="people-a"', 'id="people-b"', 'id="people-since"', 'id="people-run"', 'id="people-result"',
                    'id="releases-dialog"', 'id="releases-list"', 'id="release-detail"', 'id="release-form"',
                    'id="release-name"', 'id="release-mode"', 'id="release-sources"', 'id="release-notes"',
                    'id="release-build"', 'id="pc-a"', 'id="pc-a-version"', 'id="pc-b"', 'id="pc-b-version"',
                    'id="pc-run"', 'id="pc-result"'):
        assert element in page
    script = client.get("/static/curation/collab.js").text
    for needle in ("/people/compare", "/compare-projects", "/api/curation/releases", "renderBreakdownTable("):
        assert needle in script


def test_navigation_back_links_and_deep_links(client):
    page = client.get("/curation").text
    for element in ('id="back-bar"', 'id="back-button"', 'id="source-missing-notice"'):
        assert element in page
    script = client.get("/static/curation/curation.js").text
    for needle in ("function openSource(", "state.backTo", "params.set('image'", "data-open-source", "crop-preview?r="):
        assert needle in script


def test_the_source_editor_is_on_the_page(client):
    page = client.get("/curation").text
    assert "/static/curation/editor.js" in page
    for element in ('id="pending-tray"', 'id="pending-list"', 'id="pending-save"', 'id="pending-discard"',
                    'id="point-remove"', 'id="point-restore"', 'id="point-edit-size"'):
        assert element in page
    script = client.get("/static/curation/editor.js").text
    for needle in ("function pendingCount(", "function savePending(", "/edits", "function confirmDiscardPending("):
        assert needle in script


def test_bulk_add_and_shortcuts_are_wired(client):
    page = client.get("/curation").text
    for element in ('id="bulk-add-button"', 'id="bulk-add-form"', 'id="bulk-grid"', 'id="bulk-spacing"', 'id="bulk-model"', 'id="bulk-run"'):
        assert element in page
        assert page.count(element) == 1
    script = client.get("/static/curation/editor.js").text
    assert "/suggest" in script and "function addCandidates(" in script and "'keydown'" in script
    assert "function stepImage(" in script and "function bindMarkerDrag(" in script and "validRelabelCode" in script
    assert "classify-model" not in script


def test_missing_images_and_removed_filters_are_on_the_page(client):
    page = client.get("/curation").text
    for element in ('id="filter-hide-missing"', 'id="filter-show-removed"', 'id="missing-card"', 'id="missing-summary"',
                    'id="missing-check"', 'id="missing-download"'):
        assert element in page
    script = client.get("/static/curation/curation.js").text
    assert "source_missing" in script and "/availability" in script
