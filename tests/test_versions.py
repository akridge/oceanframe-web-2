"""Dataset versions: splits, snapshots, the in/out change log, contributors, files, seeding."""
from __future__ import annotations

import csv
import io
import time

import pytest

from test_curation import P, _add, _item_payload, _join, _project, _test_buckets, client, db_session, team  # noqa: F401


def _wait(client, project_id, number, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        version = client.get(f"{P}/projects/{project_id}/versions/{number}").json()
        if version["status"] != "building":
            return version
        time.sleep(0.1)
    raise AssertionError("version did not finish building")


def _publish(client, project_id, notes=None):
    response = client.post(f"{P}/projects/{project_id}/versions", json={"notes": notes})
    assert response.status_code == 202, response.text
    version = _wait(client, project_id, response.json()["version"])
    assert version["status"] == "published", version
    return version


def _on_image(key, label, image):
    payload = _item_payload(key, label)
    payload["source_image_uri"] = f"gs://nmfs/images/{image}.jpg"
    return payload


def test_splits_are_deterministic_and_grouped_by_image():
    from curation.splits import split_for

    ratios = (0.8, 0.1, 0.1)
    assert split_for("gs://b/images/a.jpg", 42, ratios) == split_for("gs://b/images/a.jpg", 42, ratios)
    counts = {"train": 0, "val": 0, "test": 0}
    for i in range(2000):
        counts[split_for(f"gs://b/images/{i}.jpg", 42, ratios)] += 1
    assert 1450 < counts["train"] < 1750 and counts["val"] > 120 and counts["test"] > 120


def test_statistics_index_migrates_existing_tables_idempotently(library):
    from sqlalchemy import inspect
    from appdb.bootstrap import _ledger, init_app_db
    from appdb.engine import get_engine
    from curation.models import CurationItem

    engine = get_engine()
    index = next(index for index in CurationItem.__table__.indexes if index.name == "ix_cur_items_included_label")
    with engine.begin() as connection:
        index.drop(connection)
        connection.execute(_ledger.delete().where(_ledger.c.migration_id == "20261002_01"))
    assert "20261002_01" in init_app_db()["migrations_applied"]
    assert init_app_db()["migrations_applied"] == []
    found = {entry["name"]: entry for entry in inspect(engine).get_indexes("curation_items")}
    assert found[index.name]["column_names"] == ["project_id", "included", "current_label"]


@pytest.mark.parametrize("value,expected", [("test", "test"), ("TRAIN", "train"), (" validation ", "val")])
def test_csv_preserves_original_split(value, expected):
    from curation.importer import iter_source_records

    row = {"Name": "photo.jpg", "Label code": "ACBR", "Row": "10", "Column": "20", "Split": value}
    record = next(iter_source_records([row], project_id="p", source_name="source", root_uri="gs://b/"))
    assert record["split"] == expected
    assert record["metadata_json"]["split_origin"] == "imported"


def test_csv_missing_split_is_generated_and_invalid_split_rejected():
    from curation.importer import iter_source_records
    from curation.splits import split_for

    row = {"Name": "photo.jpg", "Label code": "ACBR", "Row": "10", "Column": "20"}
    record = next(iter_source_records([row], project_id="p", source_name="source", root_uri="gs://b/"))
    assert record["split"] == split_for(record["source_image_uri"], 42, (0.8, 0.1, 0.1))
    assert record["metadata_json"]["split_origin"] == "generated"
    row["Split"] = "holdout-ish"
    with pytest.raises(ValueError, match="Split must"):
        next(iter_source_records([row], project_id="p", source_name="source", root_uri="gs://b/"))


def test_two_versions_record_what_came_in_went_out_and_was_relabelled(client):
    project = _project(client, "Versioned")
    pid = project["id"]
    a1 = _add(client, pid, _on_image("a1", "ACBR", "img-1"))
    a2 = _add(client, pid, _on_image("a2", "ACBR", "img-1"))
    _add(client, pid, _on_image("p1", "POCS", "img-2"))
    s1 = _add(client, pid, _on_image("s1", "SAND", "img-3"))

    v1 = _publish(client, pid, "first cut")
    assert v1["version"] == 1 and v1["item_count"] == 4
    assert v1["changes"]["in"] == 4 and v1["changes"]["previous_version"] is None
    assert sum(v1["statistics"]["splits"].values()) == 4

    # Change things: one out, one relabelled, one new.
    client.patch(f"{P}/projects/{pid}/items/{s1['id']}", json={"expected_revision": s1["revision"], "included": False})
    client.patch(f"{P}/projects/{pid}/items/{a2['id']}", json={"expected_revision": a2["revision"], "current_label": "POCS"})
    t1 = _add(client, pid, _on_image("t1", "TURFH", "img-4"))

    preview = client.get(f"{P}/projects/{pid}/versions/preview").json()
    assert preview["base_version"] == 1
    changes = preview["changes"]
    assert (changes["before"], changes["in"], changes["out"], changes["relabelled"], changes["after"]) == (4, 1, 1, 1, 4)
    per_class = {row["label"]: row for row in changes["per_class"]}
    assert per_class["SAND"]["out"] == 1 and per_class["SAND"]["net"] == -1
    assert per_class["TURFH"]["in"] == 1
    assert per_class["ACBR"]["relabelled_out"] == 1 and per_class["POCS"]["relabelled_in"] == 1
    assert changes["relabels"] == [{"from": "ACBR", "to": "POCS", "count": 1}]

    v2 = _publish(client, pid, "second cut")
    # What was previewed is what was published.
    published = {k: v2["changes"][k] for k in ("before", "in", "out", "relabelled", "after", "per_class", "relabels")}
    assert published == {k: changes[k] for k in published}
    assert v2["changes"]["previous_version"] == 1

    # Splits are sticky and shared by every point on an image.
    from appdb import session_scope
    from curation.models import CurationDatasetVersion, CurationVersionItem
    with session_scope() as s:
        def splits(number):
            vid = s.query(CurationDatasetVersion.id).filter_by(project_id=pid, version=number).scalar()
            return {r.item_id: r.split for r in s.query(CurationVersionItem).filter_by(version_id=vid)}
        first, second = splits(1), splits(2)
    assert first[a1["id"]] == second[a1["id"]] == second[a2["id"]]  # same image, same split, unchanged
    assert t1["id"] in second and s1["id"] not in second


def test_a_split_move_shows_in_the_preview_the_stored_changes_and_the_changelog(client):
    from appdb import session_scope
    from curation.models import CurationItem

    pid = _project(client, "Split move")["id"]
    first = _add(client, pid, _on_image("m1", "ACBR", "img-m1"))
    _add(client, pid, _on_image("m2", "POCS", "img-m2"))
    _publish(client, pid, "first cut")
    with session_scope() as db:
        item = db.get(CurationItem, first["id"])
        item.split = "val" if item.split != "val" else "test"
    preview = client.get(f"{P}/projects/{pid}/versions/preview").json()["changes"]
    assert preview["moved"] == 1 and len(preview["split_moves"]) == 1
    assert {row["label"]: row["moved"] for row in preview["per_class"]} == {"ACBR": 1}

    v2 = _publish(client, pid, "moved one")
    assert v2["changes"]["moved"] == 1
    changelog = client.get(f"{P}/projects/{pid}/versions/2/files/CHANGELOG.md").text
    assert "| changed split | 1 |" in changelog and "## Split moves" in changelog
    assert "moved split" in changelog and "| ACBR | 1 | 0 | 0 | 0 | 0 | 1 | 1 | +0 |" in changelog


def test_the_changelog_of_an_old_version_without_move_keys_still_renders(client):
    from appdb import session_scope
    from curation import versions
    from curation.models import CurationDatasetVersion, CurationProject

    pid = _project(client, "Old changes")["id"]
    _add(client, pid, _on_image("o1", "ACBR", "img-o1"))
    _publish(client, pid)
    with session_scope() as db:
        version = db.query(CurationDatasetVersion).filter_by(project_id=pid, version=1).one()
        version.changes = {key: value for key, value in version.changes.items() if key not in ("moved", "split_moves")}
        version.changes["per_class"] = [{k: v for k, v in row.items() if k not in ("moved", "moved_to")}
                                        for row in version.changes["per_class"]]
        text = versions.changelog_markdown(db.get(CurationProject, pid), version)
    assert "changed split | 0" in text and "## Split moves" not in text


def test_version_files_and_changelog(client):
    pid = _project(client, "Files")["id"]
    for i, label in enumerate(["ACBR", "POCS", "SAND"]):
        _add(client, pid, _on_image(f"f{i}", label, f"img-{i}"))
    v1 = _publish(client, pid, "Initial release")
    assert v1["files"] == ["manifest.csv", "manifest.json", "CHANGELOG.md"]

    manifest = client.get(f"{P}/projects/{pid}/versions/1/files/manifest.csv")
    assert manifest.status_code == 200
    rows = list(csv.DictReader(io.StringIO(manifest.text)))
    assert {r["label"] for r in rows} == {"ACBR", "POCS", "SAND"} and all(r["split"] in {"train", "val", "test"} for r in rows)
    changelog = client.get(f"{P}/projects/{pid}/versions/1/files/CHANGELOG.md").text
    assert "# Files — v1" in changelog and "Initial release" in changelog and "came in | +3" in changelog
    assert client.get(f"{P}/projects/{pid}/versions/1/files/../../secret").status_code != 200
    assert client.get(f"{P}/projects/{pid}/versions/1/files/data.yaml").status_code == 404  # only the listed files


def test_version_freezes_input_references_and_flags_split_leakage(client):
    from appdb import session_scope
    from curation.models import CurationItem, CurationVersionInput

    pid = _project(client, "Frozen inputs")["id"]
    first = _add(client, pid, _on_image("frozen", "ACBR", "shared"))
    second = _add(client, pid, _on_image("other", "POCS", "shared"))
    with session_scope() as db:
        db.get(CurationItem, first["id"]).split = "test"
        db.get(CurationItem, second["id"]).split = "train"
    version = _publish(client, pid)
    assert version["statistics"]["cross_split_source_images"] == 1
    before = client.get(f"{P}/projects/{pid}/versions/1/files/manifest.csv").text
    with session_scope() as db:
        item = db.get(CurationItem, first["id"])
        item.crop_uri = "gs://nmfs/changed.png"
        item.current_label = "SAND"
        frozen = db.query(CurationVersionInput).filter_by(item_id=first["id"]).one()
        assert frozen.crop_uri == first["crop_uri"] and frozen.label == "ACBR" and frozen.split == "test"
    assert client.get(f"{P}/projects/{pid}/versions/1/files/manifest.csv").text == before


def test_contributors_show_who_worked_on_which_classes(team):
    pid = _project(team["admin"], "Team effort")["id"]
    acbr = [_add(team["admin"], pid, _on_image(f"ac{i}", "ACBR", f"i{i}")) for i in range(3)]
    pocs = [_add(team["admin"], pid, _on_image(f"po{i}", "POCS", f"j{i}")) for i in range(2)]
    v1 = _publish(team["admin"], pid)
    _join(team, pid, "ana")
    _join(team, pid, "ben")

    for item in acbr:   # Ana takes the branching Acropora
        team["ana"].patch(f"{P}/projects/{pid}/items/{item['id']}", json={"expected_revision": item["revision"], "review_state": "reviewed"})
    for item in pocs:   # Ben takes Pocillopora, and excludes one
        team["ben"].patch(f"{P}/projects/{pid}/items/{item['id']}", json={"expected_revision": item["revision"], "included": False})

    preview = team["vic"].get(f"{P}/projects/{pid}/versions/preview").json()
    people = {c["name"]: c for c in preview["contributors"]}
    assert people["Ana"]["items"] == 3 and people["Ana"]["classes"][0]["label"] == "ACBR"
    assert people["Ben"]["items"] == 2 and people["Ben"]["classes"][0]["label"] == "POCS"
    assert preview["changes"]["out"] == 2

    v2 = _publish(team["admin"], pid)
    assert {c["name"] for c in v2["contributors"]} == {"Ana", "Ben"}
    assert v1["contributors"] == []  # nobody had changed anything before v1


def test_only_leads_publish(team):
    pid = _project(team["admin"], "Gated")["id"]
    assert team["ana"].post(f"{P}/projects/{pid}/versions", json={}).status_code == 403
    assert team["vic"].post(f"{P}/projects/{pid}/versions", json={}).status_code == 403
    assert team["vic"].get(f"{P}/projects/{pid}/versions").status_code == 200


def test_image_folders_for_a_version(client, monkeypatch):
    from pathlib import Path

    from curation import storage

    monkeypatch.setattr(storage, "read_bytes", lambda uri: b"png-bytes")
    pid = _project(client, "Images")["id"]
    _add(client, pid, _on_image("i1", "ACBR", "img-a"))
    _add(client, pid, _on_image("i2", "POCS", "img-b"))
    _publish(client, pid)
    assert client.post(f"{P}/projects/{pid}/versions/1/images").status_code == 202
    deadline = time.time() + 20
    while time.time() < deadline:
        state = client.get(f"{P}/projects/{pid}/versions/1").json()["images"]
        if state["status"] != "running":
            break
        time.sleep(0.1)
    assert state["status"] == "completed" and state["done"] == 2 and state["failed"] == 0
    files = sorted(p.relative_to(state["location"]).as_posix() for p in Path(state["location"]).rglob("*.jpg"))
    assert len(files) == 2 and all(f.split("/")[0] in {"train", "val", "test"} for f in files)
    assert {f.split("/")[1] for f in files} == {"ACBR", "POCS"}


def test_image_folders_cut_a_moved_item_fresh(client, monkeypatch):
    import io
    from pathlib import Path

    from PIL import Image

    from curation import image_cache, storage

    buffer = io.BytesIO()
    Image.new("RGB", (1000, 800), (30, 140, 90)).save(buffer, format="JPEG")
    source = buffer.getvalue()
    monkeypatch.setattr(storage, "read_bytes", lambda uri: source if uri.endswith("img-a.jpg") else b"stale-png")
    image_cache.clear()
    pid = _project(client, "Fresh export")["id"]
    payload = _on_image("m1", "ACBR", "img-a")
    payload.update(row_px=120, column_px=240)
    item = _add(client, pid, payload)
    moved = client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                         json={"expected_revision": item["revision"], "row_px": 300, "column_px": 300})
    assert moved.status_code == 200, moved.text
    _publish(client, pid)
    assert client.post(f"{P}/projects/{pid}/versions/1/images").status_code == 202
    deadline = time.time() + 20
    while time.time() < deadline:
        state = client.get(f"{P}/projects/{pid}/versions/1").json()["images"]
        if state["status"] != "running":
            break
        time.sleep(0.1)
    assert state["status"] == "completed" and state["done"] == 1 and state["failed"] == 0
    written = next(Path(state["location"]).rglob("*.jpg")).read_bytes()
    assert written.startswith(b"\x89PNG") and written != b"stale-png"


def test_image_folders_never_export_the_old_crop_of_a_moved_item(client, monkeypatch):
    import io
    from pathlib import Path

    from PIL import Image

    from curation import image_cache, storage

    buffer = io.BytesIO()
    Image.new("RGB", (1000, 800), (30, 140, 90)).save(buffer, format="JPEG")
    source = buffer.getvalue()
    monkeypatch.setattr(storage, "read_bytes", lambda uri: source if "/images/" in uri else b"stale-png")
    image_cache.clear()
    pid = _project(client, "Fresh export refused")["id"]
    payload = _on_image("m2", "ACBR", "img-a")
    payload.update(row_px=120, column_px=240, crop_uri="gs://bucket/crops/m2.png")  # crop stays readable, in an allowed bucket
    item = _add(client, pid, payload)
    client.patch(f"{P}/projects/{pid}/items/{item['id']}",
                 json={"expected_revision": item["revision"], "row_px": 300, "column_px": 300})
    _publish(client, pid)
    monkeypatch.setenv("CURATION_IMAGE_BUCKETS", "bucket")  # only the source's bucket (nmfs) is de-listed
    assert client.post(f"{P}/projects/{pid}/versions/1/images").status_code == 202
    deadline = time.time() + 20
    while time.time() < deadline:
        state = client.get(f"{P}/projects/{pid}/versions/1").json()["images"]
        if state["status"] != "running":
            break
        time.sleep(0.1)
    assert state["done"] == 0 and state["failed"] == 1
    assert not [p for p in Path(state["location"]).rglob("*") if p.is_file()]  # rglob of a missing dir is empty


def test_noaa_seed_runs_once_and_stays_deleted(library, monkeypatch):
    from appdb import session_scope
    from curation import starter_data
    from curation.models import CurationImportOperation, CurationProject

    monkeypatch.delenv("CURATION_STARTER_DATA")  # conftest turns it off; the old setting is the alias
    monkeypatch.setenv("CURATION_SEED", "noaa-pacific")
    with session_scope() as s:
        starter_data.ensure_once(s)
    with session_scope() as s:
        project = s.query(CurationProject).filter_by(name="NOAA Pacific T3").one()
        operation = s.query(CurationImportOperation).filter_by(project_id=project.id).one()
        assert operation.status == "queued" and len(operation.sources) == 3
        s.delete(project)
    with session_scope() as s:
        starter_data.ensure_once(s)
    with session_scope() as s:
        assert s.query(CurationProject).filter_by(name="NOAA Pacific T3").count() == 0


def test_items_get_their_split_when_created(client):
    from curation.splits import split_for

    pid = _project(client, "Split at birth")["id"]
    item = _add(client, pid, _on_image("born", "ACBR", "img-z"))
    assert item["split"] == split_for(item["source_image_uri"], 42, (0.8, 0.1, 0.1))
    point = client.post(f"{P}/projects/{pid}/source-images/{item['id']}/points",
                        json={"row_px": 5, "column_px": 5, "label": "POCS"}).json()
    assert point["split"] == item["split"]   # same photo, same split


def test_manifests_and_the_live_export_say_where_each_point_came_from(client, db_session):
    """origin: source (as imported), edited (moved or resized) or manual (added by a curator)."""
    import json

    from curation.models import CurationItem

    pid = _project(client, "Origins")["id"]
    kept = _add(client, pid, _on_image("o1", "ACBR", "origin-img"))
    moved = _add(client, pid, _on_image("o2", "POCS", "origin-img"))
    added = _add(client, pid, _on_image("o3", "SAND", "origin-img"))
    db_session.get(CurationItem, moved["id"]).metadata_json = {"geometry_edited": True}
    db_session.get(CurationItem, added["id"]).metadata_json = {"manual_point": True, "parent_item_id": kept["id"]}
    db_session.commit()
    _publish(client, pid)
    rows = {int(r["item_id"]): r for r in csv.DictReader(io.StringIO(
        client.get(f"{P}/projects/{pid}/versions/1/files/manifest.csv").text))}
    assert {i: rows[i]["origin"] for i in rows} == {kept["id"]: "source", moved["id"]: "edited", added["id"]: "manual"}
    summary = json.loads(client.get(f"{P}/projects/{pid}/versions/1/files/manifest.json").text)
    assert summary["origins"] == {"source": 1, "edited": 1, "manual": 1}
    export = {int(r["id"]): r for r in csv.DictReader(io.StringIO(client.get(f"{P}/projects/{pid}/export").text))}
    assert export[added["id"]]["origin"] == "manual" and export[added["id"]]["parent_item_id"] == str(kept["id"])
    assert export[kept["id"]]["crop_size"] == "224" and export[kept["id"]]["file_name"] == "o1.jpg"
