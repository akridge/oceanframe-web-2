"""Which source images are missing from their bucket."""
from __future__ import annotations

import io
import threading
from contextlib import contextmanager

from test_curation import CSV_HEADER, P, _add, _item_payload, _project, _test_buckets, client, db_session  # noqa: F401


def _seed(client):
    pid = _project(client, "Availability")["id"]
    for key, image, region, year in (("a1", "present-1", "Hawaii", 2023), ("a2", "present-1", "Hawaii", 2023),
                                     ("a3", "gone-1", "Hawaii", 2024), ("a4", "gone-2", "Mariana", 2024)):
        _add(client, pid, {**_item_payload(key, "ACBR", region=region), "survey_year": year,
                           "source_image_uri": f"gs://nmfs/images/{image}.jpg"})
    return pid


def test_missing_images_are_flagged_counted_and_filterable(client, monkeypatch):
    from appdb import session_scope
    from curation import availability, storage

    monkeypatch.setattr(storage, "list_names", lambda prefix: {"present-1.jpg"})
    pid = _seed(client)
    with session_scope() as db:
        result = availability.check(db, pid)
    assert (result["checked"], result["missing"]) == (3, 2)
    summary = client.get(f"{P}/projects/{pid}/availability").json()
    assert summary["missing_images"] == 2 and summary["missing_items"] == 2
    assert {(r["region"], r["year"], r["images"]) for r in summary["by_region_year"]} == {("Hawaii", 2024, 1), ("Mariana", 2024, 1)}
    assert client.get(f"{P}/projects/{pid}/items?source_missing=hide").json()["returned"] == 2
    assert client.get(f"{P}/projects/{pid}/items?source_missing=only").json()["returned"] == 2
    csv_text = client.get(f"{P}/projects/{pid}/availability.csv").text
    assert "gone-1.jpg" in csv_text and "present-1.jpg" not in csv_text


def test_a_second_check_clears_images_that_came_back(client, monkeypatch):
    from appdb import session_scope
    from curation import availability, storage

    pid = _seed(client)
    monkeypatch.setattr(storage, "list_names", lambda prefix: {"present-1.jpg"})
    with session_scope() as db:
        availability.check(db, pid)
    monkeypatch.setattr(storage, "list_names", lambda prefix: {"present-1.jpg", "gone-1.jpg", "gone-2.jpg"})
    with session_scope() as db:
        assert availability.check(db, pid)["missing"] == 0
    assert client.get(f"{P}/projects/{pid}/items?source_missing=only").json()["returned"] == 0


def test_the_check_runs_as_a_background_operation(client, monkeypatch):
    from curation import importer, storage

    monkeypatch.setattr(storage, "list_names", lambda prefix: set())
    pid = _seed(client)
    started = client.post(f"{P}/projects/{pid}/availability")
    assert started.status_code == 202
    importer.run_import(started.json()["id"], threading.Event())  # the worker thread does not run in tests
    op = client.get(f"{P}/projects/{pid}/imports/{started.json()['id']}").json()
    assert op["status"] == "completed", op
    assert (op["processed_rows"], op["rejected_rows"], op["imported_rows"]) == (3, 3, 0)
    assert client.get(f"{P}/projects/{pid}/availability").json()["missing_images"] == 3
    assert client.get(f"{P}/projects/{pid}").json()["item_count"] == 4


def test_missing_images_sort_last_in_the_source_queue(client, monkeypatch):
    from appdb import session_scope
    from curation import availability, storage

    monkeypatch.setattr(storage, "list_names", lambda prefix: {"present-1.jpg"})
    pid = _seed(client)
    with session_scope() as db:
        availability.check(db, pid)
    images = client.get(f"{P}/projects/{pid}/source-images?source_missing=any").json()["images"]
    assert images[-1]["source_image_uri"].endswith(("gone-1.jpg", "gone-2.jpg"))
    assert images[0]["source_image_uri"].endswith("present-1.jpg")


def test_paging_across_the_present_missing_boundary_skips_and_repeats_nothing(client, monkeypatch):
    from appdb import session_scope
    from curation import availability, storage

    pid = _project(client, "Paging")["id"]
    for n in range(1, 6):
        _add(client, pid, {**_item_payload(f"p{n}", "ACBR"), "source_image_uri": f"gs://nmfs/images/img-{n}.jpg"})
    monkeypatch.setattr(storage, "list_names", lambda prefix: {"img-1.jpg", "img-3.jpg", "img-5.jpg"})
    with session_scope() as db:
        availability.check(db, pid)
    seen, params = [], "limit=2&source_missing=any"
    for _ in range(10):
        page = client.get(f"{P}/projects/{pid}/source-images?{params}").json()
        seen += [image["source_image_uri"].rsplit("/", 1)[-1] for image in page["images"]]
        if page["next_cursor"] is None:
            break
        params = f"limit=2&source_missing=any&after_id={page['next_cursor']}&after_missing={page['next_missing']}"
    assert seen == ["img-1.jpg", "img-3.jpg", "img-5.jpg", "img-2.jpg", "img-4.jpg"]


def test_a_finished_import_queues_one_check_and_a_check_queues_none(client, db_session, monkeypatch):
    from curation import importer, storage
    from curation.models import CurationImportOperation

    body = CSV_HEADER + (
        "BAK-02_2018_A_01.JPG,2018-06-15,PRIAs,BAK,BAK-02,3,USER1,100,0.1,-176.4,"
        ",,,,,,,,1378,727,*ACBR,1269,Analyst,2019-05-21,,,,,,,,,,\n"
    )
    source = {"name": "Samoa/PRIA", "root_uri": "gs://bucket/ESD_REA_SAMOA_PRIA_v2/",
              "csv_uri": "gs://bucket/ESD_REA_SAMOA_PRIA_v2/annotations.csv"}

    @contextmanager
    def open_text(uri):
        yield io.StringIO(body)

    monkeypatch.setattr(storage, "open_text", open_text)
    monkeypatch.setattr(storage, "list_names", lambda prefix: set())
    pid = _project(client, "Hook")["id"]
    operation = CurationImportOperation(project_id=pid, status="starting", sources=[source], batch_size=100)
    db_session.add(operation)
    db_session.commit()
    importer.run_import(operation.id, threading.Event())

    db_session.expire_all()
    queued = db_session.query(CurationImportOperation).filter_by(project_id=pid, status="queued").all()
    assert [o.sources[0]["kind"] for o in queued] == ["availability_check"]
    importer.run_import(queued[0].id, threading.Event())
    db_session.expire_all()
    assert db_session.get(CurationImportOperation, queued[0].id).status == "completed"
    assert db_session.query(CurationImportOperation).filter_by(project_id=pid, status="queued").count() == 0


def test_a_second_check_while_one_is_pending_returns_the_same_operation(client):
    pid = _seed(client)
    first = client.post(f"{P}/projects/{pid}/availability")
    second = client.post(f"{P}/projects/{pid}/availability")
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]


def test_a_check_is_never_the_latest_import(client, db_session):
    from curation.models import CurationImportOperation

    fresh = _seed(client)
    assert client.post(f"{P}/projects/{fresh}/availability").status_code == 202
    assert client.get(f"{P}/projects/{fresh}/imports").json() == []  # a check alone: no import at all

    pid = _project(client, "Imported, then checked")["id"]
    real = CurationImportOperation(project_id=pid, status="completed", batch_size=100, sources=[
        {"kind": "regional_csv", "name": "Samoa/PRIA", "root_uri": "gs://bucket/r/", "csv_uri": "gs://bucket/r/a.csv"}])
    db_session.add(real)
    db_session.commit()
    check = client.post(f"{P}/projects/{pid}/availability").json()
    listed = client.get(f"{P}/projects/{pid}/imports").json()
    assert [operation["id"] for operation in listed] == [real.id]
    assert check["id"] not in {operation["id"] for operation in listed}
    # The check itself is still readable by id, which is how Dataset status follows it.
    assert client.get(f"{P}/projects/{pid}/imports/{check['id']}").json()["id"] == check["id"]
